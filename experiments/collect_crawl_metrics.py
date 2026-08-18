"""
Data collection for notebooks/crawl_overview.ipynb and notebooks/workflow_timing.ipynb.

Runs the real fetch -> categorize -> extract pipeline (project's configured
LLM, real HTTP fetches via Playwright, the project's own bot.config) over a
fixed, reproducible sample of starting_urls.csv, timing each step
individually. Unlike model.orchestrator.process_batch(), which only logs
progress, this records per-URL:
  - fetch/categorize/extract wall-clock duration
  - fetch outcome, assigned category
  - raw HTML size vs cleaned-text size (cleaning_service.clean() output)
  - number of fields/entries extracted, and the raw extracted JSON

Methodology notes (relevant for interpreting the resulting diagrams):
  - Sample selection prefers one URL per distinct domain, in the order they
    appear in starting_urls.csv, so a handful of link-heavy domains don't
    dominate the sample and so the run is reproducible across re-runs.
  - Each URL is fetched independently with its own fresh Playwright browser
    instance (fetching_service.get_content with browser=None), which is
    slower than the pipeline's normal shared-browser batch mode
    (parse_queue()) but keeps per-URL timing isolated and simple to reason
    about - the tradeoff is intentional for a measurement script.
  - The cleaned page text fed to the LLM is capped at MAX_CONTENT_CHARS.
    On CPU inference, prompt processing (prefill) dominates wall-clock time
    for long inputs - some real pages in the sample run to tens of
    thousands of characters, which would make a representative-sized run
    take hours. The cap keeps runtime tractable while still exercising the
    real prompts/grammars/models on real page content; it's a deliberate
    scope limitation of this measurement script, not a change to the
    production pipeline (cleaning_service.clean() itself is untouched).
  - This is a single run on one machine (see README's Requirements for
    specs) - absolute timings are hardware-specific; relative comparisons
    between steps/categories are the more portable takeaway.

Usage: python experiments/collect_crawl_metrics.py [sample_size]
"""
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from common import DATA_DIR, configure_logging, timed, write_csv

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_SAMPLE_SIZE = 16
MAX_CONTENT_CHARS = 1500
OUTPUT_PATH = DATA_DIR / "crawl_metrics.csv"

FIELDNAMES = [
    "url", "domain",
    "fetch_seconds", "fetch_success", "html_bytes",
    "categorize_seconds", "category",
    "cleaned_text_chars",
    "extract_seconds", "extracted_count", "extracted_json",
]


def _select_diverse_urls(path: Path, n: int) -> list[str]:
    """One URL per distinct domain (first occurrence), in file order, up to n."""
    seen_domains = set()
    selected = []
    for line in path.read_text().splitlines():
        url = line.strip()
        if not url:
            continue
        domain = urlparse(url).netloc
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        selected.append(url)
        if len(selected) >= n:
            break
    return selected


def _process_url(url, fetching_service, category_service, extraction_service, cleaning_service, logger):
    row = {field: "" for field in FIELDNAMES}
    row["url"] = url
    row["domain"] = urlparse(url).netloc

    with timed() as t:
        html = asyncio.run(fetching_service.get_content(url))
    row["fetch_seconds"] = round(t.seconds, 3)
    row["fetch_success"] = bool(html)
    row["html_bytes"] = len(html.encode("utf-8"))
    logger.info("Fetched %s in %.1fs (success=%s, %d bytes)", url, t.seconds, bool(html), row["html_bytes"])
    if not html:
        return row

    with timed() as t:
        category = category_service.categorize_website(html)
    row["categorize_seconds"] = round(t.seconds, 3)
    row["category"] = category.name
    logger.info("Categorized %s as %s in %.1fs", url, category.name, t.seconds)

    row["cleaned_text_chars"] = len(cleaning_service.clean(html))

    if not category.is_relevant:
        return row

    with timed() as t:
        extracted = extraction_service.extract_information(html, category, url)
    row["extract_seconds"] = round(t.seconds, 3)
    if extracted:
        data, _links = extracted
        row["extracted_count"] = len(data)
        row["extracted_json"] = json.dumps(data, ensure_ascii=False)
        logger.info("Extracted %d item(s) from %s in %.1fs", len(data), url, t.seconds)
    return row


def main():
    configure_logging()
    import logging
    logger = logging.getLogger("experiments.collect_crawl_metrics")

    sample_size = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SAMPLE_SIZE
    urls = _select_diverse_urls(PROJECT_ROOT / "starting_urls.csv", sample_size)
    logger.info("Selected %d URLs (one per domain) out of starting_urls.csv", len(urls))

    import model.crawler.fetching_service as fetching_service
    import model.analyzer.category_service as category_service
    import model.analyzer.extraction_service as extraction_service
    import model.analyzer.cleaning_service as cleaning_service

    _real_clean = cleaning_service.clean
    def _capped_clean(html, deduplicate=False):
        text = _real_clean(html, deduplicate=deduplicate)
        return text[:MAX_CONTENT_CHARS]
    cleaning_service.clean = _capped_clean
    logger.info("Page text capped at %d characters for this run", MAX_CONTENT_CHARS)

    rows = []
    for i, url in enumerate(urls, 1):
        logger.info("[%d/%d] %s", i, len(urls), url)
        try:
            row = _process_url(
                url, fetching_service, category_service, extraction_service, cleaning_service, logger
            )
        except Exception:
            logger.exception("Failed processing %s, skipping", url)
            continue
        rows.append(row)
        write_csv(OUTPUT_PATH, rows, FIELDNAMES)  # write incrementally so partial progress isn't lost

    logger.info("Wrote %d rows to %s", len(rows), OUTPUT_PATH)


if __name__ == "__main__":
    main()
