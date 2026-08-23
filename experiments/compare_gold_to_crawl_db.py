"""
Compares experiments/data/extraction_gold_labels.csv against what a *real*
crawl (python src/main.py, via model.orchestrator) actually put in its
database for those same URLs - as opposed to
experiments/collect_extraction_correctness.py, which re-fetches each gold
URL in isolation and re-runs categorize/extract fresh, outside the real
orchestrator loop.

Why this comparison matters: the isolated experiment and a real crawl run
the same categorize_website()/extract_information() calls, so in principle
they should agree - but they don't have to, since the isolated script
fetches independently (possibly at a different time, with a different
Playwright browser lifecycle - see its docstring) rather than reading back
what a real orchestrator.process_batch() run already fetched and stored.
A real, observed case of this divergence: the isolated experiment's
extraction_correctness_entry_counts.csv shows only 1/9 gold entries
extracted for the wp.wildvogelhilfe.org LIST page, but a real crawl
(session_20260822_022754, see sessions/ and experiments/analyze_session.py)
correctly extracted all 9 station entries for the same URL with names
matching gold closely - directly contradicting the isolated experiment's
result for that page. This script exists to make that kind of check
routine rather than something noticed by accident.

Uses the same gold-label reading, name-matching, and field-scoring logic as
collect_extraction_correctness.py (factored out into gold_scoring.py) so
the two are directly, row-for-row comparable - a gold URL present in both
outputs can be diffed field-by-field between "isolated re-fetch" and "real
crawl" results.

Gold URLs not yet present in the given database are reported, not treated
as an error - this script is meant to be re-run as real crawl coverage of
the gold-labeled URLs grows (e.g. once a fresh crawl session on another
machine finishes and its database is copied over), not to require gold
labels and a specific crawl run to line up perfectly upfront.

Usage: python experiments/compare_gold_to_crawl_db.py [gold_csv_path] [db_path]

gold_csv_path defaults to experiments/data/extraction_gold_labels.csv.
db_path defaults to the active bot.config's `database` setting.

Output: experiments/data/gold_vs_real_crawl.csv (matched-entry field
scores, same shape as extraction_correctness.csv) and
experiments/data/gold_vs_real_crawl_entry_counts.csv (per-URL gold vs.
real-crawl-extracted entry counts, same shape as
extraction_correctness_entry_counts.csv), plus a summary printed to stdout.
"""
import json
import sqlite3
import sys
from pathlib import Path

from common import DATA_DIR, configure_logging, write_csv
from gold_scoring import ROW_FIELDNAMES, match_gold_to_extracted, read_gold_labels, score_entry

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_GOLD_PATH = DATA_DIR / "extraction_gold_labels.csv"
OUTPUT_PATH = DATA_DIR / "gold_vs_real_crawl.csv"
SUMMARY_OUTPUT_PATH = DATA_DIR / "gold_vs_real_crawl_entry_counts.csv"

# entries.<field> columns don't necessarily match gold_scoring.FIELDS names 1:1
# (config_service._build_schema() derives DB columns from bot.config's `fields`,
# not from this evaluation harness) - map the ones the gold CSV actually scores.
ENTRY_FIELD_MAP = {
    "name": "name", "address": "address", "telephone": "telephone",
    "accepted_animals": "accepted_animals", "animal_pickup": "animal_pickup",
}


def _find_crawl(connection, url):
    """Return the crawls row for url (by exact source_url match), or None if never crawled."""
    row = connection.execute(
        "SELECT crawl_id, category, fetch_success FROM crawls WHERE source_url = ? ORDER BY crawl_id DESC LIMIT 1",
        (url,),
    ).fetchone()
    return row


def _load_entries(connection, crawl_id):
    """Return this crawl's entries as a list of dicts shaped like extraction_service.extract_information()'s output."""
    columns = [row[1] for row in connection.execute("PRAGMA table_info(entries)")]
    rows = connection.execute("SELECT * FROM entries WHERE source_crawl_id = ?", (crawl_id,)).fetchall()
    entries = []
    for row in rows:
        raw = dict(zip(columns, row))
        entry = {}
        for gold_field, db_field in ENTRY_FIELD_MAP.items():
            value = raw.get(db_field)
            if gold_field == "accepted_animals" and value:
                try:
                    value = json.loads(value)  # data_service._to_sql_value() JSON-encodes list/dict fields
                except (json.JSONDecodeError, TypeError):
                    pass
            entry[gold_field] = value
        entries.append(entry)
    return entries


def main():
    configure_logging()
    import logging
    logger = logging.getLogger("experiments.compare_gold_to_crawl_db")

    gold_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_GOLD_PATH
    if len(sys.argv) > 2:
        db_path = Path(sys.argv[2])
    else:
        # see experiments/analyze_session.py's main() for why this reads the
        # raw config dict instead of building a full validated Config
        import model.tools.config_service as config_service
        db_path = PROJECT_ROOT / config_service._read_config()["database"]

    gold_by_url = read_gold_labels(gold_path)
    logger.info("Loaded gold labels for %d source URL(s) from %s", len(gold_by_url), gold_path)
    logger.info("Comparing against real crawl database %s", db_path)

    connection = sqlite3.connect(db_path)
    connection.row_factory = None

    rows = []
    summary_rows = []
    not_yet_crawled = []

    for url, gold_entries in gold_by_url.items():
        crawl = _find_crawl(connection, url)
        if crawl is None:
            not_yet_crawled.append(url)
            logger.info("%s: not in this database yet - skipping", url)
            continue

        crawl_id, category, fetch_success = crawl
        if not fetch_success:
            logger.info("%s: real crawl's fetch failed - skipping", url)
            summary_rows.append({"source_url": url, "category": category or "", "fetch_success": False,
                                  "gold_entry_count": len(gold_entries), "extracted_entry_count": 0})
            continue

        extracted_entries = _load_entries(connection, crawl_id)
        summary_rows.append({
            "source_url": url, "category": category or "", "fetch_success": True,
            "gold_entry_count": len(gold_entries), "extracted_entry_count": len(extracted_entries),
        })
        logger.info("%s: category=%s gold=%d real_crawl_extracted=%d",
                    url, category, len(gold_entries), len(extracted_entries))

        for gold, extracted, name_score in match_gold_to_extracted(gold_entries, extracted_entries):
            rows.append(score_entry(url, category or "", gold, extracted, name_score))

    connection.close()

    write_csv(OUTPUT_PATH, rows, ROW_FIELDNAMES)
    write_csv(SUMMARY_OUTPUT_PATH, summary_rows,
              ["source_url", "category", "fetch_success", "gold_entry_count", "extracted_entry_count"])
    logger.info("Wrote %d matched-entry row(s) to %s", len(rows), OUTPUT_PATH)
    logger.info("Wrote %d source-URL summary row(s) to %s", len(summary_rows), SUMMARY_OUTPUT_PATH)

    print("\n=== Gold vs. real crawl summary ===")
    for s in summary_rows:
        flag = " <-- mismatch vs. gold count" if s["gold_entry_count"] != s["extracted_entry_count"] else ""
        print(f"  {s['source_url']}: category={s['category']} gold={s['gold_entry_count']} "
              f"real_crawl={s['extracted_entry_count']}{flag}")
    if not_yet_crawled:
        print(f"\n{len(not_yet_crawled)} gold URL(s) not yet in {db_path} (not scored):")
        for url in not_yet_crawled:
            print(f"  {url}")
        print("Re-run this script once a crawl covering these URLs has finished.")


if __name__ == "__main__":
    main()
