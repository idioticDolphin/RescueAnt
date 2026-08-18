from __future__ import annotations
import abc
import requests
from typing_extensions import Any
from pydantic import GetCoreSchemaHandler, TypeAdapter
from pydantic_core import CoreSchema, core_schema

class SearchProvider(abc.ABC):
    """Common interface for anything that can turn a text query into result URLs."""

    @abc.abstractmethod
    def search(self, query: str, max_results: int) -> list[str]:
        """Return up to max_results URLs for the given query."""
        raise NotImplementedError

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        return core_schema.nullable_schema(
            core_schema.is_instance_schema(cls)
        )


class GoogleCustomSearchProvider(SearchProvider):
    """
    Uses Google's Programmable Search Engine (Custom Search JSON API).
    Free tier: 100 queries/day, but as of 2026 no longer supports
    unrestricted whole-web search - only sites you explicitly list. For
    open-ended discovery, use ConfigurableJsonSearchProvider against a
    self-hosted SearXNG instance instead (see the README's "Search engines"
    section). To use this provider anyway:
      1. Create an API key: https://developers.google.com/custom-search/v1/introduction
      2. Create a Search Engine and get its ID (cx): https://programmablesearchengine.google.com/
    """
    BASE_URL = "https://www.googleapis.com/customsearch/v1"

    def __init__(self, api_key: str, search_engine_id: str, timeout: float = 10.0):
        self.api_key = api_key
        self.search_engine_id = search_engine_id
        self.timeout = timeout

    def search(self, query: str, max_results: int) -> list[str]:
        """Return up to max_results result URLs for query, paging through the API as needed."""
        # Google's API caps each request at 10 results; page via `start` for more.
        urls = []
        start = 1
        while len(urls) < max_results:
            batch_size = min(10, max_results - len(urls))
            params = {
                "key": self.api_key,
                "cx": self.search_engine_id,
                "q": query,
                "num": batch_size,
                "start": start,
            }
            response = requests.get(self.BASE_URL, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            items = data.get("items", [])
            if not items:
                break
            urls.extend(item["link"] for item in items if "link" in item)
            if len(items) < batch_size:
                break  # search engine ran out of results
            start += batch_size
        return urls[:max_results]

class ConfigurableJsonSearchProvider(SearchProvider):
    """
    Generic fallback for any REST/JSON search API that doesn't have a
    dedicated provider class above - Bing Web Search, SerpApi, a
    self-hosted SearXNG instance, etc. You configure how to build the
    request and how to pull result URLs out of the response, instead of
    writing a new class per API.

    Example - a self-hosted SearXNG instance (no API key needed at all):
        ConfigurableJsonSearchProvider(
            base_url="http://localhost:8080/search",
            query_param="q",
            result_path=["results"],
            url_field="url",
            extra_params={"format": "json"},
        )

    Example - SerpApi:
        ConfigurableJsonSearchProvider(
            base_url="https://serpapi.com/search",
            query_param="q",
            result_path=["organic_results"],
            url_field="link",
            extra_params={"api_key": "...", "engine": "google"},
        )
    """

    def __init__(
        self,
        base_url: str,
        query_param: str,
        result_path: list[str],
        url_field: str,
        extra_params: dict | None = None,
        headers: dict | None = None,
        timeout: float = 10.0,
    ):
        self.base_url = base_url
        self.query_param = query_param
        self.result_path = result_path
        self.url_field = url_field
        self.extra_params = extra_params or {}
        self.headers = headers or {}
        self.timeout = timeout

    def search(self, query: str, max_results: int) -> list[str]:
        """Return up to max_results result URLs for query, walking result_path into the JSON response."""
        params = {self.query_param: query, **self.extra_params}
        response = requests.get(self.base_url, params=params, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()

        results = data
        for key in self.result_path:
            results = results.get(key, []) if isinstance(results, dict) else []
        if not isinstance(results, list):
            return []

        urls = []
        for item in results[:max_results]:
            url = item.get(self.url_field) if isinstance(item, dict) else None
            if url:
                urls.append(url)
        return urls