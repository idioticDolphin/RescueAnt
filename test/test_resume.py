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


# ---------------------------------------------------------------------------
# a resumed page must not also be fetched again
#
# Observed live: after a restart, already-processed URLs were fetched a second
# time and produced a second crawl row and a second set of records. Two
# mechanisms both claimed the page - resume_pending() restored it from the
# store, while link discovery queued the same URL for a fresh fetch, because
# only *terminal* URLs were registered as processed.
# ---------------------------------------------------------------------------

def test_a_resumed_page_is_not_queued_for_fetching_again(monkeypatch):
    import model.crawler.fetching_service as fetching_service

    _fake_services(monkeypatch)
    monkeypatch.setattr(fetching_service, "processed_urls", {})
    monkeypatch.setattr(fetching_service, "url_queue", [])
    monkeypatch.setattr(fetching_service, "url_priorities", {})

    digest, path = page_store.store("<html>a</html>")
    data_service.save_crawl_instance("https://a.example/page", 1.0, True,
                                     content_path=path, content_sha256=digest,
                                     site="a.example")

    orchestrator.resume_pending()

    # a link to the same page found later in the run must be ignored
    fetching_service.queue_url("https://a.example/page")
    assert fetching_service.url_queue == []


def test_a_resumed_page_is_recognised_through_url_variants(monkeypatch):
    """The queue canonicalises before checking, so the resumed URL has to be
    registered in the same canonical form or the guard silently misses."""
    import model.crawler.fetching_service as fetching_service

    _fake_services(monkeypatch)
    monkeypatch.setattr(fetching_service, "processed_urls", {})
    monkeypatch.setattr(fetching_service, "url_queue", [])
    monkeypatch.setattr(fetching_service, "url_priorities", {})

    digest, path = page_store.store("<html>a</html>")
    data_service.save_crawl_instance("https://a.example/page", 1.0, True,
                                     content_path=path, content_sha256=digest,
                                     site="a.example")

    orchestrator.resume_pending()

    fetching_service.queue_url("https://a.example/page#section")
    fetching_service.queue_url("https://a.example/page?utm_source=x")
    assert fetching_service.url_queue == []


def test_a_page_whose_content_vanished_is_still_requeued(monkeypatch):
    """The guard must not suppress the one case that genuinely needs a
    refetch."""
    import model.crawler.fetching_service as fetching_service

    _fake_services(monkeypatch)
    monkeypatch.setattr(fetching_service, "processed_urls", {})
    monkeypatch.setattr(fetching_service, "url_queue", [])
    monkeypatch.setattr(fetching_service, "url_priorities", {})

    data_service.save_crawl_instance("https://gone.example/page", 1.0, True,
                                     content_path="no/such/file.html.gz",
                                     content_sha256="x", site="gone.example")

    orchestrator.resume_pending()

    assert "https://gone.example/page" in fetching_service.url_queue


# ---------------------------------------------------------------------------
# reprocessing under a changed taxonomy leaves no stale records
# ---------------------------------------------------------------------------

def _with_config(monkeypatch, **update):
    hub = Category(name="HUB", relevancy=Relevancy.LINKS, process_links=False)
    config = config_service.get_config()
    config = config.model_copy(update={"categories": list(config.categories) + [hub], **update})
    monkeypatch.setattr(data_service, "config", config)
    monkeypatch.setattr(config_service, "_session_config", config)
    monkeypatch.setattr(page_store, "configure", lambda path: None)
    return config


def _extracted_page(url):
    digest, path = page_store.store(f"<html>{url}</html>")
    crawl_id = data_service.save_crawl_instance(url, 1.0, True, content_path=path,
                                                content_sha256=digest, site="a.com")
    data_service.save_site_category(crawl_id, "STATION")
    data_service.save_extraction(crawl_id, {"name": url})
    data_service.set_crawl_state(crawl_id, data_service.STATE_EXTRACTED)
    return crawl_id


def test_reprocessing_does_not_skip_stored_pages_over_the_page_budget(monkeypatch):
    # The budget limits what is fetched. Pages already fetched are paid for;
    # skipping them on reprocess left their old records behind, uncategorised.
    _with_config(monkeypatch, max_pages_per_site=1)
    _fake_services(monkeypatch)
    monkeypatch.setattr(orchestrator, "fetching_service", MagicMock())
    for page in ("a", "b", "c"):
        _extracted_page(f"http://a.com/{page}")

    assert orchestrator.reprocess("categorize") == 3
    with data_service.get_connection() as c:
        states = {r[0] for r in c.execute("SELECT state FROM crawls")}
    assert states == {data_service.STATE_EXTRACTED}


def test_a_page_reclassified_as_follow_only_loses_its_old_records(monkeypatch):
    config = _with_config(monkeypatch, max_extractions_per_site=1)
    category_service, extraction_service = _fake_services(monkeypatch)
    category_service.categorize_website.return_value = config.get_category("HUB")
    extraction_service.extract_information.return_value = (None, [])
    monkeypatch.setattr(orchestrator, "fetching_service", MagicMock())
    _extracted_page("http://a.com/a")
    _extracted_page("http://a.com/b")

    orchestrator.reprocess("categorize")

    assert _entries() == []
