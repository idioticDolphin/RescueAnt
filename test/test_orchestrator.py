import itertools
import time
from unittest.mock import MagicMock

import pytest

import model.orchestrator as orchestrator
import model.crawler.fetching_service as fetching_service
import model.tools.config_service as config_service
from conftest import make_fake_category
from model.objects.category import Category, Relevancy


@pytest.fixture(autouse=True)
def _isolate_fetching_service_state(monkeypatch):
    monkeypatch.setattr(fetching_service, "url_queue", [])
    monkeypatch.setattr(fetching_service, "processed_urls", {})
    monkeypatch.setattr(fetching_service, "_last_request_time", {})
    monkeypatch.setattr(orchestrator, "discovery_queries", [])
    # Real monitor_service writes files to disk (sessions/) - keep tests from
    # touching the filesystem unless a test explicitly wants the real thing.
    monkeypatch.setattr(orchestrator, "monitor_service", MagicMock())


def _fake_parse_queue_returning(html_by_url):
    async def fake_parse_queue():
        for url in list(fetching_service.url_queue):
            fetching_service.processed_urls[url] = html_by_url.get(url, "")
        fetching_service.url_queue = []
    return fake_parse_queue


def _make_category(name="STATION", is_list_category=False):
    return Category(
        name=name,
        relevancy=Relevancy.CONTENT,
        analysis_prompt="p",
        analysis_max_tokens=1,
        analysis_model_id=0,
        process_links=True,
        is_list_category=is_list_category,
        fields={"type": "object", "properties": {}, "required": []},
    )


def _make_links_only_category(name="HUB"):
    return Category(name=name, relevancy=Relevancy.LINKS, process_links=True)


def _patch_data_service(monkeypatch):
    data_service = MagicMock()
    data_service.save_crawl_instance.side_effect = range(1, 1000)
    data_service.count_extracted_pages_for_site.return_value = 0
    monkeypatch.setattr(orchestrator, "data_service", data_service)
    return data_service


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def _make_orchestrator_config(**overrides):
    config = MagicMock()
    config.get_starting_url_path.return_value = "starting_urls.csv"
    config.get_search_query_path.return_value = "search_queries.csv"
    config.redo_failed_fetches = True
    config.redo_all_fetches = False
    config.discover_urls = False
    config.discovery_batch_size = 5
    config.results_per_query = 10
    config.query_politeness = 1.0
    config.max_discovery_batches = 0
    config.max_rounds = 0
    config.max_runtime_seconds = 0
    config.max_batch_size = 0
    config.max_extractions_per_site = 0
    config.discovery_priority = 50.0
    config.discovery_when_below = None
    config.referrer_weights = {}
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_init_wires_config_into_data_service_and_fetching_service(monkeypatch):
    config = _make_orchestrator_config()
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    data_service = MagicMock()
    monkeypatch.setattr(orchestrator, "data_service", data_service)
    fetching_service_mock = MagicMock()
    monkeypatch.setattr(orchestrator, "fetching_service", fetching_service_mock)

    orchestrator.init()

    data_service.init_db.assert_called_once()
    fetching_service_mock.init.assert_called_once_with(
        starting_url_path="starting_urls.csv",
        redo_failed_fetches=True,
        redo_all_fetches=False,
    )


def test_init_stores_no_discovery_queries_when_discovery_disabled(monkeypatch):
    config = _make_orchestrator_config(discover_urls=False)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(orchestrator, "data_service", MagicMock())
    monkeypatch.setattr(orchestrator, "fetching_service", MagicMock())
    discovery_service = MagicMock()
    monkeypatch.setattr(orchestrator, "discovery_service", discovery_service)

    orchestrator.init()

    discovery_service.read_query_templates.assert_not_called()
    assert orchestrator.discovery_queries == []


def test_init_generates_and_stores_discovery_queries_when_enabled(monkeypatch):
    config = _make_orchestrator_config(discover_urls=True)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(orchestrator, "data_service", MagicMock())
    monkeypatch.setattr(orchestrator, "fetching_service", MagicMock())
    discovery_service = MagicMock()
    discovery_service.read_query_templates.return_value = ["Tierheim in Marburg", "Wildtierhilfe Berlin"]
    monkeypatch.setattr(orchestrator, "discovery_service", discovery_service)

    orchestrator.init()

    discovery_service.read_query_templates.assert_called_once_with("search_queries.csv")
    assert orchestrator.discovery_queries == ["Tierheim in Marburg", "Wildtierhilfe Berlin"]


# ---------------------------------------------------------------------------
# run_discovery
# ---------------------------------------------------------------------------

