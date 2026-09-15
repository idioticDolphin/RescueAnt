"""
Integration tests for durable resumption (P23/P24/P25).

These use the real data_service and page_store against throwaway paths, with
only the LLM-backed services faked, because the property under test - "an
interrupted run continues from exactly where it stopped, losing nothing and
double-processing nothing" - is a property of how those two collaborate.
"""
from unittest.mock import MagicMock

import pytest

import model.orchestrator as orchestrator
import model.tools.config_service as config_service
import model.tools.data_service as data_service
from model.objects.category import Category, Relevancy
from model.tools import page_store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(data_service, "DATABASE_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(data_service, "db_fields", [])
    monkeypatch.setattr(page_store, "STORE_ROOT", tmp_path / "store")

    category = Category(
        name="STATION", relevancy=Relevancy.CONTENT,
        fields={"type": "object", "properties": {"name": {"type": "string"}},
                "required": ["name"]},
        analysis_prompt="p", analysis_max_tokens=50, analysis_model_id=0,
        process_links=False,
    )
    config = config_service.get_config().model_copy(update={"categories": [category]})
    monkeypatch.setattr(data_service, "config", config)
    monkeypatch.setattr(config_service, "_session_config", config)
    data_service.init_db()
    yield


def _fake_services(monkeypatch, extracted={"name": "Station A"}):
    category = config_service.get_config().get_category("STATION")
    category_service = MagicMock()
    category_service.categorize_website.return_value = category
    monkeypatch.setattr(orchestrator, "category_service", category_service)

    extraction_service = MagicMock()
    extraction_service.extract_information.return_value = (extracted, [])
    monkeypatch.setattr(orchestrator, "extraction_service", extraction_service)

    monkeypatch.setattr(orchestrator, "monitor_service", MagicMock())
    return category_service, extraction_service


def _entries():
    with data_service.get_connection() as c:
        return [dict(r) for r in c.execute("SELECT * FROM entries")]


# ---------------------------------------------------------------------------
# a page interrupted before processing is resumable, not lost
# ---------------------------------------------------------------------------

def test_fetched_but_unprocessed_page_is_pending_work(monkeypatch):
    """This is the F7 regression test: the exact state that used to be
    recorded as a finished crawl and skipped forever."""
    digest, path = page_store.store("<html>a</html>")
    data_service.save_crawl_instance("http://a.com/", 1.0, True,
                                     content_path=path, content_sha256=digest)

    assert len(data_service.get_pending_crawls()) == 1
    assert "http://a.com/" not in data_service.get_finished_crawl_urls()


def test_resume_processes_a_fetched_page_without_refetching(monkeypatch):
    category_service, extraction_service = _fake_services(monkeypatch)
    fetching_service = MagicMock()
    monkeypatch.setattr(orchestrator, "fetching_service", fetching_service)

    digest, path = page_store.store("<html>stored body</html>")
    data_service.save_crawl_instance("http://a.com/", 1.0, True,
                                     content_path=path, content_sha256=digest)

    assert orchestrator.resume_pending() == 1

    # categorized and extracted from the *stored* body, with no fetching
    assert category_service.categorize_website.call_args[0][0] == "<html>stored body</html>"
    fetching_service.get_content.assert_not_called()
    assert len(_entries()) == 1
    assert data_service.get_pending_crawls() == []


def test_resume_of_categorized_page_skips_recategorization(monkeypatch):
    category_service, extraction_service = _fake_services(monkeypatch)
    monkeypatch.setattr(orchestrator, "fetching_service", MagicMock())

    digest, path = page_store.store("<html>x</html>")
    crawl_id = data_service.save_crawl_instance("http://a.com/", 1.0, True,
                                                content_path=path, content_sha256=digest)
    data_service.save_site_category(crawl_id, "STATION")
    data_service.set_crawl_state(crawl_id, data_service.STATE_CATEGORIZED)

    orchestrator.resume_pending()

    category_service.categorize_website.assert_not_called()
    assert extraction_service.extract_information.called


def test_resume_requeues_pages_whose_stored_content_vanished(monkeypatch):
    _fake_services(monkeypatch)
    fetching_service = MagicMock()
    monkeypatch.setattr(orchestrator, "fetching_service", fetching_service)

    crawl_id = data_service.save_crawl_instance("http://gone.com/", 1.0, True,
                                                content_path="ab/cd/missing.html.gz")

    orchestrator.resume_pending()

    fetching_service.queue_url.assert_called_once_with("http://gone.com/")
    with data_service.get_connection() as c:
        state = c.execute("SELECT state FROM crawls WHERE crawl_id=?", (crawl_id,)).fetchone()["state"]
    assert state == data_service.STATE_FETCH_FAILED


def test_resume_is_a_noop_when_nothing_is_pending(monkeypatch):
    _fake_services(monkeypatch)
    monkeypatch.setattr(orchestrator, "fetching_service", MagicMock())
    assert orchestrator.resume_pending() == 0


# ---------------------------------------------------------------------------
# idempotency: processing a page twice must not duplicate its records
# ---------------------------------------------------------------------------

def test_reprocessing_a_page_does_not_duplicate_entries(monkeypatch):
    _fake_services(monkeypatch)
    monkeypatch.setattr(orchestrator, "fetching_service", MagicMock())

    digest, path = page_store.store("<html>x</html>")
    crawl_id = data_service.save_crawl_instance("http://a.com/", 1.0, True,
                                                content_path=path, content_sha256=digest)

    orchestrator.process_page(crawl_id, "http://a.com/", "<html>x</html>")
    first = len(_entries())
    # simulate a crash after extraction but before the state flip, then resume
    data_service.set_crawl_state(crawl_id, data_service.STATE_CATEGORIZED)
    orchestrator.resume_pending()

    assert len(_entries()) == first == 1


def test_interrupted_run_reaches_same_end_state_as_uninterrupted(monkeypatch):
    """The acceptance property: crashing between stages must not change the
    final result."""
    _fake_services(monkeypatch)
    monkeypatch.setattr(orchestrator, "fetching_service", MagicMock())

    digest, path = page_store.store("<html>x</html>")
    crawl_id = data_service.save_crawl_instance("http://a.com/", 1.0, True,
                                                content_path=path, content_sha256=digest)

    # "crash" right after fetch: nothing but the fetched row exists
    assert _entries() == []
    # resume once...
    orchestrator.resume_pending()
    # ...and again, as if the process had been killed and restarted twice more
    orchestrator.resume_pending()
    orchestrator.resume_pending()

    assert len(_entries()) == 1
    assert data_service.count_by_state()[data_service.STATE_EXTRACTED] == 1


# ---------------------------------------------------------------------------
# state transitions drive the lifecycle
# ---------------------------------------------------------------------------

def test_process_page_marks_page_extracted(monkeypatch):
    _fake_services(monkeypatch)
    monkeypatch.setattr(orchestrator, "fetching_service", MagicMock())

    crawl_id = data_service.save_crawl_instance("http://a.com/", 1.0, True)
    orchestrator.process_page(crawl_id, "http://a.com/", "<html>x</html>")

    assert data_service.count_by_state()[data_service.STATE_EXTRACTED] == 1


def test_failed_categorization_is_recorded_not_left_pending(monkeypatch):
    category_service, _ = _fake_services(monkeypatch)
    category_service.categorize_website.return_value = None
    monkeypatch.setattr(orchestrator, "fetching_service", MagicMock())

    crawl_id = data_service.save_crawl_instance("http://a.com/", 1.0, True)
    orchestrator.process_page(crawl_id, "http://a.com/", "<html>x</html>")

    assert data_service.get_pending_crawls() == []
    assert data_service.count_by_state()[data_service.STATE_FAILED] == 1
