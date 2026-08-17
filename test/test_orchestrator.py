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

def test_init_wires_config_into_data_service_and_fetching_service(monkeypatch):
    config = MagicMock()
    config.get_starting_url_path.return_value = "starting_urls.csv"
    config.redo_failed_fetches = True
    config.redo_all_fetches = False
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