def test_run_discovery_returns_zero_when_discovery_disabled(monkeypatch):
    config = _make_orchestrator_config(discover_urls=False)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    discovery_service = MagicMock()
    monkeypatch.setattr(orchestrator, "discovery_service", discovery_service)
    monkeypatch.setattr(orchestrator, "discovery_queries", ["some query"])

    assert orchestrator.run_discovery() == 0
    discovery_service.discover_urls.assert_not_called()


def test_run_discovery_returns_zero_when_no_queries_stored(monkeypatch):
    config = _make_orchestrator_config(discover_urls=True)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    discovery_service = MagicMock()
    monkeypatch.setattr(orchestrator, "discovery_service", discovery_service)
    monkeypatch.setattr(orchestrator, "discovery_queries", [])

    assert orchestrator.run_discovery() == 0
    discovery_service.discover_urls.assert_not_called()


def test_run_discovery_consumes_only_batch_size_queries(monkeypatch):
    config = _make_orchestrator_config(discover_urls=True, discovery_batch_size=2)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    discovery_service = MagicMock()
    discovery_service.discover_urls.return_value = []
    discovery_service.queue_discovered_urls.return_value = 0
    monkeypatch.setattr(orchestrator, "discovery_service", discovery_service)
    monkeypatch.setattr(orchestrator, "discovery_queries", ["q1", "q2", "q3", "q4", "q5"])

    orchestrator.run_discovery()

    discovery_service.discover_urls.assert_called_once_with(
        config.get_search_provider(), ["q1", "q2"],
        results_per_query=10, politeness=1.0,
    )
    assert orchestrator.discovery_queries == ["q3", "q4", "q5"]


def test_run_discovery_consumes_remaining_queries_if_fewer_than_batch_size(monkeypatch):
    config = _make_orchestrator_config(discover_urls=True, discovery_batch_size=5)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    discovery_service = MagicMock()
    discovery_service.discover_urls.return_value = []
    discovery_service.queue_discovered_urls.return_value = 0
    monkeypatch.setattr(orchestrator, "discovery_service", discovery_service)
    monkeypatch.setattr(orchestrator, "discovery_queries", ["q1", "q2"])

    orchestrator.run_discovery()

    discovery_service.discover_urls.assert_called_once_with(
        config.get_search_provider(), ["q1", "q2"],
        results_per_query=10, politeness=1.0,
    )
    assert orchestrator.discovery_queries == []


def test_run_discovery_returns_number_of_newly_queued_urls(monkeypatch):
    config = _make_orchestrator_config(discover_urls=True, discovery_batch_size=5)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    discovery_service = MagicMock()
    discovery_service.discover_urls.return_value = ["http://a.com/", "http://b.com/"]
    discovery_service.queue_discovered_urls.return_value = 2
    monkeypatch.setattr(orchestrator, "discovery_service", discovery_service)
    monkeypatch.setattr(orchestrator, "discovery_queries", ["q1"])

    added = orchestrator.run_discovery()

    discovery_service.queue_discovered_urls.assert_called_once_with(["http://a.com/", "http://b.com/"])
    assert added == 2


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def test_run_calls_init_first(monkeypatch):
    config = _make_orchestrator_config()
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    call_order = []
    monkeypatch.setattr(orchestrator, "init", lambda: call_order.append("init"))
    monkeypatch.setattr(orchestrator, "run_discovery", lambda: call_order.append("run_discovery"))
    fetching_service.url_queue = []

    orchestrator.run()

    assert call_order == ["init"]


def test_run_processes_initial_queue_completely_before_running_discovery(monkeypatch):
    """
    The full initial queue (starting URLs plus whatever process_batch's own
    extraction discovers along the way) must be drained before discovery is
    ever consulted - discovery is a last resort, not an upfront bulk-add.
    """
    config = _make_orchestrator_config()
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(orchestrator, "init", lambda: None)
    monkeypatch.setattr(orchestrator, "discovery_queries", ["some query"])

    call_order = []
    fetching_service.url_queue = ["http://a.com/", "http://b.com/"]

    def fake_process_batch():
        call_order.append("process_batch")
        fetching_service.url_queue.pop()

    def fake_run_discovery():
        call_order.append("run_discovery")
        orchestrator.discovery_queries.clear()  # this batch used up the only query, found nothing

    monkeypatch.setattr(orchestrator, "process_batch", fake_process_batch)
    monkeypatch.setattr(orchestrator, "run_discovery", fake_run_discovery)

    orchestrator.run()

    assert call_order == ["process_batch", "process_batch", "run_discovery"]


