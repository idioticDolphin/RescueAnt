"""
Data collection for notebooks/discovery_yield.ipynb.

Runs real search-engine discovery (the project's configured SearXNG
instance and search_queries.csv) in fixed-size batches - the same
discovery_batch_size mechanism model.orchestrator.run_discovery() uses -
and records, per batch: how many queries have been used so far, how many
*new* unique URLs that batch contributed, and the cumulative unique URL
count. This is what motivates running discovery in small batches instead
of all queries at once (see model.orchestrator.run()'s docstring): if
per-batch yield drops off quickly, later batches are mostly wasted API
calls unless the crawl queue has actually run dry.

No LLM involved - just the configured SearchProvider - so this is fast and
uses the real network (SearXNG at http://localhost:8080 by default; see the
README's "Search engines" section).

Usage: python experiments/collect_discovery_yield.py [batch_size] [max_batches]
"""
import sys

from common import DATA_DIR, configure_logging, timed, write_csv

OUTPUT_PATH = DATA_DIR / "discovery_yield.csv"
DEFAULT_BATCH_SIZE = 5
DEFAULT_MAX_BATCHES = 12


def main():
    configure_logging()
    import logging
    logger = logging.getLogger("experiments.collect_discovery_yield")

    batch_size = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_BATCH_SIZE
    max_batches = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_MAX_BATCHES

    import model.tools.config_service as config_service
    config = config_service.get_config()
    import model.crawler.discovery_service as discovery_service

    if not config.discover_urls:
        logger.error("discover_urls is False in the active config - nothing to measure. Aborting.")
        return

    all_queries = discovery_service.read_query_templates(config.get_search_query_path())
    logger.info("Loaded %d queries from %s", len(all_queries), config.get_search_query_path())

    provider = config.get_search_provider()
    seen_urls: set[str] = set()
    rows = []
    fieldnames = [
        "batch", "queries_used_cumulative", "batch_seconds",
        "batch_unique_urls", "batch_new_urls", "cumulative_unique_urls",
    ]

    for batch_index in range(max_batches):
        start = batch_index * batch_size
        batch_queries = all_queries[start:start + batch_size]
        if not batch_queries:
            logger.info("No queries left after batch %d, stopping.", batch_index)
            break

        with timed() as t:
            discovered = discovery_service.discover_urls(
                provider, batch_queries,
                results_per_query=config.results_per_query,
                politeness=config.query_politeness,
            )
        new_urls = set(discovered) - seen_urls
        seen_urls |= new_urls

        logger.info(
            "Batch %d: %d quer%s -> %d URL(s), %d new (cumulative: %d) in %.1fs",
            batch_index, len(batch_queries), "y" if len(batch_queries) == 1 else "ies",
            len(discovered), len(new_urls), len(seen_urls), t.seconds,
        )
        rows.append({
            "batch": batch_index,
            "queries_used_cumulative": start + len(batch_queries),
            "batch_seconds": round(t.seconds, 3),
            "batch_unique_urls": len(discovered),
            "batch_new_urls": len(new_urls),
            "cumulative_unique_urls": len(seen_urls),
        })
        write_csv(OUTPUT_PATH, rows, fieldnames)

    logger.info("Wrote %d rows to %s", len(rows), OUTPUT_PATH)


if __name__ == "__main__":
    main()
