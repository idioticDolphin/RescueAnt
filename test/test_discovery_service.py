import pytest

import model.crawler.discovery_service as discovery_service
import model.crawler.fetching_service as fetching_service


@pytest.fixture(autouse=True)
def _isolate_fetching_service_state(monkeypatch):
    monkeypatch.setattr(fetching_service, "url_queue", [])
    monkeypatch.setattr(fetching_service, "processed_urls", {})


# ---------------------------------------------------------------------------
# generate_queries
# ---------------------------------------------------------------------------

def test_generate_queries_substitutes_location_placeholder():
    queries = discovery_service.generate_queries(["Tierheim in {location}"], ["Marburg"])
    assert queries == ["Tierheim in Marburg"]


def test_generate_queries_appends_location_when_no_placeholder():
    queries = discovery_service.generate_queries(["Wildtierhilfe"], ["Marburg"])
    assert queries == ["Wildtierhilfe Marburg"]


def test_generate_queries_combines_every_template_with_every_location():
    queries = discovery_service.generate_queries(["A", "B"], ["X", "Y"])
    assert queries == ["A X", "A Y", "B X", "B Y"]


def test_generate_queries_empty_locations_produces_no_queries():
    assert discovery_service.generate_queries(["A"], []) == []


# ---------------------------------------------------------------------------
# _is_valid_crawl_url
# ---------------------------------------------------------------------------

def test_is_valid_crawl_url_accepts_http_and_https():
    assert discovery_service._is_valid_crawl_url("http://example.com") is True
    assert discovery_service._is_valid_crawl_url("https://example.com") is True


def test_is_valid_crawl_url_rejects_other_schemes():
    assert discovery_service._is_valid_crawl_url("mailto:info@example.com") is False
    assert discovery_service._is_valid_crawl_url("ftp://example.com") is False


def test_is_valid_crawl_url_rejects_missing_netloc():
    assert discovery_service._is_valid_crawl_url("http://") is False


# ---------------------------------------------------------------------------
# discover_urls
# ---------------------------------------------------------------------------

class _FakeProvider:
    def __init__(self, results_by_query=None, raise_for=None):
        self.results_by_query = results_by_query or {}
        self.raise_for = raise_for or set()
        self.queries_seen = []

    def search(self, query, max_results):
        self.queries_seen.append(query)
        if query in self.raise_for:
            raise RuntimeError("search API failed")
        return self.results_by_query.get(query, [])


def test_discover_urls_filters_invalid_urls():
    provider = _FakeProvider({"q": ["http://ok.com", "mailto:a@b.com", "not a url"]})

    result = discovery_service.discover_urls(provider, ["q"], results_per_query=10, politeness=0)

    assert result == ["http://ok.com"]


def test_discover_urls_deduplicates_and_sorts_across_queries():
    provider = _FakeProvider({
        "q1": ["http://b.com", "http://a.com"],
        "q2": ["http://a.com", "http://c.com"],
    })

    result = discovery_service.discover_urls(provider, ["q1", "q2"], results_per_query=10, politeness=0)

    assert result == ["http://a.com", "http://b.com", "http://c.com"]


def test_discover_urls_skips_failing_query_without_aborting_batch():
    provider = _FakeProvider(
        {"good": ["http://ok.com"]},
        raise_for={"bad"},
    )

    result = discovery_service.discover_urls(provider, ["bad", "good"], results_per_query=10, politeness=0)

    assert result == ["http://ok.com"]
    assert provider.queries_seen == ["bad", "good"]


def test_discover_urls_sleeps_between_queries_but_not_after_last(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(discovery_service.time, "sleep", lambda s: sleep_calls.append(s))
    provider = _FakeProvider({"q1": [], "q2": [], "q3": []})

    discovery_service.discover_urls(provider, ["q1", "q2", "q3"], results_per_query=10, politeness=2.5)

    assert sleep_calls == [2.5, 2.5]


# ---------------------------------------------------------------------------
# queue_discovered_urls
# ---------------------------------------------------------------------------

def test_queue_discovered_urls_adds_new_urls_to_fetching_queue():
    added = discovery_service.queue_discovered_urls(["http://a.com", "http://b.com"])

    assert added == 2
    assert fetching_service.url_queue == ["http://a.com", "http://b.com"]


def test_queue_discovered_urls_does_not_count_duplicates():
    fetching_service.processed_urls["http://already-done.com"] = "<html></html>"

    added = discovery_service.queue_discovered_urls(["http://a.com", "http://a.com", "http://already-done.com"])

    assert added == 1
    assert fetching_service.url_queue == ["http://a.com"]


# ---------------------------------------------------------------------------
# read_query_templates
# ---------------------------------------------------------------------------

def test_read_query_templates_combines_templates_and_locations(tmp_path):
    query_file = tmp_path / "queries.csv"
    query_file.write_text(
        "location: Marburg\n"
        "location: Berlin\n"
        "Tierheim in {location}\n"
        "Wildtierhilfe\n"
    )

    result = discovery_service.read_query_templates(str(query_file))

    assert result == [
        "Tierheim in Marburg",
        "Tierheim in Berlin",
        "Wildtierhilfe Marburg",
        "Wildtierhilfe Berlin",
    ]


def test_read_query_templates_ignores_blank_lines_and_comments(tmp_path):
    query_file = tmp_path / "queries.csv"
    query_file.write_text(
        "# a comment\n"
        "\n"
        "location: Marburg\n"
        "\n"
        "# another comment\n"
        "Tierheim in {location}\n"
    )

    result = discovery_service.read_query_templates(str(query_file))

    assert result == ["Tierheim in Marburg"]


def test_read_query_templates_location_prefix_is_case_insensitive(tmp_path):
    query_file = tmp_path / "queries.csv"
    query_file.write_text(
        "LOCATION: Marburg\n"
        "Tierheim in {location}\n"
    )

    result = discovery_service.read_query_templates(str(query_file))

    assert result == ["Tierheim in Marburg"]


def test_read_query_templates_with_no_locations_produces_no_queries(tmp_path):
    query_file = tmp_path / "queries.csv"
    query_file.write_text("Tierheim in {location}\n")

    assert discovery_service.read_query_templates(str(query_file)) == []


def test_read_query_templates_default_path_comes_from_config():
    assert discovery_service.read_query_templates.__defaults__ == (
        discovery_service.config.get_search_query_path(),
    )