def test_run_retries_next_discovery_batch_if_previous_one_found_nothing(monkeypatch):
    config = _make_orchestrator_config()
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(orchestrator, "init", lambda: None)
    monkeypatch.setattr(orchestrator, "discovery_queries", ["q1", "q2", "q3"])
    fetching_service.url_queue = []

    discovery_calls = []

    def fake_run_discovery():
        discovery_calls.append(True)
        orchestrator.discovery_queries.pop(0)  # simulates discovery_batch_size=1, no urls found

    monkeypatch.setattr(orchestrator, "run_discovery", fake_run_discovery)
    monkeypatch.setattr(orchestrator, "process_batch", MagicMock())

    orchestrator.run()

    assert len(discovery_calls) == 3
    assert orchestrator.discovery_queries == []
    orchestrator.process_batch.assert_not_called()


def test_run_resumes_processing_once_discovery_finds_something(monkeypatch):
    config = _make_orchestrator_config()
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(orchestrator, "init", lambda: None)
    monkeypatch.setattr(orchestrator, "discovery_queries", ["q1"])
    fetching_service.url_queue = []

    call_order = []

    def fake_run_discovery():
        call_order.append("run_discovery")
        orchestrator.discovery_queries.clear()
        fetching_service.url_queue.append("http://discovered.com/")

    def fake_process_batch():
        call_order.append("process_batch")
        fetching_service.url_queue.pop()

    monkeypatch.setattr(orchestrator, "run_discovery", fake_run_discovery)
    monkeypatch.setattr(orchestrator, "process_batch", fake_process_batch)

    orchestrator.run()

    assert call_order == ["run_discovery", "process_batch"]


def test_run_stops_on_runtime_limit_reached_right_after_a_discovery_batch(monkeypatch):
    # queue stays empty and queries never run out - only the runtime limit,
    # checked immediately after each discovery batch, should stop this.
    config = _make_orchestrator_config(max_runtime_seconds=0.01)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(orchestrator, "init", lambda: None)
    monkeypatch.setattr(orchestrator, "discovery_queries", ["q1", "q2", "q3"])
    fetching_service.url_queue = []

    discovery_calls = []

    def fake_run_discovery():
        discovery_calls.append(True)
        time.sleep(0.02)

    monkeypatch.setattr(orchestrator, "run_discovery", fake_run_discovery)
    monkeypatch.setattr(orchestrator, "process_batch", MagicMock())

    orchestrator.run()

    assert len(discovery_calls) == 1
    orchestrator.process_batch.assert_not_called()


def test_run_stops_after_max_discovery_batches(monkeypatch):
    config = _make_orchestrator_config(max_discovery_batches=2)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(orchestrator, "init", lambda: None)
    # never runs out and never finds anything - only max_discovery_batches should stop this
    monkeypatch.setattr(orchestrator, "discovery_queries", ["q1", "q2", "q3", "q4", "q5"])
    fetching_service.url_queue = []

    discovery_calls = []
    monkeypatch.setattr(orchestrator, "run_discovery", lambda: discovery_calls.append(True))

    orchestrator.run()

    assert len(discovery_calls) == 2


def test_run_stops_after_max_rounds(monkeypatch):
    config = _make_orchestrator_config(max_rounds=2)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(orchestrator, "init", lambda: None)
    monkeypatch.setattr(orchestrator, "run_discovery", lambda: None)

    fetching_service.url_queue = ["http://never-ending.com/"]
    process_batch_calls = []

    def fake_process_batch():
        process_batch_calls.append(True)
        # queue never actually empties on its own - only max_rounds should stop this

    monkeypatch.setattr(orchestrator, "process_batch", fake_process_batch)

    orchestrator.run()

    assert len(process_batch_calls) == 2


def test_run_stops_after_max_runtime_seconds(monkeypatch):
    config = _make_orchestrator_config(max_runtime_seconds=0.05)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(orchestrator, "init", lambda: None)
    monkeypatch.setattr(orchestrator, "run_discovery", lambda: None)

    fetching_service.url_queue = ["http://never-ending.com/"]
    process_batch_calls = []

    # Drive a fake clock rather than sleeping: Windows' timer granularity is
    # ~15.6ms, so a real sleep(0.06) can return in under the 0.05s budget and
    # make this test flaky.
    # first read is the run's start_time, the next is the post-batch limit
    # check - one simulated second later, well past the 0.05s budget.
    clock = itertools.count(0.0, 1.0)
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: next(clock))

    def fake_process_batch():
        process_batch_calls.append(True)

    monkeypatch.setattr(orchestrator, "process_batch", fake_process_batch)

    orchestrator.run()

    assert len(process_batch_calls) == 1


def test_run_does_nothing_when_queue_stays_empty(monkeypatch):
    config = _make_orchestrator_config()
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(orchestrator, "init", lambda: None)
    monkeypatch.setattr(orchestrator, "run_discovery", lambda: None)
    fetching_service.url_queue = []
    process_batch = MagicMock()
    monkeypatch.setattr(orchestrator, "process_batch", process_batch)

    orchestrator.run()

    process_batch.assert_not_called()


