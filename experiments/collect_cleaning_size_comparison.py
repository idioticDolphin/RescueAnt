"""
Data collection for notebooks/workflow_timing.ipynb's cleaning-size comparison.

Fetch-only (no LLM involved at all) - re-fetches each URL and measures how much
cleaning_service.clean() reduces raw HTML down to the plain text actually handed
to the model, separately for the two different ways the pipeline calls it:
  - category_service.categorize_website(): clean(html, deduplicate=True)
  - extraction_service.extract_information(): clean(html) (deduplicate=False)

These are genuinely different-length outputs from the same page (deduplication
drops repeated lines - e.g. repeated nav/list-item boilerplate), so a single
"cleaned_text_chars" figure (as recorded in crawl_metrics.csv, which only ever
calls clean() with the extraction/deduplicate=False variant) understates how
much smaller categorization's actual input is.

Usage: python experiments/collect_cleaning_size_comparison.py [urls_path]
"""
import asyncio
import sys
from pathlib import Path

from common import DATA_DIR, configure_logging, write_csv

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_URLS_PATH = PROJECT_ROOT / "starting_urls.csv"
OUTPUT_PATH = DATA_DIR / "cleaning_size_comparison.csv"

FIELDNAMES = [
    "url", "fetch_success", "html_bytes",
    "categorize_cleaned_chars", "extract_cleaned_chars",
    "categorize_reduction_pct", "extract_reduction_pct",
]


def main():
    configure_logging()
    import logging
    logger = logging.getLogger("experiments.collect_cleaning_size_comparison")

    urls_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_URLS_PATH
    urls = [line.strip() for line in urls_path.read_text().splitlines() if line.strip()]
    logger.info("Loaded %d URL(s) from %s", len(urls), urls_path)

    import model.crawler.fetching_service as fetching_service
    import model.analyzer.cleaning_service as cleaning_service

    rows = []
    for i, url in enumerate(urls, 1):
        html = asyncio.run(fetching_service.get_content(url))
        fetch_success = bool(html)
        row = {field: "" for field in FIELDNAMES}
        row["url"] = url
        row["fetch_success"] = fetch_success
        if not fetch_success:
            logger.warning("[%d/%d] %s: fetch failed (or robots.txt-disallowed), skipping", i, len(urls), url)
            rows.append(row)
            write_csv(OUTPUT_PATH, rows, FIELDNAMES)
            continue

        html_bytes = len(html.encode("utf-8"))
        categorize_chars = len(cleaning_service.clean(html, deduplicate=True))
        extract_chars = len(cleaning_service.clean(html))

        row["html_bytes"] = html_bytes
        row["categorize_cleaned_chars"] = categorize_chars
        row["extract_cleaned_chars"] = extract_chars
        row["categorize_reduction_pct"] = round(100 * (1 - categorize_chars / html_bytes), 1)
        row["extract_reduction_pct"] = round(100 * (1 - extract_chars / html_bytes), 1)
        logger.info(
            "[%d/%d] %s: html_bytes=%d categorize_cleaned=%d (-%.1f%%) extract_cleaned=%d (-%.1f%%)",
            i, len(urls), url, html_bytes, categorize_chars, row["categorize_reduction_pct"],
            extract_chars, row["extract_reduction_pct"],
        )
        rows.append(row)
        write_csv(OUTPUT_PATH, rows, FIELDNAMES)

    logger.info("Wrote %d rows to %s", len(rows), OUTPUT_PATH)


if __name__ == "__main__":
    main()
