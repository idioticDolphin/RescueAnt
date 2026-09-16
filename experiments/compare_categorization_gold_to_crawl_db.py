"""
Compares experiments/data/categorization_gold_labels.csv against what a
*real* crawl session actually stored as each URL's category - the
categorization counterpart to compare_gold_to_crawl_db.py (which does the
same for extraction), and the real-crawl-session replacement for
collect_categorization_accuracy.py's isolated re-fetch-and-recategorize
approach.

Why this comparison matters, same reasoning as compare_gold_to_crawl_db.py:
an isolated re-fetch of each gold URL, run outside the real orchestrator
loop, is not guaranteed to see the same page content or categorize
identically to a real crawl session (different fetch time, different
Playwright browser lifecycle). Scoring gold labels against a real session's
actual database results measures what the pipeline did during an actual
run, not what an isolated harness reproduces afterwards.

Gold URLs not present in the given database (never crawled in that
session) are reported, not treated as an error.

Usage: python experiments/compare_categorization_gold_to_crawl_db.py [gold_csv_path] [db_path]

gold_csv_path defaults to experiments/data/categorization_gold_labels.csv.
db_path defaults to the active bot.config's `database` setting.

Output: experiments/data/categorization_gold_vs_real_crawl.csv, one row per
gold-labeled URL found in the database (url, true_category,
predicted_category, correct, fetch_success), same shape as
categorization_accuracy.csv minus categorize_seconds (a real crawl session's
per-page categorization time lives in that session's _page_timing.csv via
analyze_session.py, not here) - plus a summary printed to stdout.

!! STALE TAXONOMY !!
These labels were assigned under the original five categories
(STATION / LIST / HUB / IRRELEVANT). The crawler now uses twelve, and most of
these labels are wrong under it: a pet shelter labelled STATION is SHELTER, a
page of animals offered for adoption is ANIMAL, a campaigning body is
ADVOCACY. Scoring against this file reports accuracy against a taxonomy the
crawler no longer uses.

The current classification ground truth is the labelled case list in
experiments/categorization_benchmark.py. This file is kept because its URLs
are still a useful sample to relabel from, not because its labels are usable.
"""
import csv
import sqlite3
import sys
from pathlib import Path

from common import DATA_DIR, configure_logging, write_csv

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_GOLD_PATH = DATA_DIR / "categorization_gold_labels.csv"
OUTPUT_PATH = DATA_DIR / "categorization_gold_vs_real_crawl.csv"


def _read_gold_labels(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = row["url"].strip()
            true_category = row["true_category"].strip()
            if url and true_category:
                rows.append((url, true_category))
    return rows


def _find_crawl(connection, url):
    """Return the crawls row for url (by exact source_url match), or None if never crawled."""
    return connection.execute(
        "SELECT category, fetch_success FROM crawls WHERE source_url = ? ORDER BY crawl_id DESC LIMIT 1",
        (url,),
    ).fetchone()


def main():
    configure_logging()
    import logging
    logger = logging.getLogger("experiments.compare_categorization_gold_to_crawl_db")

    gold_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_GOLD_PATH
    if len(sys.argv) > 2:
        db_path = Path(sys.argv[2])
    else:
        # see experiments/analyze_session.py's main() for why this reads the
        # raw config dict instead of building a full validated Config
        import model.tools.config_service as config_service
        db_path = PROJECT_ROOT / config_service._read_config()["database"]

    gold = _read_gold_labels(gold_path)
    logger.info("Loaded %d gold-labeled URL(s) from %s", len(gold), gold_path)
    logger.info("Comparing against real crawl database %s", db_path)

    connection = sqlite3.connect(db_path)

    rows = []
    fieldnames = ["url", "true_category", "predicted_category", "correct", "fetch_success"]
    not_yet_crawled = []
    correct_count = 0
    fetch_failures = 0

    for url, true_category in gold:
        crawl = _find_crawl(connection, url)
        if crawl is None:
            not_yet_crawled.append(url)
            logger.info("%s: not in this database - skipping", url)
            continue

        category, fetch_success = crawl
        fetch_success = bool(fetch_success)
        row = {
            "url": url, "true_category": true_category,
            "predicted_category": "", "correct": "", "fetch_success": fetch_success,
        }
        if not fetch_success:
            fetch_failures += 1
            logger.info("%s: real crawl's fetch failed - skipping", url)
        else:
            predicted_category = category or ""
            is_correct = predicted_category == true_category
            row["predicted_category"] = predicted_category
            row["correct"] = is_correct
            if is_correct:
                correct_count += 1
            logger.info("%s: true=%s predicted=%s (%s)", url, true_category, predicted_category,
                        "correct" if is_correct else "WRONG")
        rows.append(row)

    connection.close()

    write_csv(OUTPUT_PATH, rows, fieldnames)
    logger.info("Wrote %d row(s) to %s", len(rows), OUTPUT_PATH)

    scored = len(rows) - fetch_failures
    accuracy = 100 * correct_count / scored if scored else 0.0
    print("\n=== Categorization: gold vs. real crawl summary ===")
    print(f"Gold URLs: {len(gold)}, found in database: {len(rows)}, "
          f"successfully fetched: {scored}, fetch failures: {fetch_failures}")
    print(f"Overall accuracy: {correct_count}/{scored} = {accuracy:.1f}%")
    if not_yet_crawled:
        print(f"\n{len(not_yet_crawled)} gold URL(s) not in {db_path} (not scored):")
        for url in not_yet_crawled:
            print(f"  {url}")


if __name__ == "__main__":
    main()