def test_process_batch_saves_crawl_instance_for_every_url(monkeypatch):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com/": "<html>a</html>",
        "http://b.com/": "",
    }))
    data_service = _patch_data_service(monkeypatch)
    category_service = MagicMock()
    category_service.categorize_website.return_value = _make_category("STATION")
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    orchestrator.process_batch(["http://a.com/", "http://b.com/"])

    calls = {c.args[0]: c.args[2] for c in data_service.save_crawl_instance.call_args_list}
    assert calls == {"http://a.com/": True, "http://b.com/": False}


def test_process_batch_only_categorizes_successfully_fetched_sites(monkeypatch):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com/": "<html>a</html>",
        "http://b.com/": "",
    }))
    _patch_data_service(monkeypatch)
    category_service = MagicMock()
    category_service.categorize_website.return_value = Category(name="IRRELEVANT", relevancy=Relevancy.IRRELEVANT)
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    orchestrator.process_batch(["http://a.com/", "http://b.com/"])

    assert category_service.categorize_website.call_count == 1
    category_service.categorize_website.assert_called_with("<html>a</html>", "http://a.com/")


def test_process_batch_saves_extraction_for_single_category(monkeypatch):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com/": "<html>a</html>",
    }))
    data_service = _patch_data_service(monkeypatch)
    category = _make_category("STATION", is_list_category=False)
    category_service = MagicMock()
    category_service.categorize_website.return_value = category
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = ({"name": "Station A"}, [])
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    orchestrator.process_batch(["http://a.com/"])

    data_service.save_extraction.assert_called_once_with(1, {"name": "Station A"})


def test_process_batch_logs_fetch_summary_and_categorization(monkeypatch, caplog):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com/": "<html>a</html>",
        "http://b.com/": "",
    }))
    _patch_data_service(monkeypatch)
    category_service = MagicMock()
    category_service.categorize_website.return_value = _make_category("STATION")
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    with caplog.at_level("INFO", logger="model.orchestrator"):
        orchestrator.process_batch(["http://a.com/", "http://b.com/"])

    assert "Fetched 1/2 URL(s) successfully" in caplog.text
    assert "Categorized http://a.com/ as STATION" in caplog.text


def test_process_batch_logs_extraction_progress_without_dumping_full_content(monkeypatch, caplog):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com/": "<html>a</html>",
    }))
    _patch_data_service(monkeypatch)
    category = _make_category("STATION", is_list_category=False)
    category_service = MagicMock()
    category_service.categorize_website.return_value = category
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    secret_looking_value = "THIS-SHOULD-NOT-APPEAR-AT-INFO-LEVEL"
    extraction_service.extract_information.return_value = ({"name": secret_looking_value}, [])
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    with caplog.at_level("INFO", logger="model.orchestrator"):
        orchestrator.process_batch(["http://a.com/"])

    assert "Extracted 1 field(s) from http://a.com" in caplog.text
    # the extracted content itself is only logged at DEBUG (extraction_service),
    # not re-dumped into orchestrator's INFO-level progress log
    assert secret_looking_value not in caplog.text


def test_process_batch_logs_entry_count_for_list_category(monkeypatch, caplog):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com/": "<html>a</html>",
    }))
    _patch_data_service(monkeypatch)
    category = _make_category("LIST", is_list_category=True)
    category_service = MagicMock()
    category_service.categorize_website.return_value = category
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    entries = [{"station_url": "http://x.com/"}, {"station_url": "http://y.com/"}]
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = (entries, [])
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    with caplog.at_level("INFO", logger="model.orchestrator"):
        orchestrator.process_batch(["http://a.com/"])

    assert "Extracted 2 entries from http://a.com" in caplog.text


def test_process_batch_does_not_log_at_info_when_nothing_extracted(monkeypatch, caplog):
    # irrelevant-category pages are routine, not a "fail" - must not spam INFO
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com/": "<html>a</html>",
    }))
    _patch_data_service(monkeypatch)
    category = _make_category("IRRELEVANT")
    category_service = MagicMock()
    category_service.categorize_website.return_value = category
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    with caplog.at_level("INFO", logger="model.orchestrator"):
        orchestrator.process_batch(["http://a.com/"])

    assert "Extracted" not in caplog.text


def test_process_batch_saves_each_entry_for_list_category(monkeypatch):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com/": "<html>a</html>",
    }))
    data_service = _patch_data_service(monkeypatch)
    category = _make_category("LIST", is_list_category=True)
    category_service = MagicMock()
    category_service.categorize_website.return_value = category
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    entries = [{"station_url": "http://x.com/"}, {"station_url": "http://y.com/"}]
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = (entries, [])
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    orchestrator.process_batch(["http://a.com/"])

    assert data_service.save_extraction.call_args_list == [
        ((1, entries[0]),), ((1, entries[1]),)
    ]


