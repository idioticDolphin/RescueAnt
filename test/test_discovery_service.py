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
    assert discovery_service._is_valid_crawl_url("http://example.com/") is True
    assert discovery_service._is_valid_crawl_url("https://example.com/") is True


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
    provider = _FakeProvider({"q": ["http://ok.com/", "mailto:a@b.com", "not a url"]})

    result = discovery_service.discover_urls(provider, ["q"], results_per_query=10, politeness=0)

    assert result == ["http://ok.com/"]


def test_discover_urls_deduplicates_and_sorts_across_queries():
    provider = _FakeProvider({
        "q1": ["http://b.com/", "http://a.com/"],
        "q2": ["http://a.com/", "http://c.com/"],
    })

    result = discovery_service.discover_urls(provider, ["q1", "q2"], results_per_query=10, politeness=0)

    assert result == ["http://a.com/", "http://b.com/", "http://c.com/"]


def test_discover_urls_skips_failing_query_without_aborting_batch():
    provider = _FakeProvider(
        {"good": ["http://ok.com/"]},
        raise_for={"bad"},
    )

    result = discovery_service.discover_urls(provider, ["bad", "good"], results_per_query=10, politeness=0)

    assert result == ["http://ok.com/"]
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
    added = discovery_service.queue_discovered_urls(["http://a.com/", "http://b.com/"])

    assert added == 2
    assert fetching_service.url_queue == ["http://a.com/", "http://b.com/"]


def test_queue_discovered_urls_does_not_count_duplicates():
    fetching_service.processed_urls["http://already-done.com/"] = "<html></html>"

    added = discovery_service.queue_discovered_urls(["http://a.com/", "http://a.com/", "http://already-done.com/"])

    assert added == 1
    assert fetching_service.url_queue == ["http://a.com/"]


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
    assert discovery_service.read_query_templates.__defaults__[0] ==         discovery_service.config.get_search_query_path()


def test_query_blocks_keep_their_own_locations(tmp_path):
    # Without blocks every template meets every location, and a French
    # template is paired with German cities.
    query_file = tmp_path / "queries.csv"
    query_file.write_text(
        "location: Marburg\n"
        "Tierheim in {location}\n"
        "---\n"
        "location: Lyon\n"
        "centre de sauvegarde {location}\n",
        encoding="utf-8")

    result = discovery_service.read_query_templates(str(query_file))

    assert result == ["Tierheim in Marburg", "centre de sauvegarde Lyon"]


def test_query_files_are_read_as_utf8(tmp_path):
    query_file = tmp_path / "queries.csv"
    query_file.write_text("location: München\nWildtierhilfe\n", encoding="utf-8")

    assert discovery_service.read_query_templates(str(query_file)) == ["Wildtierhilfe München"]


def test_query_order_is_file_order_by_default(tmp_path):
    query_file = tmp_path / "queries.csv"
    query_file.write_text(
        "location: Marburg\nlocation: Berlin\nTierheim in {location}\n"
        "---\n"
        "location: Lyon\ncentre de sauvegarde {location}\n",
        encoding="utf-8")

    result = discovery_service.read_query_templates(str(query_file), order="file")

    assert result == ["Tierheim in Marburg", "Tierheim in Berlin",
                      "centre de sauvegarde Lyon"]


def test_interleaved_order_takes_one_query_per_block_in_turn(tmp_path):
    # Discovery consumes queries from the front of the list, a handful per
    # turn. In file order a run reaches the second language only after every
    # German query has been spent - which, with a world-wide query file, is
    # never.
    query_file = tmp_path / "queries.csv"
    query_file.write_text(
        "location: Marburg\nlocation: Berlin\nTierheim in {location}\n"
        "---\n"
        "location: Lyon\ncentre de sauvegarde {location}\n",
        encoding="utf-8")

    result = discovery_service.read_query_templates(str(query_file), order="interleave")

    assert result == ["Tierheim in Marburg", "centre de sauvegarde Lyon",
                      "Tierheim in Berlin"]


def test_interleaved_order_keeps_every_query(tmp_path):
    query_file = tmp_path / "queries.csv"
    query_file.write_text(
        "location: A\nlocation: B\nlocation: C\nx {location}\ny {location}\n"
        "---\n"
        "location: D\nz {location}\n",
        encoding="utf-8")

    interleaved = discovery_service.read_query_templates(str(query_file), order="interleave")

    assert sorted(interleaved) == sorted(
        discovery_service.read_query_templates(str(query_file), order="file"))


