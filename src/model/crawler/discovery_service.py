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
import logging
import time
from itertools import zip_longest
from typing import Iterable
from urllib.parse import urlparse
from model.objects.searchprovider import *

import model.crawler.fetching_service as fetching_service
import model.tools.config_service as config_service

logger = logging.getLogger(__name__)

CRAWLABLE_SCHEMES = {"http", "https"}
config = config_service.get_config()


class Query(str):
    """
    A search query, plus the parameters its block says to send with it.

    It is a string, so everything that logs, compares or hands a query to a
    provider keeps working; `params` rides along for providers that can use
    it - `language=ja` on a Japanese block, so a Japanese query is answered
    with Japanese pages instead of whatever else matches the characters.
    """

    def __new__(cls, text: str, params: dict | None = None, block: str = ""):
        query = super().__new__(cls, text)
        query.params = dict(params or {})
        query.block = block
        return query


def generate_queries(keyword_templates: Iterable[str], locations: Iterable[str],
                     value_sets: dict[str, list[str]] | None = None,
                     params: dict | None = None, block: str = "") -> list[Query]:
    """
    Combine keyword templates with locations to build a list of search
    queries. A template containing "{location}" gets that substituted in
    directly (e.g. "Tierheim in {location}" -> "Tierheim in Marburg");
    a plain keyword with no placeholder is just appended with a space
    (e.g. "Wildtierhilfe" -> "Wildtierhilfe Marburg").

    :param value_sets: further placeholders declared by the block, as
                       {"animal": ["Igel", "Fledermaus"]}. A template naming
                       one is written out once per value, so a file can say
                       "{animal}station {location}" instead of listing the
                       product by hand. Templates naming none are unaffected.
    """
    queries = []
    for template in keyword_templates:
        for variant in _expand_value_sets(template, value_sets or {}):
            for location in locations:
                text = (variant.replace("{location}", location) if "{location}" in variant
                        else f"{variant} {location}")
                queries.append(Query(text, params, block))
    return queries


def _expand_value_sets(template: str, value_sets: dict[str, list[str]]) -> list[str]:
    """Write a template out once per combination of the sets it names."""
    variants = [template]
    for name, values in value_sets.items():
        placeholder = "{" + name + "}"
        if not any(placeholder in variant for variant in variants):
            continue
        variants = [variant.replace(placeholder, value)
                    for variant in variants for value in values]
    return variants


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
            params = getattr(query, "params", None)
            # Only providers that accept per-query parameters are given them,
            # so a provider written before blocks carried a language still works.
            results = (provider.search(query, results_per_query, params=params) if params
                       else provider.search(query, results_per_query))
        except Exception as e:
            logger.warning("Search failed for query %r: %r", query, e)
            results = []

        for url in results:
            if _is_valid_crawl_url(url):
                discovered.add(url)

        if i < len(queries) - 1:
            time.sleep(politeness)

    logger.info("Discovery: %d quer%s -> %d unique URL%s",
                len(queries), "y" if len(queries) == 1 else "ies",
                len(discovered), "" if len(discovered) == 1 else "s")
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
        # Search hits answer the configured queries directly, so they are the
        # most promising thing in the frontier - not just another link.
        fetching_service.queue_url(
            url, priority=config_service.get_config().discovery_priority)
        if len(fetching_service.url_queue) > before:
            added += 1
    return added

def read_query_templates(path: str = config.get_search_query_path(),
                         order: str | None = None) -> list[str]:
    """
    Read keyword templates and locations from a query-template file and
    combine them into a list of search queries via generate_queries().

    File format - one entry per line:
      - lines starting with "location:" declare a location (e.g. "location: Marburg")
      - a line of three or more dashes starts a new block: the templates in a
        block are combined only with that block's locations, so a French
        template is not sent out against German cities
      - "language: ja" gives the block's queries a language parameter, passed
        to the search provider so a Japanese query is answered with Japanese
        pages; "params: safesearch=0, time_range=year" adds any others
      - "weight: 2" takes that many of the block's queries per interleaved
        turn, for a language worth asking about more often than the rest
      - "set animal = Igel, Fledermaus" declares a placeholder the block's
        templates can use, written out once per value (see generate_queries())
      - blank lines and lines starting with "#" are ignored
      - every other non-empty line is a keyword template, optionally
        containing "{location}" as a placeholder (see generate_queries())

    :param order: "file" keeps the file's own order; "interleave" takes one
                  query from each block in turn (or `weight` of them).
                  run_discovery() consumes a handful of queries per turn from
                  the front of the list, so in file order a query file
                  covering many languages spends its whole run inside the
                  first block. Defaults to config.discovery_query_order.
    """
    blocks: list[list[Query]] = []
    weights: list[int] = []
    templates, locations = [], []
    directives: dict[str, str] = {}
    value_sets: dict[str, list[str]] = {}

    def flush():
        if templates and locations:
            params = {}
            if directives.get("language"):
                params["language"] = directives["language"]
            params.update(_parse_params(directives.get("params", "")))
            blocks.append(generate_queries(templates, locations, value_sets, params,
                                           directives.get("language", "")))
            weights.append(max(1, int(directives.get("weight", 1) or 1)))
        templates.clear()
        locations.clear()
        directives.clear()
        value_sets.clear()

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if set(line) == {"-"} and len(line) >= 3:
                flush()
            elif line.lower().startswith("location:"):
                locations.append(line.split(":", 1)[1].strip())
            elif line.lower().startswith("set "):
                name, _, values = line[4:].partition("=")
                value_sets[name.strip()] = [v.strip() for v in values.split(",") if v.strip()]
            elif _directive(line):
                key, value = _directive(line)
                directives[key] = value
            else:
                templates.append(line)
    flush()

    if order is None:
        order = getattr(config_service.get_config(), "discovery_query_order", "file")
    if order == "interleave":
        return _interleave(blocks, weights)
    return [query for block in blocks for query in block]


BLOCK_DIRECTIVES = ("language", "params", "weight")


def _directive(line: str):
    """("language", "ja") for a block directive line, None for a template."""
    key, separator, value = line.partition(":")
    if separator and key.strip().lower() in BLOCK_DIRECTIVES:
        return key.strip().lower(), value.strip()
    return None


def _parse_params(text: str) -> dict:
    """"safesearch=0, time_range=year" -> {"safesearch": "0", "time_range": "year"}"""
    params = {}
    for pair in text.split(","):
        key, separator, value = pair.partition("=")
        if separator and key.strip():
            params[key.strip()] = value.strip()
    return params


def _interleave(blocks: list[list[Query]], weights: list[int]) -> list[Query]:
    """Take `weight` queries from each block per turn until every block is spent."""
    queries, cursors = [], [0] * len(blocks)
    while any(cursor < len(block) for cursor, block in zip(cursors, blocks)):
        for index, block in enumerate(blocks):
            take = block[cursors[index]:cursors[index] + weights[index]]
            cursors[index] += len(take)
            queries.extend(take)
    return queries