def test_process_batch_queues_links_discovered_during_extraction(monkeypatch):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com/": "<html>a</html>",
    }))
    _patch_data_service(monkeypatch)
    category = _make_category("STATION", is_list_category=False)
    category_service = MagicMock()
    category_service.categorize_website.return_value = category
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = ({"name": "Station A"}, ["http://linked.com/"])
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    orchestrator.process_batch(["http://a.com/"])

    assert fetching_service.url_queue == ["http://linked.com/"]


def test_process_batch_queues_links_but_saves_nothing_for_links_only_category(monkeypatch):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://hub.com/": "<html>hub</html>",
    }))
    data_service = _patch_data_service(monkeypatch)
    category = _make_links_only_category("HUB")
    category_service = MagicMock()
    category_service.categorize_website.return_value = category
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = (None, ["http://station-a.com/"])
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    orchestrator.process_batch(["http://hub.com/"])

    data_service.save_extraction.assert_not_called()
    assert fetching_service.url_queue == ["http://station-a.com/"]


def test_process_batch_skips_extraction_and_keeps_going_when_categorization_fails(monkeypatch, caplog):
    # e.g. category_service.categorize_website() returning None because the
    # page's content overflowed the model's context window - must not crash
    # process_batch, and must not let extraction see a None category
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://too-long.com/": "<html>too long</html>",
        "http://fine.com/": "<html>fine</html>",
    }))
    _patch_data_service(monkeypatch)
    category_service = MagicMock()
    category_service.categorize_website.side_effect = [None, _make_category("STATION")]
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    with caplog.at_level("WARNING", logger="model.orchestrator"):
        orchestrator.process_batch(["http://too-long.com/", "http://fine.com/"])

    assert "Skipping http://too-long.com/ - categorization failed" in caplog.text
    extraction_service.extract_information.assert_called_once()
    assert extraction_service.extract_information.call_args.args[2] == "http://fine.com/"


def test_process_batch_reports_page_timing_to_monitor_service(monkeypatch):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://too-long.com/": "<html>too long</html>",
        "http://station.com/": "<html>station</html>",
    }))
    _patch_data_service(monkeypatch)
    category_service = MagicMock()
    category_service.categorize_website.side_effect = [None, _make_category("STATION")]
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)
    monitor_service = MagicMock()
    monkeypatch.setattr(orchestrator, "monitor_service", monitor_service)

    orchestrator.process_batch(["http://too-long.com/", "http://station.com/"])

    calls = {c.args[0]: c.args[1:] for c in monitor_service.page.call_args_list}
    assert set(calls.keys()) == {"http://too-long.com/", "http://station.com/"}
    # failed categorization: category is None, no extraction attempted (extract_seconds == 0.0)
    failed_category, failed_categorize_s, failed_extract_s = calls["http://too-long.com/"]
    assert failed_category is None
    assert failed_categorize_s >= 0
    assert failed_extract_s == 0.0
    # successful categorization: category name reported, extraction was attempted
    ok_category, ok_categorize_s, ok_extract_s = calls["http://station.com/"]
    assert ok_category == "STATION"
    assert ok_categorize_s >= 0
    assert ok_extract_s >= 0


def test_process_batch_uses_existing_queue_when_no_urls_passed(monkeypatch):
    fetching_service.queue_url("http://already-queued.com/")
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://already-queued.com/": "<html></html>",
    }))
    data_service = _patch_data_service(monkeypatch)
    monkeypatch.setattr(orchestrator, "category_service", MagicMock())
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    orchestrator.process_batch()

    data_service.save_crawl_instance.assert_called_once()
    assert data_service.save_crawl_instance.call_args.args[0] == "http://already-queued.com/"


def test_process_batch_passes_url_to_categorizer(monkeypatch):
    """The URL is a strong classification signal (P7); the classifier cannot
    use it unless process_batch hands it over."""
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com/presse": "<html>a</html>",
    }))
    _patch_data_service(monkeypatch)
    category_service = MagicMock()
    category_service.categorize_website.return_value = _make_category("STATION")
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    orchestrator.process_batch(["http://a.com/presse"])

    args, _ = category_service.categorize_website.call_args
    assert args[1] == "http://a.com/presse"


# ---------------------------------------------------------------------------
# bounded rounds
# ---------------------------------------------------------------------------

def test_process_batch_caps_round_at_max_batch_size(monkeypatch):
    config = _make_orchestrator_config(max_batch_size=2)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(fetching_service, "parse_queue",
                        _fake_parse_queue_returning({f"http://a.com/{i}": "<html/>" for i in range(5)}))
    _patch_data_service(monkeypatch)
    category_service = MagicMock()
    category_service.categorize_website.return_value = _make_category("STATION")
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction = MagicMock()
    extraction.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction)

    orchestrator.process_batch([f"http://a.com/{i}" for i in range(5)])

    assert category_service.categorize_website.call_count == 2


