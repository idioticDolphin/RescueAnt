"""
Ask a yes/no question of pages classified LIST, and score it against labels.

Directory sites of fawn-rescue groups and animal shelters are classified LIST
however the category is described, and each page puts dozens of records that
are not stations into the database. A single focused question about the
entries may separate them where a fourteen-way choice does not. This measures
that before it is built into the pipeline.

Labels: experiments/data/list_page_labels.csv (url, station_list yes|no).

Usage:
    python experiments/list_confirmation_trial.py [--question "..."]
"""
import argparse
import csv
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from llama_cpp import LlamaGrammar  # noqa: E402
from model.tools import config_service, data_service, llm_service, page_store  # noqa: E402
from model.analyzer import cleaning_service, boilerplate_service  # noqa: E402

LABELS = Path(__file__).parent / "data" / "list_page_labels.csv"
DEFAULT_QUESTION = ("Look at the entries this page lists and say what most of them are. "
                    "wildlife-stations: places that take in and care for injured or orphaned wild "
                    "animals, such as wildlife rescue centres, bird, hedgehog, squirrel or bat "
                    "care stations and private wildlife carers. animal-shelters: shelters and "
                    "rehoming societies for pets. fawn-rescue: groups or drone pilots that search "
                    "fields for fawns before mowing. vets: veterinary practices and clinics. "
                    "other: anything else.")
# Only this answer keeps a page a station listing.
ACCEPT = "wildlife-stations"
ANSWERS = ("fawn-rescue", "animal-shelters", "vets", "other", "wildlife-stations")


def ask(llm, llm_id, question, html, url, config):
    text = cleaning_service.clean(html, deduplicate=True)
    text = boilerplate_service.strip_for(text, url, config)
    text = llm_service.fit_to_context(llm, f"URL: {url}\n{text}",
                                      llm_service.get_context(llm_id), 4, overhead=question)
    result = llm_service.complete(
        llm, messages=[{"role": "system", "content": question},
                       {"role": "user", "content": f"Website content:\n{text}"}],
        grammar=LlamaGrammar.from_string("root ::= " + " | ".join(f'"{a}"' for a in ANSWERS)),
        temperature=0, max_tokens=8)
    return result["choices"][0]["message"]["content"].strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="crawl.db")
    ap.add_argument("--question", default=DEFAULT_QUESTION)
    ap.add_argument("--include-unsure", action="store_true")
    ap.add_argument("--labels", default=str(LABELS),
                    help="CSV of url, accept (yes|no) or station_list, confidence")
    ap.add_argument("--answers", help="pipe-separated answers, the accepted one included")
    ap.add_argument("--accept", help="the answer that confirms the page")
    args = ap.parse_args()

    config_service.load_config()
    config = config_service.get_config()
    data_service.DATABASE_PATH = Path(args.db)
    page_store.configure(config.page_store_path)
    boilerplate_service.forget_all()
    llm_id = config.get_category_model_id()
    llm = llm_service.get_model(llm_id)

    global ANSWERS, ACCEPT
    if args.answers:
        ANSWERS = tuple(args.answers.split("|"))
    if args.accept:
        ACCEPT = args.accept
    with open(args.labels, encoding="utf-8") as f:
        labels = [r for r in csv.DictReader(f) if args.include_unsure or r["confidence"] == "sure"]
    c = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    lost = kept_junk = correct = 0
    started = time.monotonic()
    for row in labels:
        hit = c.execute("SELECT content_path FROM crawls WHERE source_url = ? AND content_path IS NOT NULL",
                        (row["url"],)).fetchone()
        html = page_store.load(hit[0]) if hit else None
        if not html:
            continue
        want = row.get("accept") or row.get("station_list")
        said = ask(llm, llm_id, args.question, html, row["url"], config)
        answer = "yes" if said == ACCEPT else "no"
        ok = answer == want
        correct += ok
        lost += want == "yes" and answer == "no"
        kept_junk += want == "no" and answer == "yes"
        print(f"{'ok  ' if ok else 'FAIL'} want={want:<3} got={said:<17} {row['url'][:70]}", flush=True)
    n = correct + lost + kept_junk
    print(f"\n{correct}/{n} correct; station lists rejected: {lost}; other lists accepted: {kept_junk}; "
          f"{(time.monotonic() - started) / max(n, 1):.1f}s per page")


if __name__ == "__main__":
    main()