def test_query_order_defaults_to_the_configured_one(monkeypatch, tmp_path):
    query_file = tmp_path / "queries.csv"
    query_file.write_text(
        "location: Marburg\nTierheim in {location}\n"
        "---\n"
        "location: Lyon\ncentre de sauvegarde {location}\n",
        encoding="utf-8")
    monkeypatch.setattr(discovery_service.config, "discovery_query_order", "interleave")

    assert discovery_service.read_query_templates(str(query_file))[1] == \
        "centre de sauvegarde Lyon"


# ---------------------------------------------------------------------------
# block directives: placeholder sets, search parameters, weight
# ---------------------------------------------------------------------------

def test_a_declared_set_expands_its_placeholder(tmp_path):
    query_file = tmp_path / "queries.csv"
    query_file.write_text(
        "set animal = Igel, Fledermaus\n"
        "location: Bayern\n"
        "{animal}station {location}\n",
        encoding="utf-8")

    assert discovery_service.read_query_templates(str(query_file)) == [
        "Igelstation Bayern", "Fledermausstation Bayern"]


def test_a_set_belongs_to_its_own_block(tmp_path):
    query_file = tmp_path / "queries.csv"
    query_file.write_text(
        "set animal = Igel\nlocation: Bayern\n{animal}station {location}\n"
        "---\n"
        "location: France\ncentre de soins {location}\n",
        encoding="utf-8")

    assert discovery_service.read_query_templates(str(query_file)) == [
        "Igelstation Bayern", "centre de soins France"]


def test_a_template_naming_no_set_is_left_alone(tmp_path):
    query_file = tmp_path / "queries.csv"
    query_file.write_text(
        "set animal = Igel, Fledermaus\nlocation: Bayern\nWildtierhilfe {location}\n",
        encoding="utf-8")

    assert discovery_service.read_query_templates(str(query_file)) == ["Wildtierhilfe Bayern"]


def test_block_language_rides_along_with_every_query_of_that_block(tmp_path):
    query_file = tmp_path / "queries.csv"
    query_file.write_text(
        "language: de\nlocation: Bayern\nWildtierhilfe {location}\n"
        "---\n"
        "language: ja\nlocation: 北海道\n野生動物保護センター {location}\n",
        encoding="utf-8")

    german, japanese = discovery_service.read_query_templates(str(query_file), order="file")

    assert german.params == {"language": "de"}
    assert japanese.params == {"language": "ja"}


def test_extra_search_params_are_parsed_and_merged_with_the_language(tmp_path):
    query_file = tmp_path / "queries.csv"
    query_file.write_text(
        "language: de\nparams: safesearch=0, time_range=year\n"
        "location: Bayern\nWildtierhilfe {location}\n",
        encoding="utf-8")

    query, = discovery_service.read_query_templates(str(query_file))

    assert query.params == {"language": "de", "safesearch": "0", "time_range": "year"}


def test_queries_without_directives_carry_no_params(tmp_path):
    query_file = tmp_path / "queries.csv"
    query_file.write_text("location: Bayern\nWildtierhilfe {location}\n", encoding="utf-8")

    assert discovery_service.read_query_templates(str(query_file))[0].params == {}


def test_a_query_is_still_a_plain_string(tmp_path):
    query_file = tmp_path / "queries.csv"
    query_file.write_text("language: de\nlocation: Bayern\nWildtierhilfe {location}\n",
                          encoding="utf-8")

    assert discovery_service.read_query_templates(str(query_file)) == ["Wildtierhilfe Bayern"]


def test_block_weight_takes_that_many_queries_per_interleaved_turn(tmp_path):
    # The deployment's own language is worth asking about more often than the
    # thirty-sixth language in the file.
    query_file = tmp_path / "queries.csv"
    query_file.write_text(
        "weight: 2\nlocation: A\nlocation: B\nlocation: C\nx {location}\n"
        "---\n"
        "location: D\nlocation: E\ny {location}\n",
        encoding="utf-8")

    assert discovery_service.read_query_templates(str(query_file), order="interleave") == [
        "x A", "x B", "y D", "x C", "y E"]


def test_search_params_reach_the_provider(monkeypatch):
    seen = {}

    class Provider:
        def search(self, query, max_results, params=None):
            seen[query] = params
            return ["http://found.example/"]

    discovery_service.discover_urls(
        Provider(), [discovery_service.Query("Wildtierhilfe Bayern", {"language": "de"})],
        results_per_query=3, politeness=0)

    assert seen == {"Wildtierhilfe Bayern": {"language": "de"}}


def test_a_provider_without_params_support_still_works(monkeypatch):
    class OldProvider:
        def search(self, query, max_results):
            return ["http://found.example/"]

    assert discovery_service.discover_urls(
        OldProvider(), ["Wildtierhilfe Bayern"], results_per_query=3,
        politeness=0) == ["http://found.example/"]