def test_process_batch_requeues_the_remainder(monkeypatch):
    config = _make_orchestrator_config(max_batch_size=2)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(fetching_service, "parse_queue",
                        _fake_parse_queue_returning({f"http://a.com/{i}": "<html/>" for i in range(5)}))
    _patch_data_service(monkeypatch)
    category_service = MagicMock()
    category_service.categorize_website.return_value = _make_category("STATION")
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction = MagicMock()
    extraction.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction)

    orchestrator.process_batch([f"http://a.com/{i}" for i in range(5)])

    assert len(fetching_service.url_queue) == 3


def test_unlimited_batch_size_processes_everything(monkeypatch):
    config = _make_orchestrator_config(max_batch_size=0)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(fetching_service, "parse_queue",
                        _fake_parse_queue_returning({f"http://a.com/{i}": "<html/>" for i in range(5)}))
    _patch_data_service(monkeypatch)
    category_service = MagicMock()
    category_service.categorize_website.return_value = _make_category("STATION")
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction = MagicMock()
    extraction.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction)

    orchestrator.process_batch([f"http://a.com/{i}" for i in range(5)])

    assert category_service.categorize_website.call_count == 5
    assert fetching_service.url_queue == []


def test_process_batch_prefers_high_priority_urls_when_capped(monkeypatch):
    """Frontier ordering: with a capped round, the most promising URLs go
    first rather than whatever was discovered earliest."""
    config = _make_orchestrator_config(max_batch_size=1)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(fetching_service, "url_priorities",
                        {"http://a.com/dull": 0.0, "http://a.com/promising": 9.0})
    fetching_service.url_queue = ["http://a.com/dull", "http://a.com/promising"]
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning(
        {"http://a.com/dull": "<html/>", "http://a.com/promising": "<html/>"}))
    data_service = _patch_data_service(monkeypatch)
    category_service = MagicMock()
    category_service.categorize_website.return_value = _make_category("STATION")
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction = MagicMock()
    extraction.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction)

    orchestrator.process_batch()

    crawled = [c.args[0] for c in data_service.save_crawl_instance.call_args_list]
    assert crawled == ["http://a.com/promising"]


def test_discovery_runs_when_frontier_is_unproductive(monkeypatch):
    """Discovery used to fire only on an empty queue; once link-following
    reaches the open web the queue never empties, so it never ran again."""
    config = _make_orchestrator_config(discover_urls=True, discovery_when_below=0.5,
                                       max_rounds=1, max_discovery_batches=1)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(orchestrator, "init", lambda: None)
    monkeypatch.setattr(orchestrator, "discovery_queries", ["q1"])
    fetching_service.url_queue = ["http://junk.com/a"]
    monkeypatch.setattr(fetching_service, "url_priorities", {"http://junk.com/a": 0.0})

    discovery_calls = []
    monkeypatch.setattr(orchestrator, "run_discovery",
                        lambda: discovery_calls.append(True))
    monkeypatch.setattr(orchestrator, "process_batch", lambda: None)

    orchestrator.run()

    assert discovery_calls, "discovery should fire when nothing queued looks promising"


def test_low_value_queue_is_still_processed_when_discovery_is_exhausted(monkeypatch):
    config = _make_orchestrator_config(discover_urls=True, discovery_when_below=0.5,
                                       max_rounds=1)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(orchestrator, "init", lambda: None)
    monkeypatch.setattr(orchestrator, "discovery_queries", [])   # nothing left to discover
    fetching_service.url_queue = ["http://junk.com/a"]
    monkeypatch.setattr(fetching_service, "url_priorities", {"http://junk.com/a": 0.0})

    batches = []
    monkeypatch.setattr(orchestrator, "process_batch", lambda: batches.append(True))

    orchestrator.run()

    assert batches, "remaining work must still be crawled once discovery is exhausted"


def test_promising_frontier_does_not_trigger_discovery(monkeypatch):
    config = _make_orchestrator_config(discover_urls=True, discovery_when_below=0.5,
                                       max_rounds=1, max_discovery_batches=1)
    monkeypatch.setattr(orchestrator.config_service, "get_config", lambda: config)
    monkeypatch.setattr(orchestrator, "init", lambda: None)
    monkeypatch.setattr(orchestrator, "discovery_queries", ["q1"])
    fetching_service.url_queue = ["http://good.com/a"]
    monkeypatch.setattr(fetching_service, "url_priorities", {"http://good.com/a": 9.0})

    discovery_calls = []
    monkeypatch.setattr(orchestrator, "run_discovery", lambda: discovery_calls.append(True))
    monkeypatch.setattr(orchestrator, "process_batch", lambda: None)

    orchestrator.run()

    assert not discovery_calls


