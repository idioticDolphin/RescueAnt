"""
Replay stored pages through the categorizer and score them against labels.

The crawl keeps every fetched body in the page store, so a prompt or taxonomy
change can be evaluated against the exact pages that motivated it - offline,
with no network and no re-crawl. Each case below is a page from a real run,
labelled by hand; the "was" comment records what the run actually produced, so
a regression in the other direction is visible too.

Usage:
    python experiments/categorization_benchmark.py [--db crawl.db] [--limit N]
"""
import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from model.tools import config_service, data_service, page_store  # noqa: E402
from model.analyzer import category_service, boilerplate_service  # noqa: E402

# url substring -> expected category.
# Labels are judgements about the page's role, not about the site as a whole:
# a shelter's job-ad page is HUB even though the shelter itself is a STATION.
CASES = [
    # --- lists of the wrong kind of thing (all were LIST) ---
    ("fressnapf.de/c/katze/katzenfutter",                "COMMERCIAL"),
    ("fressnapf.de/c/katze/katzenfutter/nassfutter",     "COMMERCIAL"),
    ("fressnapf.de/c/hund/hundefutter/ergaenzungsfutter", "COMMERCIAL"),
    ("fressnapf.de/c/hund/hundeschlafplaetze/hundebetten", "COMMERCIAL"),
    ("fressnapf.de/c/garten-teich/wildvoegel/vogelhaeuser-nistkaesten", "COMMERCIAL"),
    ("dogorama.app/de-de/hundeshops",                    "COMMERCIAL"),
    ("dogorama.app/de-de/hundepensionen",                "COMMERCIAL"),
    ("dogorama.app/de-de/ernaehrungsberater",            "COMMERCIAL"),
    ("about.google/locations",                           "IRRELEVANT"),
    # An index of Brazilian government agencies. Labelled IRRELEVANT at
    # first; AUTHORITY is what it actually is, and the model said so.
    ("falabr.cgu.gov.br/web/orgao",                      "AUTHORITY"),
    # memon.eu sells "vitalisation" products; labelled IRRELEVANT at first and
    # corrected to COMMERCIAL after reading the page - the model was right.
    ("memon.eu/",                                        "COMMERCIAL"),

    # --- genuine listings of rescue organisations ---
    ("vbu-ffm.de/pflegestationen.shtml",                 "LIST"),
    ("rlp.nabu.de/tiere-und-pflanzen/tieren-helfen/pflege-und-auffangstationen", "LIST"),
    ("deutsche-wildtierrettung.de/wildtierauffangstationen", "LIST"),

    # --- genuine wildlife rescue: the primary target ---
    ("greifvogelhilfe.de/greifvogelhilfe",               "STATION"),
    ("wildtierzentrum.de/",                              "STATION"),
    ("eichhoernchen-schutz.de/",                         "STATION"),
    ("reptilienauffangstation.de/",                      "STATION"),

    # --- pet shelters: rehoming domestic animals, not wildlife rescue ---
    ("tierheim-marburg.de/",                             "SHELTER"),
    ("tierheim-bergheim.de/",                            "SHELTER"),
    ("franziskustierheim.de/",                           "SHELTER"),
    ("hamburger-tierschutzverein.de/",                   "SHELTER"),
    ("tierheim-mainz.de/",                               "SHELTER"),
    ("tierheim-wetterau-ev.de/",                         "SHELTER"),
    ("tierheim-ostermuenchen.de/unser-tierheim",         "SHELTER"),

    # --- lifetime care without rehoming ---
    ("elztal-gnadenhof.de.tl/",                          "SANCTUARY"),

    # --- veterinary practices ---
    ("tierklinik-hofheim.de/",                           "VET"),
    ("vetpuls.de/",                                      "VET"),
    ("tiermed-muenchen.de/",                             "VET"),

    # --- what-to-do guidance: the densest source of links to real stations ---
    # Titled "Verletztes Wildtier | Pflegestellen bundesweit": it is a
    # nationwide station list, so LIST is right and my first label was not.
    ("wildtierschutz-deutschland.de/verletztes-wildtier", "LIST"),
    ("wildtierschutz-deutschland.de/verletztes-wildtier-gefunden", "ADVICE"),

    # --- campaigning bodies that do not take animals in ---
    # Two political parties and three conservation foundations reached the
    # entity table as rescue stations.
    ("tierschutzpartei.de/",                             "ADVOCACY"),
    ("klimaliste-berlin.de/",                            "ADVOCACY"),
    ("sozis-tiere.de/",                                  "ADVOCACY"),
    ("naturefund.de/",                                   "ADVOCACY"),
    ("natur-zuerst.de/",                                 "ADVOCACY"),

    # --- individual animals ---
    # One sanctuary's per-resident pages came back STATION, LIST, HUB,
    # IRRELEVANT and COMMERCIAL - at random - and seven individual animals
    # ended up in the output as rescue stations.
    ("elztal-gnadenhof.de.tl/Amely.htm",                 "ANIMAL"),
    ("elztal-gnadenhof.de.tl/Bob.htm",                   "ANIMAL"),
    ("elztal-gnadenhof.de.tl/Sina.htm",                  "ANIMAL"),
    ("elztal-gnadenhof.de.tl/Alca.htm",                  "ANIMAL"),
    ("elztal-gnadenhof.de.tl/Gypsy.htm",                 "ANIMAL"),
    ("tierheim-ostermuenchen.de/hund",                   "ANIMAL"),
    ("hamburger-tierschutzverein.de/tiervermittlung/hunde", "ANIMAL"),
    ("tierheim-marburg.de/k/hunde",                      "ANIMAL"),
    ("franziskustierheim.de/tiervermittlung/hunde-17.html", "ANIMAL"),

    # --- subpages of relevant sites: followed, never mined ---
    ("tierheim-ostermuenchen.de/stellenangebote",        "HUB"),
    ("tierheim-ostermuenchen.de/aktuelles",              "HUB"),
    ("tierheim-ostermuenchen.de/gassigeher-und-katzenstreichler", "HUB"),
    ("tierheim-ostermuenchen.de/unsere-vereinszeitung",  "HUB"),
    ("tiermed-muenchen.de/stellenangebote",              "HUB"),
    ("tiermed-muenchen.de/team2",                        "HUB"),
]


