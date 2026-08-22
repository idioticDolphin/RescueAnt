import time
from unittest.mock import MagicMock

import pytest

import model.orchestrator as orchestrator
import model.crawler.fetching_service as fetching_service
from model.objects.category import Category


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
        is_relevant=True,
        analysis_prompt="p",
        analysis_max_tokens=1,
        analysis_model_id=0,
        process_links=True,
        is_list_category=is_list_category,
        fields={"type": "object", "properties": {}, "required": []},
    )


def _patch_data_service(monkeypatch):
    data_service = MagicMock()
    data_service.save_crawl_instance.side_effect = range(1, 1000)
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
    discovery_service.discover_urls.return_value = ["http://a.com", "http://b.com"]
    discovery_service.queue_discovered_urls.return_value = 2
    monkeypatch.setattr(orchestrator, "discovery_service", discovery_service)
    monkeypatch.setattr(orchestrator, "discovery_queries", ["q1"])

    added = orchestrator.run_discovery()

    discovery_service.queue_discovered_urls.assert_called_once_with(["http://a.com", "http://b.com"])
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
    fetching_service.url_queue = ["http://a.com", "http://b.com"]

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
        fetching_service.url_queue.append("http://discovered.com")

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

    fetching_service.url_queue = ["http://never-ending.com"]
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

    fetching_service.url_queue = ["http://never-ending.com"]
    process_batch_calls = []

    def fake_process_batch():
        process_batch_calls.append(True)
        time.sleep(0.06)

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
        "http://a.com": "<html>a</html>",
        "http://b.com": "",
    }))
    data_service = _patch_data_service(monkeypatch)
    category_service = MagicMock()
    category_service.categorize_website.return_value = _make_category("STATION")
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    orchestrator.process_batch(["http://a.com", "http://b.com"])

    calls = {c.args[0]: c.args[2] for c in data_service.save_crawl_instance.call_args_list}
    assert calls == {"http://a.com": True, "http://b.com": False}


def test_process_batch_only_categorizes_successfully_fetched_sites(monkeypatch):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com": "<html>a</html>",
        "http://b.com": "",
    }))
    _patch_data_service(monkeypatch)
    category_service = MagicMock()
    category_service.categorize_website.return_value = _make_category("IRRELEVANT")
    category_service.categorize_website.return_value.is_relevant = False
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    orchestrator.process_batch(["http://a.com", "http://b.com"])

    assert category_service.categorize_website.call_count == 1
    category_service.categorize_website.assert_called_with("<html>a</html>")


def test_process_batch_saves_extraction_for_single_category(monkeypatch):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com": "<html>a</html>",
    }))
    data_service = _patch_data_service(monkeypatch)
    category = _make_category("STATION", is_list_category=False)
    category_service = MagicMock()
    category_service.categorize_website.return_value = category
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = ({"name": "Station A"}, [])
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    orchestrator.process_batch(["http://a.com"])

    data_service.save_extraction.assert_called_once_with(1, {"name": "Station A"})


def test_process_batch_logs_fetch_summary_and_categorization(monkeypatch, caplog):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com": "<html>a</html>",
        "http://b.com": "",
    }))
    _patch_data_service(monkeypatch)
    category_service = MagicMock()
    category_service.categorize_website.return_value = _make_category("STATION")
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    with caplog.at_level("INFO", logger="model.orchestrator"):
        orchestrator.process_batch(["http://a.com", "http://b.com"])

    assert "Fetched 1/2 URL(s) successfully" in caplog.text
    assert "Categorized http://a.com as STATION" in caplog.text


def test_process_batch_logs_extraction_progress_without_dumping_full_content(monkeypatch, caplog):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com": "<html>a</html>",
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
        orchestrator.process_batch(["http://a.com"])

    assert "Extracted 1 field(s) from http://a.com" in caplog.text
    # the extracted content itself is only logged at DEBUG (extraction_service),
    # not re-dumped into orchestrator's INFO-level progress log
    assert secret_looking_value not in caplog.text


def test_process_batch_logs_entry_count_for_list_category(monkeypatch, caplog):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com": "<html>a</html>",
    }))
    _patch_data_service(monkeypatch)
    category = _make_category("LIST", is_list_category=True)
    category_service = MagicMock()
    category_service.categorize_website.return_value = category
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    entries = [{"station_url": "http://x.com"}, {"station_url": "http://y.com"}]
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = (entries, [])
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    with caplog.at_level("INFO", logger="model.orchestrator"):
        orchestrator.process_batch(["http://a.com"])

    assert "Extracted 2 entries from http://a.com" in caplog.text


def test_process_batch_does_not_log_at_info_when_nothing_extracted(monkeypatch, caplog):
    # irrelevant-category pages are routine, not a "fail" - must not spam INFO
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com": "<html>a</html>",
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
        orchestrator.process_batch(["http://a.com"])

    assert "Extracted" not in caplog.text


def test_process_batch_saves_each_entry_for_list_category(monkeypatch):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com": "<html>a</html>",
    }))
    data_service = _patch_data_service(monkeypatch)
    category = _make_category("LIST", is_list_category=True)
    category_service = MagicMock()
    category_service.categorize_website.return_value = category
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    entries = [{"station_url": "http://x.com"}, {"station_url": "http://y.com"}]
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = (entries, [])
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    orchestrator.process_batch(["http://a.com"])

    assert data_service.save_extraction.call_args_list == [
        ((1, entries[0]),), ((1, entries[1]),)
    ]


def test_process_batch_queues_links_discovered_during_extraction(monkeypatch):
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://a.com": "<html>a</html>",
    }))
    _patch_data_service(monkeypatch)
    category = _make_category("STATION", is_list_category=False)
    category_service = MagicMock()
    category_service.categorize_website.return_value = category
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = ({"name": "Station A"}, ["http://linked.com"])
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    orchestrator.process_batch(["http://a.com"])

    assert fetching_service.url_queue == ["http://linked.com"]


def test_process_batch_skips_extraction_and_keeps_going_when_categorization_fails(monkeypatch, caplog):
    # e.g. category_service.categorize_website() returning None because the
    # page's content overflowed the model's context window - must not crash
    # process_batch, and must not let extraction see a None category
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://too-long.com": "<html>too long</html>",
        "http://fine.com": "<html>fine</html>",
    }))
    _patch_data_service(monkeypatch)
    category_service = MagicMock()
    category_service.categorize_website.side_effect = [None, _make_category("STATION")]
    monkeypatch.setattr(orchestrator, "category_service", category_service)
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    with caplog.at_level("WARNING", logger="model.orchestrator"):
        orchestrator.process_batch(["http://too-long.com", "http://fine.com"])

    assert "Skipping http://too-long.com - categorization failed" in caplog.text
    extraction_service.extract_information.assert_called_once()
    assert extraction_service.extract_information.call_args.args[2] == "http://fine.com"


def test_process_batch_uses_existing_queue_when_no_urls_passed(monkeypatch):
    fetching_service.queue_url("http://already-queued.com")
    monkeypatch.setattr(fetching_service, "parse_queue", _fake_parse_queue_returning({
        "http://already-queued.com": "<html></html>",
    }))
    data_service = _patch_data_service(monkeypatch)
    monkeypatch.setattr(orchestrator, "category_service", MagicMock())
    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = None
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    orchestrator.process_batch()

    data_service.save_crawl_instance.assert_called_once()
    assert data_service.save_crawl_instance.call_args.args[0] == "http://already-queued.com"
