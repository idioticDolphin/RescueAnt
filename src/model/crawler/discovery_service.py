"""
Generates new URLs to crawl, so the queue doesn't depend entirely on manually
curated starting_urls.csv. Two discovery paths:

1. Search-engine discovery: build queries from keyword/location templates,
   run them against a pluggable SearchProvider, queue the results.
2. Known-site discovery: feed URLs already extracted from a LIST-category
   page (cleaning_service.extract_links / extraction_service's second
   return value) back into the crawl queue.

Both paths ultimately go through queue_discovered_urls(), which just wraps
fetching_service.queue_url() - so discovered/extracted URLs get the same
dedup-against-already-processed and dedup-against-already-queued behavior
as every other URL in the system, and the same robots.txt / politeness
handling once they're actually fetched.
"""
from __future__ import annotations
import time
from typing import Iterable
from urllib.parse import urlparse
from model.objects.searchprovider import *

import model.crawler.fetching_service as fetching_service

CRAWLABLE_SCHEMES = {"http", "https"}


def generate_queries(keyword_templates: Iterable[str], locations: Iterable[str]) -> list[str]:
    """
    Combine keyword templates with locations to build a list of search
    queries. A template containing "{location}" gets that substituted in
    directly (e.g. "Tierheim in {location}" -> "Tierheim in Marburg");
    a plain keyword with no placeholder is just appended with a space
    (e.g. "Wildtierhilfe" -> "Wildtierhilfe Marburg").
    """
    queries = []
    for template in keyword_templates:
        for location in locations:
            if "{location}" in template:
                queries.append(template.format(location=location))
            else:
                queries.append(f"{template} {location}")
    return queries


# ---------------------------------------------------------------------------
# Discovery + queueing
# ---------------------------------------------------------------------------

def _is_valid_crawl_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in CRAWLABLE_SCHEMES and bool(parsed.netloc)


def discover_urls(
    provider: SearchProvider,
    queries: Iterable[str],
    results_per_query: int = 10,
    politeness: float = 1.0,
) -> list[str]:
    """
    Run each query against the given provider, filter to valid crawlable
    URLs, and return the deduplicated combined list. Sleeps `politeness`
    seconds between queries - separate from fetching_service's per-domain
    politeness_delay, since this is rate-limiting calls to the search API
    itself, not to the sites being discovered.

    A failing individual query is logged and skipped rather than aborting
    the whole batch, since search APIs can be flaky/rate-limited and one
    bad query shouldn't cost you every other result.
    """
    discovered: set[str] = set()
    queries = list(queries)
    for i, query in enumerate(queries):
        try:
            results = provider.search(query, results_per_query)
        except Exception as e:
            print(f"Search failed for query {query!r}: {e!r}")
            results = []

        for url in results:
            if _is_valid_crawl_url(url):
                discovered.add(url)

        if i < len(queries) - 1:
            time.sleep(politeness)

    return sorted(discovered)


def queue_discovered_urls(urls: Iterable[str]) -> int:
    """
    Queue newly discovered URLs for crawling via fetching_service.
    fetching_service.queue_url() already dedupes against both the current
    queue and already-processed URLs, so this is a thin pass-through -
    returns how many URLs were actually newly added to the queue.
    """
    added = 0
    for url in urls:
        before = len(fetching_service.url_queue)
        fetching_service.queue_url(url)
        if len(fetching_service.url_queue) > before:
            added += 1
    return added


def queue_extracted_links(links: Iterable[str]) -> int:
    """
    Feed URLs pulled from an already-crawled LIST-category page (the second
    element extraction_service.extract_information() returns, sourced from
    cleaning_service.extract_links()) back into the crawl queue.
    extract_links() already filters out non-crawlable schemes, so no extra
    filtering happens here - this just gives the "known directory site"
    discovery path its own named entry point, separate from search-based
    discovery, for clarity at the call site.
    """
    return queue_discovered_urls(links)