def main():
    ap = argparse.ArgumentParser()
    # Several databases, because the labelled pages were collected across
    # more than one run and the page store on disk is shared between them.
    ap.add_argument("--db", nargs="+",
                    default=["crawl.db", "crawl_third_taxonomy_baseline.db",
                             "crawl_second_hub.db"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", help="GGUF to classify with, overriding the config")
    ap.add_argument("--context", type=int, help="context size for --model")
    args = ap.parse_args()

    config_service.load_config()
    config = config_service.get_config()
    if args.model:
        # Swap the classifier without editing config, so several models can be
        # compared against the same labelled pages in one sitting.
        from model.tools import llm_service
        context = args.context or config.category_context
        config = config.model_copy(update={
            "category_model_id": llm_service.get_model_id(args.model, context)})
        config_service._session_config = config
        category_service.config = config
    page_store.configure(config.page_store_path)
    # Templates are learned from stored pages, exactly as in a live run.
    boilerplate_service.forget_all()

    stored = []
    for db in args.db:
        if not Path(db).exists():
            continue
        data_service.DATABASE_PATH = Path(db)
        try:
            stored.extend(data_service.get_all_content_paths())
        except Exception as e:
            # Databases from before the page store exists have no such column.
            print(f"(skipping {db}: {e})")
    # Keep the newest copy of each URL: later runs replace earlier ones.
    seen = {}
    for url, path in stored:
        seen[url] = path
    stored = list(seen.items())
    # Point back at the primary database: boilerplate learning reads through
    # data_service, and leaving it on an old-schema database silently disabled
    # template stripping for the whole benchmark.
    data_service.DATABASE_PATH = Path(args.db[0])
    cases = CASES[: args.limit] if args.limit else CASES

    rows, confusion, elapsed = [], Counter(), []
    for needle, expected in cases:
        match = next(((u, p) for u, p in stored if needle in u and p), None)
        if match is None:
            rows.append((needle, expected, "MISSING", False))
            continue
        url, path = match
        html = page_store.load(path)
        if not html:
            rows.append((needle, expected, "NOCONTENT", False))
            continue
        started = time.monotonic()
        got = category_service.categorize_website(html, url=url)
        elapsed.append(time.monotonic() - started)
        got = getattr(got, "name", got)
        ok = got == expected
        confusion[(expected, got)] += 1
        rows.append((needle, expected, got, ok))

    width = max(len(r[0]) for r in rows)
    for needle, expected, got, ok in rows:
        mark = "ok  " if ok else "FAIL"
        print(f"{mark} {needle:<{width}}  expected={expected:<11} got={got}")

    scored = [r for r in rows if r[2] not in ("MISSING", "NOCONTENT")]
    correct = sum(1 for r in scored if r[3])
    print(f"\n{correct}/{len(scored)} correct"
          f"  ({100 * correct / len(scored):.0f}%)" if scored else "\nnothing scored")
    skipped = len(rows) - len(scored)
    if skipped:
        print(f"{skipped} case(s) skipped - page not in this store")

    if elapsed:
        total = sum(elapsed)
        print(f"classification: {total:.0f}s for {len(elapsed)} pages "
              f"= {total / len(elapsed):.1f}s per page")

    print("\nmistakes (expected -> got):")
    for (exp, got), n in sorted(confusion.items(), key=lambda kv: -kv[1]):
        if exp != got:
            print(f"  {n:3}  {exp} -> {got}")


if __name__ == "__main__":
    main()
