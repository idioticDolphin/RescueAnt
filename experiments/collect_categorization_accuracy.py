"""
Data collection for Results/correctness_evaluation.tex.

Runs the real categorization step (project's configured LLM, real
bot.config) against experiments/data/categorization_gold_labels.csv's
hand-labeled URLs, and records predicted vs. true category per URL - the
correctness counterpart to notebooks/crawl_overview.ipynb's category
*distribution* figure, which only shows what the pipeline predicted, never
whether it was right.

Fetches share one Playwright browser instance under bounded concurrency for
throughput (matching model.crawler.fetching_service.parse_queue()'s
approach), but categorization itself runs sequentially per page afterwards,
since it's a single local model instance.

Page text is UNCAPPED by default (full real content, same as the
production pipeline) - this script measures correctness, and correctness
against a truncated view of the page is not a meaningful proxy for
correctness against the real page a human or the production pipeline
would see. An earlier version of this script defaulted to a 4000-character
cap for runtime reasons; that was a real methodological mistake (applied
without checking whether it was acceptable for an accuracy measurement,
not just a timing one) and has been reverted. Pass a max_content_chars
value explicitly if you want a capped run for comparison - it is opt-in,
not the default. Runtime is correspondingly slower and less predictable
(a single long LIST page's categorization call alone was observed to take
329s uncapped); budget accordingly rather than assuming this finishes in
minutes.

Usage: python experiments/collect_categorization_accuracy.py [gold_csv_path] [max_content_chars]
"""
import asyncio
import csv
import sys
from pathlib import Path

from common import DATA_DIR, configure_logging, timed, write_csv

DEFAULT_GOLD_PATH = DATA_DIR / "categorization_gold_labels.csv"
DEFAULT_MAX_CONTENT_CHARS = 0  # 0 = uncapped (full real content) - see docstring
OUTPUT_PATH = DATA_DIR / "categorization_accuracy.csv"
MAX_CONCURRENCY = 4


def _read_gold_labels(path):
    rows = []
    with open(path) as f:
        for row in csv.DictReader(f):
            url = row["url"].strip()
            true_category = row["true_category"].strip()
            if url and true_category:
                rows.append((url, true_category))
    return rows


async def _fetch_all(urls, fetching_service):
    from playwright.async_api import async_playwright
    html_by_url = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

        async def fetch_one(url):
            async with semaphore:
                html_by_url[url] = await fetching_service.get_content(url, browser=browser)

        await asyncio.gather(*(fetch_one(url) for url in urls))
        await browser.close()
    return html_by_url


def main():
    configure_logging()
    import logging
    logger = logging.getLogger("experiments.collect_categorization_accuracy")

    gold_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_GOLD_PATH
    max_content_chars = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_MAX_CONTENT_CHARS
    gold = _read_gold_labels(gold_path)
    logger.info("Loaded %d gold-labeled URLs from %s", len(gold), gold_path)

    import model.crawler.fetching_service as fetching_service
    import model.analyzer.category_service as category_service
    import model.analyzer.cleaning_service as cleaning_service

    if max_content_chars:
        _real_clean = cleaning_service.clean
        def _capped_clean(html, deduplicate=False):
            return _real_clean(html, deduplicate=deduplicate)[:max_content_chars]
        cleaning_service.clean = _capped_clean
        logger.info("Page text capped at %d characters for this run", max_content_chars)
    else:
        logger.info("Page text uncapped for this run (slower - see docstring)")

    urls = [url for url, _ in gold]
    with timed() as fetch_t:
        html_by_url = asyncio.run(_fetch_all(urls, fetching_service))
    logger.info("Fetched %d URL(s) in %.1fs", len(urls), fetch_t.seconds)

    rows = []
    fieldnames = ["url", "true_category", "predicted_category", "correct", "fetch_success", "categorize_seconds"]
    correct_count = 0
    fetch_failures = 0
    for i, (url, true_category) in enumerate(gold, 1):
        html = html_by_url.get(url, "")
        fetch_success = bool(html)
        row = {
            "url": url, "true_category": true_category,
            "predicted_category": "", "correct": "", "fetch_success": fetch_success,
            "categorize_seconds": "",
        }
        if not fetch_success:
            fetch_failures += 1
            logger.warning("[%d/%d] %s: fetch failed (or robots.txt-disallowed), skipping categorization", i, len(gold), url)
        else:
            with timed() as t:
                category = category_service.categorize_website(html)
            row["categorize_seconds"] = round(t.seconds, 3)
            row["predicted_category"] = category.name
            is_correct = category.name == true_category
            row["correct"] = is_correct
            if is_correct:
                correct_count += 1
            logger.info(
                "[%d/%d] %s: true=%s predicted=%s (%s) in %.1fs",
                i, len(gold), url, true_category, category.name,
                "correct" if is_correct else "WRONG", t.seconds,
            )
        rows.append(row)
        write_csv(OUTPUT_PATH, rows, fieldnames)  # write incrementally so partial progress isn't lost

    scored = len(gold) - fetch_failures
    logger.info(
        "Done: %d/%d correct (%.1f%%) over %d successfully fetched URL(s) (%d fetch failure(s) excluded)",
        correct_count, scored, 100 * correct_count / scored if scored else 0.0, scored, fetch_failures,
    )
    logger.info("Wrote %d rows to %s", len(rows), OUTPUT_PATH)


if __name__ == "__main__":
    main()
