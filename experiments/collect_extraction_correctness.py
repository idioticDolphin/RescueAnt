"""
Data collection for Results/correctness_evaluation.tex.

Runs the real fetch -> categorize -> extract pipeline against
experiments/data/extraction_gold_labels.csv's hand-labeled entries, and
scores the model's output against that ground truth for the reduced
5-field subset (name, address, telephone, accepted_animals, animal_pickup
- see correctness_evaluation.tex for why these five and not all 14
configured fields).

Gold rows are grouped by source_url; a source_url with multiple gold rows
is a LIST page a human sampled a handful of entries from - not necessarily
every entry on the page (see correctness_evaluation.tex's sampling note).
Extracted entries are matched to gold entries by name similarity
(difflib.SequenceMatcher), not position, since neither the page's nor the
model's entry order is canonical - a gold row is matched to whichever
not-yet-used extracted entry has the highest name-similarity score,
assigned greedily best-score-first across the whole page.

Field comparison, since exact string equality is too strict for
human-transcribed text:
  - name, address: difflib.SequenceMatcher ratio (0-1), plus a boolean
    "close" flag at a 0.8 threshold.
  - telephone: compared as digits-only (formatting varies a lot across
    sites - "+1 250-337-2021" vs "0170-2122524" vs spaces).
  - accepted_animals: both sides split on "|" (gold) / treated as a list
    (extracted) into a set of lowercased strings, scored as Jaccard overlap
    (intersection / union) - not exact set equality, since accepted_animals
    is unconstrained free text (see Extraction Design's note on this) and
    there's no fixed vocabulary both sides are guaranteed to share.
  - animal_pickup: exact boolean match, only scored where the gold value is
    non-blank (blank means "not stated by the labeler", not "False").

This does NOT measure recall for list pages (whether the model found every
station on the page) - gold entries here are a sample, not necessarily the
full list, so an unmatched gold entry means the model didn't produce that
*specific* entry, not that it under-counted overall. Entry counts (gold
rows vs. extracted entries) are still recorded per source_url for
reference, with that caveat.

Usage: python experiments/collect_extraction_correctness.py [gold_csv_path]
"""
import asyncio
import sys
from pathlib import Path

from common import DATA_DIR, configure_logging, timed, write_csv
from gold_scoring import ROW_FIELDNAMES, match_gold_to_extracted, read_gold_labels, score_entry

DEFAULT_GOLD_PATH = DATA_DIR / "extraction_gold_labels.csv"
OUTPUT_PATH = DATA_DIR / "extraction_correctness.csv"
SUMMARY_OUTPUT_PATH = DATA_DIR / "extraction_correctness_entry_counts.csv"


def main():
    configure_logging()
    import logging
    logger = logging.getLogger("experiments.collect_extraction_correctness")

    gold_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_GOLD_PATH
    gold_by_url = read_gold_labels(gold_path)
    logger.info("Loaded gold labels for %d source URL(s) from %s", len(gold_by_url), gold_path)

    import model.crawler.fetching_service as fetching_service
    import model.analyzer.category_service as category_service
    import model.analyzer.extraction_service as extraction_service

    rows = []
    fieldnames = ROW_FIELDNAMES
    summary_rows = []
    summary_fieldnames = ["source_url", "category", "fetch_success", "gold_entry_count", "extracted_entry_count"]

    for i, (url, gold_entries) in enumerate(gold_by_url.items(), 1):
        logger.info("[%d/%d] %s (%d gold entr%s)", i, len(gold_by_url), url,
                    len(gold_entries), "y" if len(gold_entries) == 1 else "ies")

        html = asyncio.run(fetching_service.get_content(url))
        fetch_success = bool(html)
        if not fetch_success:
            logger.warning("  fetch failed (or robots.txt-disallowed), skipping")
            summary_rows.append({"source_url": url, "category": "", "fetch_success": False,
                                  "gold_entry_count": len(gold_entries), "extracted_entry_count": 0})
            write_csv(SUMMARY_OUTPUT_PATH, summary_rows, summary_fieldnames)
            continue

        category = category_service.categorize_website(html)
        logger.info("  categorized as %s", category.name)

        extracted_entries = []
        if category.is_relevant:
            result = extraction_service.extract_information(html, category, url)
            if result:
                extracted, _links = result
                extracted_entries = extracted if isinstance(extracted, list) else [extracted]

        summary_rows.append({
            "source_url": url, "category": category.name, "fetch_success": True,
            "gold_entry_count": len(gold_entries), "extracted_entry_count": len(extracted_entries),
        })
        write_csv(SUMMARY_OUTPUT_PATH, summary_rows, summary_fieldnames)
        logger.info("  gold=%d extracted=%d", len(gold_entries), len(extracted_entries))

        for gold, extracted, name_score in match_gold_to_extracted(gold_entries, extracted_entries):
            if extracted is None:
                logger.info("  no extracted entry matched gold '%s' (name similarity below threshold)", gold["name"])
            rows.append(score_entry(url, category.name, gold, extracted, name_score))
            write_csv(OUTPUT_PATH, rows, fieldnames)

    logger.info("Wrote %d matched-entry row(s) to %s", len(rows), OUTPUT_PATH)
    logger.info("Wrote %d source-URL summary row(s) to %s", len(summary_rows), SUMMARY_OUTPUT_PATH)


if __name__ == "__main__":
    main()
