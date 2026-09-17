"""
Score a listing's entry filter against judged records.

A listing page can be a genuine list of stations and still mix in vets, zoos
and shelters, which page-level judgement cannot fix. This puts one filter call
per listing page - the entries as a numbered list - and scores its verdicts
against an entity sample that was judged by hand
(experiments/data/precision/*.csv, column "wildlife").

Only judged names are scored; the rest of each listing is passed to the model
as context, exactly as in a real call.

Usage:
    python experiments/record_filter_trial.py [--judged sample.csv] [--prompt "..."]
"""
import argparse
import csv
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from model.tools import config_service, llm_service  # noqa: E402
from model.analyzer import extraction_service  # noqa: E402

DEFAULT_JUDGED = Path(__file__).parent / "data" / "precision" / "sample_0917_lexicon.csv"
DEFAULT_PROMPT = (
    "Each line is one organisation listed on a web page, numbered. Answer with a JSON array "
    "holding the number of every organisation that takes in and cares for injured or orphaned "
    "wild animals - a wildlife rescue or care station, including bird, hedgehog, squirrel, bat "
    "and seal stations and private wildlife carers. Leave out everything else: animal shelters "
    "for pets (Tierheim, Tierschutzverein), veterinary practices and clinics (Tierklinik, "
    "Tierarzt), zoos, wildlife and bird parks, falconry shows, fawn-rescue groups (Kitzrettung), "
    "authorities, associations that keep no animals, and headings that are not an organisation."
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="crawl.db")
    ap.add_argument("--judged", default=str(DEFAULT_JUDGED))
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--removals", action="store_true",
                    help="the answer names the entries to remove, not to keep")
    args = ap.parse_args()

    config_service.load_config()
    config = config_service.get_config()
    category = config.get_category("LIST").model_copy(update={"record_filter_prompt": args.prompt,
                                                       "record_filter_names_removals": args.removals})
    llm = llm_service.get_model(category.analysis_model_id)

    with open(args.judged, encoding="utf-8") as f:
        judged = {(r["name"] or "").strip(): r["wildlife"] for r in csv.DictReader(f) if r.get("wildlife")}

    c = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    pages = c.execute("""SELECT cr.crawl_id, cr.source_url FROM crawls cr
                         WHERE cr.category = 'LIST'
                           AND EXISTS (SELECT 1 FROM entries e WHERE e.source_crawl_id = cr.crawl_id)
                         ORDER BY cr.source_url""").fetchall()
    verdicts = Counter()
    lost = []
    started = time.monotonic()
    for crawl_id, url in pages:
        records = [{"name": r[0]} for r in c.execute(
            "SELECT name FROM entries WHERE source_crawl_id = ?", (crawl_id,))]
        kept = extraction_service._kept_by_filter(records, category, llm, url)
        kept_names = {(r.get("name") or "").strip() for r in kept}
        page_judged = [(r["name"] or "").strip() for r in records
                       if (r["name"] or "").strip() in judged]
        for name in page_judged:
            verdict = "kept" if name in kept_names else "dropped"
            verdicts[(judged[name], verdict)] += 1
            if judged[name] == "yes" and verdict == "dropped":
                lost.append(name)
        print(f"{len(kept):>3}/{len(records):<3} kept  {len(page_judged):>2} judged  "
              f"{url.split('//', 1)[-1][:70]}", flush=True)

    total_judged = sum(verdicts.values())
    print(f"\n{len(pages)} listing pages, {total_judged} judged entries, "
          f"{(time.monotonic() - started) / max(len(pages), 1):.1f}s per page")
    for label in ("yes", "unsure", "no"):
        kept = verdicts[(label, "kept")]
        dropped = verdicts[(label, "dropped")]
        if kept + dropped:
            print(f"  judged {label:<6} kept {kept:>3}  dropped {dropped:>3}")
    if lost:
        print("stations dropped:", "; ".join(n[:40] for n in lost))
    stations = verdicts[("yes", "kept")] + verdicts[("yes", "dropped")]
    junk = verdicts[("no", "kept")] + verdicts[("no", "dropped")]
    if stations and junk:
        print(f"\nstations lost: {verdicts[('yes', 'dropped')]}/{stations}   "
              f"non-stations removed: {verdicts[('no', 'dropped')]}/{junk}")


if __name__ == "__main__":
    main()
