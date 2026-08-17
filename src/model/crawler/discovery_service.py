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
import model.tools.config_service as config_service

CRAWLABLE_SCHEMES = {"http", "https"}
config = config_service.get_config()


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

def read_query_templates(path: str = config.get_search_query_path()) -> list[str]:
    """
    Read keyword templates and locations from a query-template file and
    combine them into a list of search queries via generate_queries().

    File format - one entry per line:
      - lines starting with "location:" declare a location (e.g. "location: Marburg")
      - blank lines and lines starting with "#" are ignored
      - every other non-empty line is a keyword template, optionally
        containing "{location}" as a placeholder (see generate_queries())
    """
    templates = []
    locations = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("location:"):
                locations.append(line.split(":", 1)[1].strip())
            else:
                templates.append(line)
    return generate_queries(templates, locations)