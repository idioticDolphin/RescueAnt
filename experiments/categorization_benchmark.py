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
    ("falabr.cgu.gov.br/web/orgao",                      "IRRELEVANT"),

    # --- genuine listings of rescue organisations (were LIST: keep them) ---
    ("vbu-ffm.de/pflegestationen.shtml",                 "LIST"),
    ("rlp.nabu.de/tiere-und-pflanzen/tieren-helfen/pflege-und-auffangstationen", "LIST"),
    ("deutsche-wildtierrettung.de/wildtierauffangstationen", "LIST"),

    # --- a shelter's own main page (were STATION: keep them) ---
    ("tierheim-ostermuenchen.de/unser-tierheim",         "STATION"),
    ("tierheim-marburg.de/",                             "STATION"),
    ("tierheim-bergheim.de/",                            "STATION"),
    ("franziskustierheim.de/",                           "STATION"),
    ("hamburger-tierschutzverein.de/",                   "STATION"),

    # --- subpages of shelter sites (all were STATION: the duplication source) ---
    ("tierheim-ostermuenchen.de/stellenangebote",        "HUB"),
    ("tierheim-ostermuenchen.de/aktuelles",              "HUB"),
    ("tierheim-ostermuenchen.de/hund",                   "HUB"),
    ("tierheim-ostermuenchen.de/katze",                  "HUB"),
    ("tierheim-ostermuenchen.de/gassigeher-und-katzenstreichler", "HUB"),
    ("tierheim-ostermuenchen.de/vermisste-tiere",        "HUB"),
    ("tierheim-ostermuenchen.de/unsere-vereinszeitung",  "HUB"),
    ("tierheim-marburg.de/k/hunde",                      "HUB"),
    ("tierschutzverein-muenchen.de/tiervermittlung/tierheim/hunde", "HUB"),
    ("hamburger-tierschutzverein.de/tiervermittlung/hunde", "HUB"),
    ("tiermed-muenchen.de/stellenangebote",              "HUB"),
    ("tiermed-muenchen.de/team2",                        "HUB"),
    ("franziskustierheim.de/tiervermittlung/hunde-17.html", "HUB"),

    # --- not rescue organisations at all (were STATION) ---
    # memon.eu sells "vitalisation" products; labelled IRRELEVANT at first,
    # corrected to COMMERCIAL after looking at the page - both are non-
    # extracting, so the distinction only affects referrer weight.
    ("memon.eu/",                                        "COMMERCIAL"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="crawl.db")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    config_service.load_config()
    config = config_service.get_config()
    data_service.DATABASE_PATH = Path(args.db)
    page_store.configure(config.page_store_path)
    # Templates are learned from stored pages, exactly as in a live run.
    boilerplate_service.forget_all()

    stored = data_service.get_all_content_paths()
    cases = CASES[: args.limit] if args.limit else CASES

    rows, confusion = [], Counter()
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
        got = category_service.categorize_website(html, url=url)
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

    print("\nmistakes (expected -> got):")
    for (exp, got), n in sorted(confusion.items(), key=lambda kv: -kv[1]):
        if exp != got:
            print(f"  {n:3}  {exp} -> {got}")


if __name__ == "__main__":
    main()