# ---------------------------------------------------------------------------
# per-site extraction budget
#
# Observed at depth: tina-uvb.de produced 20 records from 20 pages, all one
# entity; musella-stiftung.li 20 from 20; natur-zuerst.de 21 from 21. The
# classifier is inconsistent about which subpages introduce the organisation
# (/geldspenden came back STATION while /sachspenden came back HUB), so the
# prompt alone cannot stop this. A site that has already yielded its details
# a few times has nothing left to give, and 33% of all extraction calls in
# one run went to pages past that point.
#
# Listing pages are exempt: each yields *different* entities, so capping them
# would discard real data rather than redundancy.
# ---------------------------------------------------------------------------

def _config_with_extraction_budget(monkeypatch, budget):
    cfg = config_service.get_config().model_copy(
        update={"max_extractions_per_site": budget})
    monkeypatch.setattr(config_service, "_session_config", cfg)
    return cfg


def test_extraction_is_skipped_once_a_site_has_given_enough(monkeypatch):
    _config_with_extraction_budget(monkeypatch, 3)
    category = make_fake_category("STATION")

    extraction_service = MagicMock()
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)
    monkeypatch.setattr(orchestrator, "category_service", MagicMock())
    monkeypatch.setattr(orchestrator, "monitor_service", MagicMock())
    data_service = MagicMock()
    data_service.count_extracted_pages_for_site.return_value = 3
    monkeypatch.setattr(orchestrator, "data_service", data_service)
    monkeypatch.setattr(orchestrator, "url_service", MagicMock())

    orchestrator.process_page(1, "https://a.example/page20", "<html/>", category)

    extraction_service.extract_information.assert_not_called()


def test_a_site_under_budget_is_still_extracted(monkeypatch):
    _config_with_extraction_budget(monkeypatch, 3)
    category = make_fake_category("STATION")

    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = ({"name": "A"}, [])
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)
    monkeypatch.setattr(orchestrator, "category_service", MagicMock())
    monkeypatch.setattr(orchestrator, "monitor_service", MagicMock())
    data_service = MagicMock()
    data_service.count_extracted_pages_for_site.return_value = 2
    monkeypatch.setattr(orchestrator, "data_service", data_service)
    monkeypatch.setattr(orchestrator, "url_service", MagicMock())

    orchestrator.process_page(1, "https://a.example/page3", "<html/>", category)

    extraction_service.extract_information.assert_called_once()


def test_a_listing_page_is_never_capped(monkeypatch):
    """Each listing page yields different entities; capping them would throw
    away real data."""
    _config_with_extraction_budget(monkeypatch, 3)
    category = make_fake_category("LIST").model_copy(update={"is_list_category": True})

    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = ([{"name": "A"}], [])
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)
    monkeypatch.setattr(orchestrator, "category_service", MagicMock())
    monkeypatch.setattr(orchestrator, "monitor_service", MagicMock())
    data_service = MagicMock()
    data_service.count_extracted_pages_for_site.return_value = 99
    monkeypatch.setattr(orchestrator, "data_service", data_service)
    monkeypatch.setattr(orchestrator, "url_service", MagicMock())

    orchestrator.process_page(1, "https://a.example/list", "<html/>", category)

    extraction_service.extract_information.assert_called_once()


def test_no_budget_configured_means_no_cap(monkeypatch):
    _config_with_extraction_budget(monkeypatch, 0)
    category = make_fake_category("STATION")

    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = ({"name": "A"}, [])
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)
    monkeypatch.setattr(orchestrator, "category_service", MagicMock())
    monkeypatch.setattr(orchestrator, "monitor_service", MagicMock())
    data_service = MagicMock()
    data_service.count_extracted_pages_for_site.return_value = 500
    monkeypatch.setattr(orchestrator, "data_service", data_service)
    monkeypatch.setattr(orchestrator, "url_service", MagicMock())

    orchestrator.process_page(1, "https://a.example/page", "<html/>", category)

    extraction_service.extract_information.assert_called_once()


def test_a_capped_page_still_reaches_a_terminal_state(monkeypatch):
    """Otherwise it stays pending forever and every restart retries it."""
    _config_with_extraction_budget(monkeypatch, 1)
    category = make_fake_category("STATION")

    monkeypatch.setattr(orchestrator, "extraction_service", MagicMock())
    monkeypatch.setattr(orchestrator, "category_service", MagicMock())
    monkeypatch.setattr(orchestrator, "monitor_service", MagicMock())
    data_service = MagicMock()
    data_service.count_extracted_pages_for_site.return_value = 5
    data_service.STATE_EXTRACTED = "EXTRACTED"
    monkeypatch.setattr(orchestrator, "data_service", data_service)
    monkeypatch.setattr(orchestrator, "url_service", MagicMock())

    orchestrator.process_page(1, "https://a.example/page", "<html/>", category)

    data_service.set_crawl_state.assert_any_call(1, "EXTRACTED")


# ---------------------------------------------------------------------------
# acting on a "mislabeled" verdict
# ---------------------------------------------------------------------------

def _mislabel_setup(monkeypatch, target="HUB"):
    import model.analyzer.extraction_service as real_extraction
    cfg = config_service.get_config().model_copy(update={
        "mislabel_check": True, "mislabeled_category": target,
        "max_extractions_per_site": 0,
        "referrer_weights": {"STATION": 4.0, "HUB": 1.0}})
    monkeypatch.setattr(config_service, "_session_config", cfg)

    extraction = MagicMock()
    extraction.MISLABELED = real_extraction.MISLABELED
    extraction.extract_information.return_value = (real_extraction.MISLABELED,
                                                    ["https://e.com/next"])
    monkeypatch.setattr(orchestrator, "extraction_service", extraction)
    monkeypatch.setattr(orchestrator, "category_service", MagicMock())
    monkeypatch.setattr(orchestrator, "monitor_service", MagicMock())
    data = MagicMock()
    data.STATE_EXTRACTED = "EXTRACTED"
    data.count_extracted_pages_for_site.return_value = 0
    monkeypatch.setattr(orchestrator, "data_service", data)
    queued = []
    monkeypatch.setattr(orchestrator.fetching_service, "queue_url",
                        lambda url, priority=0.0: queued.append((url, priority)))
    return data, queued, cfg


def test_a_mislabeled_page_stores_no_record(monkeypatch):
    data, _, _ = _mislabel_setup(monkeypatch)
    orchestrator.process_page(7, "https://e.com/p", "<html/>", make_fake_category("STATION"))
    data.save_extraction.assert_not_called()


def test_a_mislabeled_page_is_refiled_and_remembers_the_original(monkeypatch):
    data, _, _ = _mislabel_setup(monkeypatch, target="HUB")
    orchestrator.process_page(7, "https://e.com/p", "<html/>", make_fake_category("STATION"))
    data.reclassify_page.assert_called_once_with(7, "HUB", from_category="STATION")
    data.set_crawl_state.assert_any_call(7, "EXTRACTED")


def test_a_mislabeled_page_passes_on_the_weight_of_its_new_category(monkeypatch):
    """A page the extractor says is not a station must not hand its links a
    station's referrer weight."""
    _, queued, cfg = _mislabel_setup(monkeypatch, target="HUB")
    scores = []
    monkeypatch.setattr(orchestrator.url_service, "score_url",
                        lambda link, referrer_category_weight=0.0, **kw:
                        scores.append(referrer_category_weight) or 0.0)
    orchestrator.process_page(7, "https://e.com/p", "<html/>", make_fake_category("STATION"))
    assert queued, "links from a mislabeled page are still followed"
    assert scores == [1.0]


def test_every_classified_page_tells_the_frontier_what_it_was_worth(monkeypatch):
    _config_with_extraction_budget(monkeypatch, 0)
    category = make_fake_category("STATION")
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)
    monkeypatch.setattr(orchestrator, "category_service", MagicMock())
    monkeypatch.setattr(orchestrator, "monitor_service", MagicMock())
    monkeypatch.setattr(orchestrator, "data_service", MagicMock())
    fetching_service = MagicMock()
    monkeypatch.setattr(orchestrator, "fetching_service", fetching_service)

    orchestrator.process_page(1, "https://a.example/kontakt", "<html/>", category)

    fetching_service.record_page_value.assert_called_once_with("https://a.example/kontakt", category)


def test_a_page_that_raises_is_marked_failed_instead_of_ending_the_run(monkeypatch):
    data_service = MagicMock()
    monkeypatch.setattr(orchestrator, "data_service", data_service)
    seen = []

    def process_page(crawl_id, url, html, category=None):
        seen.append(url)
        if url.endswith("/bad"):
            raise ValueError("Invalid IPv6 URL")

    monkeypatch.setattr(orchestrator, "process_page", process_page)

    orchestrator.process_page_safely(1, "https://a.example/bad", "<html/>")
    orchestrator.process_page_safely(2, "https://a.example/good", "<html/>")

    assert seen == ["https://a.example/bad", "https://a.example/good"]
    data_service.set_crawl_state.assert_called_once()
    args, kwargs = data_service.set_crawl_state.call_args
    assert args[0] == 1
    assert args[1] == data_service.STATE_FAILED
    assert "Invalid IPv6 URL" in kwargs["last_error"]
