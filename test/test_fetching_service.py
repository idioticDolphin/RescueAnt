import asyncio
from unittest.mock import MagicMock

import pytest

import model.crawler.fetching_service as fetching_service


@pytest.fixture(autouse=True)
def _isolate_fetching_service_state(monkeypatch):
    """fetching_service keeps several module-level global caches; reset them
    around every test so tests can't leak state into one another."""
    monkeypatch.setattr(fetching_service, "url_queue", [])
    monkeypatch.setattr(fetching_service, "processed_urls", {})
    monkeypatch.setattr(fetching_service, "_robots_cache", {})
    monkeypatch.setattr(fetching_service, "_last_request_time", {})
    monkeypatch.setattr(fetching_service, "url_priorities", {})
    monkeypatch.setattr(fetching_service, "abandoned_sites", set())
    monkeypatch.setattr(fetching_service, "_site_low_value", {})


# ---------------------------------------------------------------------------
# _get_domain
# ---------------------------------------------------------------------------

def test_get_domain_extracts_netloc():
    assert fetching_service._get_domain("http://example.com/some/page") == "example.com"


def test_get_domain_ignores_path_and_query():
    assert fetching_service._get_domain("https://example.com/x?y=1") == "example.com"


# ---------------------------------------------------------------------------
# queue_url
# ---------------------------------------------------------------------------

def test_queue_url_adds_new_url():
    fetching_service.queue_url("http://example.com/a")
    assert fetching_service.url_queue == ["http://example.com/a"]


def test_queue_url_does_not_add_duplicate_already_queued():
    fetching_service.queue_url("http://example.com/a")
    fetching_service.queue_url("http://example.com/a")
    assert fetching_service.url_queue == ["http://example.com/a"]


def test_queue_url_does_not_re_add_already_processed_url():
    fetching_service.processed_urls["http://example.com/a"] = "<html></html>"
    fetching_service.queue_url("http://example.com/a")
    assert fetching_service.url_queue == []


# ---------------------------------------------------------------------------
# _is_allowed (robots.txt)
# ---------------------------------------------------------------------------

def _robots(monkeypatch, status=200, body="", boom=None):
    """Answer the next robots.txt request with this status and body."""
    calls = []

    def fake_get(url, headers=None, timeout=None, **kwargs):
        calls.append((url, (headers or {}).get("User-Agent")))
        if boom:
            raise boom
        response = MagicMock()
        response.status_code = status
        response.text = body
        return response

    monkeypatch.setattr(fetching_service.requests, "get", fake_get)
    return calls


def test_a_disallow_rule_is_obeyed(monkeypatch):
    _robots(monkeypatch, body="User-agent: *\nDisallow: /private\n")

    assert fetching_service._is_allowed("http://example.com/private/page") is False
    assert fetching_service._is_allowed("http://example.com/public") is True


def test_an_empty_robots_file_allows_everything(monkeypatch):
    _robots(monkeypatch, body="")

    assert fetching_service._is_allowed("http://example.com/page") is True


def test_a_forbidden_robots_file_does_not_mean_a_forbidden_site(monkeypatch):
    # Bot protection answers 403 to anything that is not a browser, and the
    # standard library reads that as "disallow everything". 480 hosts were
    # skipped that way in one week of crawling, wildlife rescues among them.
    # RFC 9309 is explicit: a 4xx robots.txt is unavailable, not restrictive.
    _robots(monkeypatch, status=403)

    assert fetching_service._is_allowed("http://example.com/page") is True


def test_a_missing_robots_file_allows_everything(monkeypatch):
    _robots(monkeypatch, status=404)

    assert fetching_service._is_allowed("http://example.com/page") is True


def test_a_server_error_is_treated_as_a_full_disallow(monkeypatch):
    # RFC 9309: an unreachable robots.txt means the server is in trouble, and
    # a crawler should stay off it until it can ask again.
    _robots(monkeypatch, status=503)

    assert fetching_service._is_allowed("http://example.com/page") is False


def test_an_unreachable_host_is_left_to_the_fetch_to_report(monkeypatch):
    _robots(monkeypatch, boom=OSError("connection refused"))

    assert fetching_service._is_allowed("http://example.com/page") is True


def test_robots_is_asked_once_per_domain(monkeypatch):
    calls = _robots(monkeypatch, body="")

    fetching_service._is_allowed("http://example.com/a")
    fetching_service._is_allowed("http://example.com/b")

    assert len(calls) == 1


def test_the_crawler_names_itself_when_asking_for_robots(monkeypatch):
    calls = _robots(monkeypatch, body="")

    fetching_service._is_allowed("http://example.com/a")

    url, agent = calls[0]
    assert url == "http://example.com/robots.txt"
    assert agent and "RescueAnt" in agent


# ---------------------------------------------------------------------------
# _wait_politely
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_politely_does_not_sleep_on_first_request_for_domain(monkeypatch):
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await fetching_service._wait_politely("http://example.com/a")

    assert sleep_calls == []
    assert "example.com" in fetching_service._last_request_time


@pytest.mark.asyncio
async def test_wait_politely_sleeps_remaining_time_for_same_domain(monkeypatch):
    # NOTE: we deliberately do NOT monkeypatch time.monotonic globally here.
    # fetching_service does `import time` and calls `time.monotonic()`, so
    # patching that attribute patches the one shared stdlib `time` module
    # for the whole process - including asyncio's own internal event-loop
    # scheduling, which also relies on monotonic time and starts raising
    # StopIteration / misbehaving once its clock is hijacked by a short
    # canned sequence. Using the real clock with a small, known offset
    # avoids that entirely and is more than precise enough for this check.
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    import time as real_time
    fetching_service.politeness_delay = 5
    fetching_service._last_request_time["example.com"] = real_time.monotonic() - 0.2

    await fetching_service._wait_politely("http://example.com/a")

    assert len(sleep_calls) == 1
    assert sleep_calls[0] == pytest.approx(4.8, abs=0.05)


@pytest.mark.asyncio
async def test_wait_politely_does_not_sleep_for_different_domains(monkeypatch):
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    import time as real_time
    fetching_service._last_request_time["other-domain.com"] = real_time.monotonic()

    await fetching_service._wait_politely("http://example.com/a")

    assert sleep_calls == []


# ---------------------------------------------------------------------------
# get_content
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_content_returns_cached_content_without_fetching(monkeypatch):
    fetching_service.processed_urls["http://example.com/a"] = "<html>cached</html>"

    # if get_content tried to actually fetch, this would raise, since we
    # didn't provide a browser or patch async_playwright
    result = await fetching_service.get_content("http://example.com/a")

    assert result == "<html>cached</html>"


@pytest.mark.asyncio
async def test_get_content_returns_empty_and_caches_when_disallowed_by_robots(monkeypatch):
    monkeypatch.setattr(fetching_service, "_is_allowed", lambda url, user_agent="*": False)

    result = await fetching_service.get_content("http://example.com/blocked")

    assert result == ""
    assert fetching_service.processed_urls["http://example.com/blocked"] == ""


@pytest.mark.asyncio
async def test_get_content_fetches_via_provided_browser(monkeypatch):
    monkeypatch.setattr(fetching_service, "_is_allowed", lambda url, user_agent="*": True)

    async def fake_wait_politely(url):
        return None

    monkeypatch.setattr(fetching_service, "_wait_politely", fake_wait_politely)

    fake_page = MagicMock()
    fake_page.goto = _async_mock(None)
    fake_page.content = _async_mock("<html>fetched</html>")
    fake_page.close = _async_mock(None)

    fake_browser = MagicMock()
    fake_browser.new_page = _async_mock(fake_page)

    result = await fetching_service.get_content("http://example.com/new", browser=fake_browser)

    assert result == "<html>fetched</html>"
    assert fetching_service.processed_urls["http://example.com/new"] == "<html>fetched</html>"


@pytest.mark.asyncio
async def test_get_content_falls_back_to_load_when_networkidle_times_out(monkeypatch):
    monkeypatch.setattr(fetching_service, "_is_allowed", lambda url, user_agent="*": True)
    monkeypatch.setattr(fetching_service, "_wait_politely", _async_mock(None))

    goto_calls = []

    async def flaky_goto(url, wait_until, timeout):
        goto_calls.append(wait_until)
        if wait_until == "networkidle":
            raise TimeoutError("networkidle timed out")

    fake_page = MagicMock()
    fake_page.goto = flaky_goto
    fake_page.content = _async_mock("<html>loaded via fallback</html>")
    fake_page.close = _async_mock(None)

    fake_browser = MagicMock()
    fake_browser.new_page = _async_mock(fake_page)

    result = await fetching_service.get_content("http://example.com/slow", browser=fake_browser)

    assert result == "<html>loaded via fallback</html>"
    assert goto_calls == ["networkidle", "load"]


@pytest.mark.asyncio
async def test_get_content_returns_empty_html_when_both_waits_fail(monkeypatch):
    monkeypatch.setattr(fetching_service, "_is_allowed", lambda url, user_agent="*": True)
    monkeypatch.setattr(fetching_service, "_wait_politely", _async_mock(None))

    async def always_fails(url, wait_until, timeout):
        raise TimeoutError(f"{wait_until} timed out")

    fake_page = MagicMock()
    fake_page.goto = always_fails
    fake_page.close = _async_mock(None)

    fake_browser = MagicMock()
    fake_browser.new_page = _async_mock(fake_page)

    result = await fetching_service.get_content("http://example.com/broken", browser=fake_browser)

    assert result == ""
    assert fetching_service.processed_urls["http://example.com/broken"] == ""


@pytest.mark.asyncio
async def test_get_content_launches_and_closes_own_browser_when_none_provided(monkeypatch):
    monkeypatch.setattr(fetching_service, "_is_allowed", lambda url, user_agent="*": True)
    monkeypatch.setattr(fetching_service, "_wait_politely", _async_mock(None))

    fake_page = MagicMock()
    fake_page.goto = _async_mock(None)
    fake_page.content = _async_mock("<html>own browser</html>")
    fake_page.close = _async_mock(None)

    stopped = []

    async def fake_stop():
        stopped.append(True)

    closed = []

    async def fake_close():
        closed.append(True)

    fake_browser = MagicMock()
    fake_browser.new_page = _async_mock(fake_page)
    fake_browser.close = fake_close

    fake_chromium = MagicMock()
    fake_chromium.launch = _async_mock(fake_browser)

    fake_playwright = MagicMock()
    fake_playwright.chromium = fake_chromium
    fake_playwright.stop = fake_stop

    class FakePlaywrightStarter:
        async def start(self):
            return fake_playwright

    monkeypatch.setattr(fetching_service, "async_playwright", lambda: FakePlaywrightStarter())

    result = await fetching_service.get_content("http://example.com/solo")

    assert result == "<html>own browser</html>"
    assert closed == [True]
    assert stopped == [True]


def _async_mock(return_value):
    async def _mock(*args, **kwargs):
        return return_value
    return _mock


class _FakePlaywrightContextManager:
    def __init__(self, playwright_obj):
        self._obj = playwright_obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *args):
        return False


# ---------------------------------------------------------------------------
# parse_queue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parse_queue_fetches_all_queued_urls_and_clears_queue(monkeypatch):
    monkeypatch.setattr(fetching_service, "_is_allowed", lambda url, user_agent="*": True)
    monkeypatch.setattr(fetching_service, "_wait_politely", _async_mock(None))

    fake_page = MagicMock()
    fake_page.goto = _async_mock(None)
    fake_page.content = _async_mock("<html>fetched</html>")
    fake_page.close = _async_mock(None)

    close_calls = []

    async def fake_close():
        close_calls.append(True)

    fake_browser = MagicMock()
    fake_browser.new_page = _async_mock(fake_page)
    fake_browser.close = fake_close

    fake_chromium = MagicMock()
    fake_chromium.launch = _async_mock(fake_browser)

    fake_playwright_obj = MagicMock()
    fake_playwright_obj.chromium = fake_chromium

    monkeypatch.setattr(
        fetching_service, "async_playwright",
        lambda: _FakePlaywrightContextManager(fake_playwright_obj),
    )

    fetching_service.url_queue = ["http://example.com/a", "http://example.com/b"]

    await fetching_service.parse_queue()

    assert fetching_service.url_queue == []
    assert fetching_service.processed_urls["http://example.com/a"] == "<html>fetched</html>"
    assert fetching_service.processed_urls["http://example.com/b"] == "<html>fetched</html>"
    assert close_calls == [True]


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def test_init_marks_successful_crawls_as_processed_when_redoing_failed(monkeypatch, tmp_path):
    csv_file = tmp_path / "starting_urls.csv"
    csv_file.write_text("http://example.com/new\n")
    fake_data_service = MagicMock()
    fake_data_service.get_finished_crawl_urls.return_value = ["http://example.com/done"]
    monkeypatch.setattr(fetching_service, "data_service", fake_data_service)

    fetching_service.init(starting_url_path=csv_file, redo_failed_fetches=True, redo_all_fetches=False)

    fake_data_service.get_finished_crawl_urls.assert_called_once()
    fake_data_service.get_crawl_urls.assert_not_called()
    assert fetching_service.processed_urls["http://example.com/done"] is None
    assert fetching_service.url_queue == ["http://example.com/new"]


def test_init_uses_all_crawled_urls_when_not_redoing_failed(monkeypatch, tmp_path):
    csv_file = tmp_path / "starting_urls.csv"
    csv_file.write_text("http://example.com/new\n")
    fake_data_service = MagicMock()
    fake_data_service.get_crawl_urls.return_value = ["http://example.com/done", "http://example.com/failed"]
    monkeypatch.setattr(fetching_service, "data_service", fake_data_service)

    fetching_service.init(starting_url_path=csv_file, redo_failed_fetches=False, redo_all_fetches=False)

    fake_data_service.get_crawl_urls.assert_called_once()
    fake_data_service.get_finished_crawl_urls.assert_not_called()
    assert set(fetching_service.processed_urls.keys()) == {
        "http://example.com/done", "http://example.com/failed"
    }


def test_init_skips_db_lookup_entirely_when_redoing_all_fetches(monkeypatch, tmp_path):
    csv_file = tmp_path / "starting_urls.csv"
    csv_file.write_text("http://example.com/new\n")
    fake_data_service = MagicMock()
    monkeypatch.setattr(fetching_service, "data_service", fake_data_service)

    fetching_service.init(starting_url_path=csv_file, redo_failed_fetches=True, redo_all_fetches=True)

    fake_data_service.get_finished_crawl_urls.assert_not_called()
    fake_data_service.get_crawl_urls.assert_not_called()
    assert fetching_service.processed_urls == {}
    assert fetching_service.url_queue == ["http://example.com/new"]


# ---------------------------------------------------------------------------
# read_starting_urls
# ---------------------------------------------------------------------------

def test_read_starting_urls_queues_each_url(tmp_path):
    csv_file = tmp_path / "starting_urls.csv"
    csv_file.write_text("http://example.com/a\nhttp://example.com/b\n")

    fetching_service._read_starting_urls(path=csv_file)

    assert fetching_service.url_queue == ["http://example.com/a", "http://example.com/b"]


def test_read_starting_urls_skips_duplicates_and_already_processed(tmp_path):
    csv_file = tmp_path / "starting_urls.csv"
    csv_file.write_text("http://example.com/a\nhttp://example.com/a\n")
    fetching_service.processed_urls["http://example.com/a"] = "<html></html>"

    fetching_service._read_starting_urls(path=csv_file)

    assert fetching_service.url_queue == []


# ---------------------------------------------------------------------------
# get_crawl_time
# ---------------------------------------------------------------------------

def test_get_crawl_time_returns_last_request_time_for_urls_domain():
    fetching_service._last_request_time["example.com"] = 123.456

    assert fetching_service.get_crawl_time("http://example.com/some/page") == 123.456


def test_get_crawl_time_returns_zero_for_unknown_domain():
    assert fetching_service.get_crawl_time("http://never-fetched.com/a") == 0.0


def test_get_crawl_time_distinguishes_between_domains():
    fetching_service._last_request_time["example.com"] = 10.0
    fetching_service._last_request_time["other.com"] = 20.0

    assert fetching_service.get_crawl_time("http://example.com/a") == 10.0
    assert fetching_service.get_crawl_time("http://other.com/b") == 20.0


# ---------------------------------------------------------------------------
# queue_url canonicalization (P2)
# ---------------------------------------------------------------------------

def test_queue_url_canonicalizes_before_queueing():
    fetching_service.queue_url("http://www.example.com/presse/")
    assert fetching_service.url_queue == ["http://example.com/presse"]


def test_queue_url_collapses_trailing_slash_duplicates():
    fetching_service.queue_url("http://example.com/presse")
    fetching_service.queue_url("http://example.com/presse/")
    assert fetching_service.url_queue == ["http://example.com/presse"]


def test_queue_url_collapses_tracking_param_duplicates():
    fetching_service.queue_url("http://example.com/a")
    fetching_service.queue_url("http://example.com/a?utm_source=news")
    assert fetching_service.url_queue == ["http://example.com/a"]


def test_queue_url_collapses_duplicate_slash_variants():
    fetching_service.queue_url("http://example.com//greifvogelhilfe")
    fetching_service.queue_url("http://example.com/greifvogelhilfe")
    assert len(fetching_service.url_queue) == 1


def test_queue_url_skips_already_processed_canonical_form():
    fetching_service.processed_urls["http://example.com/a"] = "<html></html>"
    fetching_service.queue_url("http://www.example.com/a/")
    assert fetching_service.url_queue == []


def test_queue_url_ignores_empty_url():
    fetching_service.queue_url("")
    assert fetching_service.url_queue == []


def test_queue_url_keeps_distinct_pages_separate():
    fetching_service.queue_url("http://example.com/a")
    fetching_service.queue_url("http://example.com/b")
    assert len(fetching_service.url_queue) == 2


# ---------------------------------------------------------------------------
# per-site budget
# ---------------------------------------------------------------------------

def test_no_site_budget_by_default():
    for i in range(10):
        fetching_service.queue_url(f"http://example.com/{i}")
    assert len(fetching_service.url_queue) == 10


def test_site_budget_caps_pages_per_registrable_domain(monkeypatch):
    monkeypatch.setattr(fetching_service.config, "max_pages_per_site", 3, raising=False)
    monkeypatch.setattr(fetching_service, "_site_counts", {})
    for i in range(10):
        fetching_service.queue_url(f"http://example.com/{i}")
    assert len(fetching_service.url_queue) == 3


def test_site_budget_is_per_site_not_global(monkeypatch):
    monkeypatch.setattr(fetching_service.config, "max_pages_per_site", 2, raising=False)
    monkeypatch.setattr(fetching_service, "_site_counts", {})
    for i in range(5):
        fetching_service.queue_url(f"http://a.com/{i}")
        fetching_service.queue_url(f"http://b.com/{i}")
    assert len(fetching_service.url_queue) == 4


def test_site_budget_treats_subdomains_as_one_site(monkeypatch):
    monkeypatch.setattr(fetching_service.config, "max_pages_per_site", 2, raising=False)
    monkeypatch.setattr(fetching_service, "_site_counts", {})
    fetching_service.queue_url("http://rlp.nabu.de/a")
    fetching_service.queue_url("http://www.nabu.de/b")
    fetching_service.queue_url("http://shop.nabu.de/c")
    assert len(fetching_service.url_queue) == 2


@pytest.mark.asyncio
async def test_successful_fetch_is_persisted_immediately(monkeypatch, tmp_path):
    """A batch can be thousands of pages; anything fetched but not yet written
    to disk is lost if the process stops mid-batch."""
    monkeypatch.setattr(fetching_service.page_store, "STORE_ROOT", tmp_path / "store")
    monkeypatch.setattr(fetching_service, "fetched_content", {})
    monkeypatch.setattr(fetching_service, "_is_allowed", lambda url, user_agent="*": True)

    async def no_wait(url):
        return None
    monkeypatch.setattr(fetching_service, "_wait_politely", no_wait)

    fake_page = MagicMock()
    fake_page.goto = _async_mock(None)
    fake_page.content = _async_mock("<html>persisted</html>")
    fake_page.close = _async_mock(None)
    fake_browser = MagicMock()
    fake_browser.new_page = _async_mock(fake_page)

    await fetching_service.get_content("http://example.com/p", browser=fake_browser)

    _digest, path = fetching_service.fetched_content["http://example.com/p"]
    assert fetching_service.page_store.load(path) == "<html>persisted</html>"


@pytest.mark.asyncio
async def test_blocked_fetch_stores_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(fetching_service.page_store, "STORE_ROOT", tmp_path / "store")
    monkeypatch.setattr(fetching_service, "fetched_content", {})
    monkeypatch.setattr(fetching_service, "_is_allowed", lambda url, user_agent="*": False)

    await fetching_service.get_content("http://example.com/blocked")

    assert fetching_service.fetched_content == {}


def test_denylisted_domains_are_never_queued(monkeypatch):
    monkeypatch.setattr(fetching_service.config, "domain_denylist",
                        ["google.com", "facebook.com"], raising=False)
    fetching_service.queue_url("https://www.google.com/search?q=x")
    fetching_service.queue_url("https://m.facebook.com/somepage")
    fetching_service.queue_url("https://real-station.de/kontakt")
    assert fetching_service.url_queue == ["https://real-station.de/kontakt"]


def test_denylist_matches_subdomains(monkeypatch):
    monkeypatch.setattr(fetching_service.config, "domain_denylist", ["google.com"], raising=False)
    fetching_service.queue_url("https://maps.google.com/x")
    assert fetching_service.url_queue == []


def test_empty_denylist_blocks_nothing(monkeypatch):
    monkeypatch.setattr(fetching_service.config, "domain_denylist", [], raising=False)
    fetching_service.queue_url("https://www.google.com/x")
    assert len(fetching_service.url_queue) == 1


def test_queue_url_records_priority(monkeypatch):
    monkeypatch.setattr(fetching_service, "url_priorities", {})
    fetching_service.queue_url("http://a.com/x", priority=3.5)
    assert fetching_service.url_priorities["http://a.com/x"] == 3.5


def test_requeueing_keeps_the_best_priority(monkeypatch):
    monkeypatch.setattr(fetching_service, "url_priorities", {})
    fetching_service.queue_url("http://a.com/x", priority=1.0)
    fetching_service.queue_url("http://a.com/x", priority=7.0)
    fetching_service.queue_url("http://a.com/x", priority=2.0)
    assert fetching_service.url_priorities["http://a.com/x"] == 7.0
    assert len(fetching_service.url_queue) == 1


# ---------------------------------------------------------------------------
# parse_queue resilience
#
# A 3.3-hour run ended on "Connection.init: Connection closed while reading
# from the driver" raised by async_playwright().__aenter__. The browser driver
# is a separate process and can die at any time; when it does, the crawl must
# lose that batch at worst, not the run.
# ---------------------------------------------------------------------------

def _fake_playwright_factory(fake_page):
    """Build an async_playwright() stand-in that serves the given page."""
    fake_browser = MagicMock()
    fake_browser.new_page = _async_mock(fake_page)
    fake_browser.close = _async_mock(None)
    fake_chromium = MagicMock()
    fake_chromium.launch = _async_mock(fake_browser)
    obj = MagicMock()
    obj.chromium = fake_chromium
    return _FakePlaywrightContextManager(obj)


def _working_page(html="<html>fetched</html>"):
    page = MagicMock()
    page.goto = _async_mock(None)
    page.content = _async_mock(html)
    page.close = _async_mock(None)
    return page


@pytest.mark.asyncio
async def test_parse_queue_retries_after_a_driver_failure(monkeypatch):
    monkeypatch.setattr(fetching_service, "_is_allowed", lambda url, user_agent="*": True)
    monkeypatch.setattr(fetching_service, "_wait_politely", _async_mock(None))

    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise Exception("Connection closed while reading from the driver")
        return _fake_playwright_factory(_working_page())

    monkeypatch.setattr(fetching_service, "async_playwright", flaky)
    monkeypatch.setattr(fetching_service, "_RETRY_BACKOFF_SECONDS", 0)
    fetching_service.url_queue = ["http://example.com/a"]

    await fetching_service.parse_queue()

    assert len(attempts) == 2
    assert fetching_service.processed_urls["http://example.com/a"] == "<html>fetched</html>"
    assert fetching_service.url_queue == []


@pytest.mark.asyncio
async def test_parse_queue_does_not_refetch_what_the_failed_attempt_got(monkeypatch):
    """Pages fetched before the driver died are already stored; re-fetching
    them would waste the politeness budget and hit sites twice."""
    monkeypatch.setattr(fetching_service, "_is_allowed", lambda url, user_agent="*": True)
    monkeypatch.setattr(fetching_service, "_wait_politely", _async_mock(None))

    fetched = []

    async def counting_get_content(url, browser=None):
        fetched.append(url)
        fetching_service.processed_urls[url] = "<html>x</html>"

    monkeypatch.setattr(fetching_service, "get_content", counting_get_content)

    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            # "a" was already fetched when the driver died mid-batch
            fetching_service.processed_urls["http://example.com/a"] = "<html>x</html>"
            raise Exception("driver died")
        return _fake_playwright_factory(_working_page())

    monkeypatch.setattr(fetching_service, "async_playwright", flaky)
    monkeypatch.setattr(fetching_service, "_RETRY_BACKOFF_SECONDS", 0)
    fetching_service.url_queue = ["http://example.com/a", "http://example.com/b"]

    await fetching_service.parse_queue()

    assert "http://example.com/a" not in fetched
    assert "http://example.com/b" in fetched


@pytest.mark.asyncio
async def test_parse_queue_gives_up_without_raising(monkeypatch):
    """After the last attempt the crawl must continue: process_batch() still
    has to persist and process whatever was fetched, and the run must not die
    on a transient browser fault."""
    monkeypatch.setattr(fetching_service, "_is_allowed", lambda url, user_agent="*": True)
    monkeypatch.setattr(fetching_service, "_wait_politely", _async_mock(None))

    attempts = []

    def always_fails():
        attempts.append(1)
        raise Exception("driver died")

    monkeypatch.setattr(fetching_service, "async_playwright", always_fails)
    monkeypatch.setattr(fetching_service, "_RETRY_BACKOFF_SECONDS", 0)
    fetching_service.url_queue = ["http://example.com/a"]

    await fetching_service.parse_queue()  # must not raise

    assert len(attempts) == fetching_service._FETCH_ATTEMPTS
    # the unfetched URL is left queued so a later round can retry it
    assert fetching_service.url_queue == ["http://example.com/a"]


# ---------------------------------------------------------------------------
# http/https are the same page
#
# Observed live: http://aktionsbuendnis-fuchs.de/ and
# https://aktionsbuendnis-fuchs.de/ were fetched as two separate pages, and
# the model classified one LIST and the other STATION - so the same site both
# duplicated its record and disagreed with itself about what it was.
#
# The scheme is not forced, because sites that only serve http still have to
# be fetchable; only the "have we already done this one" test ignores it.
# ---------------------------------------------------------------------------

def test_an_https_url_is_skipped_when_the_http_one_was_fetched(monkeypatch):
    monkeypatch.setattr(fetching_service, "processed_urls", {"http://a.example/": "<html/>"})
    monkeypatch.setattr(fetching_service, "url_queue", [])
    monkeypatch.setattr(fetching_service, "url_priorities", {})

    fetching_service.queue_url("https://a.example/")

    assert fetching_service.url_queue == []


def test_an_http_url_is_skipped_when_the_https_one_was_fetched(monkeypatch):
    monkeypatch.setattr(fetching_service, "processed_urls", {"https://a.example/": "<html/>"})
    monkeypatch.setattr(fetching_service, "url_queue", [])
    monkeypatch.setattr(fetching_service, "url_priorities", {})

    fetching_service.queue_url("http://a.example/")

    assert fetching_service.url_queue == []


def test_the_scheme_twin_of_a_queued_url_is_not_queued_again(monkeypatch):
    monkeypatch.setattr(fetching_service, "processed_urls", {})
    monkeypatch.setattr(fetching_service, "url_queue", [])
    monkeypatch.setattr(fetching_service, "url_priorities", {})

    fetching_service.queue_url("https://a.example/page", priority=1.0)
    fetching_service.queue_url("http://a.example/page", priority=5.0)

    assert len(fetching_service.url_queue) == 1
    # and the better score still wins, as for any other repeat offer
    assert fetching_service.url_priorities["https://a.example/page"] == 5.0


def test_different_hosts_are_still_queued_separately(monkeypatch):
    monkeypatch.setattr(fetching_service, "processed_urls", {"https://a.example/": "<html/>"})
    monkeypatch.setattr(fetching_service, "url_queue", [])
    monkeypatch.setattr(fetching_service, "url_priorities", {})

    fetching_service.queue_url("https://b.example/")

    assert fetching_service.url_queue == ["https://b.example/"]


def test_a_non_http_scheme_is_left_alone(monkeypatch):
    """The twin rule is about http/https only; it must not rewrite anything
    else it is handed."""
    monkeypatch.setattr(fetching_service, "processed_urls", {})
    monkeypatch.setattr(fetching_service, "url_queue", [])
    monkeypatch.setattr(fetching_service, "url_priorities", {})

    fetching_service.queue_url("ftp://a.example/file")

    assert fetching_service.url_queue == ["ftp://a.example/file"]


# ---------------------------------------------------------------------------
# multiple seed files
#
# Seeds arrive in themed sets - a regional directory here, a species-specific
# network there - and keeping them in separate files lets a deployment mix and
# match without editing anyone else's list.
# ---------------------------------------------------------------------------

def test_seeds_are_read_from_several_files(monkeypatch, tmp_path):
    monkeypatch.setattr(fetching_service, "processed_urls", {})
    monkeypatch.setattr(fetching_service, "url_queue", [])
    monkeypatch.setattr(fetching_service, "url_priorities", {})

    a = tmp_path / "a.csv"; a.write_text("https://a.example/\n")
    b = tmp_path / "b.csv"; b.write_text("https://b.example/\n")

    fetching_service._read_starting_urls([str(a), str(b)])

    assert "https://a.example/" in fetching_service.url_queue
    assert "https://b.example/" in fetching_service.url_queue


def test_a_single_seed_path_still_works(monkeypatch, tmp_path):
    """The config may name one file or several; both have to behave."""
    monkeypatch.setattr(fetching_service, "processed_urls", {})
    monkeypatch.setattr(fetching_service, "url_queue", [])
    monkeypatch.setattr(fetching_service, "url_priorities", {})

    a = tmp_path / "a.csv"; a.write_text("https://a.example/\n")

    fetching_service._read_starting_urls(str(a))

    assert fetching_service.url_queue == ["https://a.example/"]


def test_a_missing_seed_file_does_not_stop_the_others(monkeypatch, tmp_path):
    """A deployment that references an optional seed list it does not ship
    should still start, with the omission logged."""
    monkeypatch.setattr(fetching_service, "processed_urls", {})
    monkeypatch.setattr(fetching_service, "url_queue", [])
    monkeypatch.setattr(fetching_service, "url_priorities", {})

    a = tmp_path / "a.csv"; a.write_text("https://a.example/\n")

    fetching_service._read_starting_urls([str(a), str(tmp_path / "nope.csv")])

    assert fetching_service.url_queue == ["https://a.example/"]


def test_blank_lines_and_comments_in_a_seed_file_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(fetching_service, "processed_urls", {})
    monkeypatch.setattr(fetching_service, "url_queue", [])
    monkeypatch.setattr(fetching_service, "url_priorities", {})

    a = tmp_path / "a.csv"
    a.write_text("# regional directories\nhttps://a.example/\n\n   \nhttps://b.example/\n")

    fetching_service._read_starting_urls([str(a)])

    assert fetching_service.url_queue == ["https://a.example/", "https://b.example/"]


# ---------------------------------------------------------------------------
# recording why a fetch failed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_robots_disallowed_url_records_that_reason(monkeypatch):
    monkeypatch.setattr(fetching_service, "_is_allowed", lambda url, user_agent="*": False)
    monkeypatch.setattr(fetching_service, "processed_urls", {})
    monkeypatch.setattr(fetching_service, "fetch_errors", {})

    await fetching_service.get_content("https://a.example/blocked")

    assert "robots" in fetching_service.fetch_errors["https://a.example/blocked"].lower()


@pytest.mark.asyncio
async def test_a_navigation_failure_records_the_error(monkeypatch):
    monkeypatch.setattr(fetching_service, "_is_allowed", lambda url, user_agent="*": True)
    monkeypatch.setattr(fetching_service, "_wait_politely", _async_mock(None))
    monkeypatch.setattr(fetching_service, "processed_urls", {})
    monkeypatch.setattr(fetching_service, "fetch_errors", {})

    page = MagicMock()
    async def boom(*a, **kw):
        raise Exception("net::ERR_NAME_NOT_RESOLVED")
    page.goto = boom
    page.close = _async_mock(None)
    browser = MagicMock()
    browser.new_page = _async_mock(page)

    await fetching_service.get_content("https://gone.example/", browser=browser)

    recorded = fetching_service.fetch_errors["https://gone.example/"]
    assert "ERR_NAME_NOT_RESOLVED" in recorded


@pytest.mark.asyncio
async def test_a_successful_fetch_records_no_error(monkeypatch):
    monkeypatch.setattr(fetching_service, "_is_allowed", lambda url, user_agent="*": True)
    monkeypatch.setattr(fetching_service, "_wait_politely", _async_mock(None))
    monkeypatch.setattr(fetching_service, "processed_urls", {})
    monkeypatch.setattr(fetching_service, "fetch_errors", {})
    monkeypatch.setattr(fetching_service.page_store, "store", lambda html: (None, None))

    page = _working_page("<html>ok</html>")
    browser = MagicMock()
    browser.new_page = _async_mock(page)

    await fetching_service.get_content("https://a.example/ok", browser=browser)

    assert "https://a.example/ok" not in fetching_service.fetch_errors


# ---------------------------------------------------------------------------
# reusing stored pages instead of refetching (development runs)
# ---------------------------------------------------------------------------

@pytest.fixture
def reuse_env(tmp_path, monkeypatch):
    monkeypatch.setattr(fetching_service.page_store, "STORE_ROOT", tmp_path / "store")
    monkeypatch.setattr(fetching_service, "processed_urls", {})
    monkeypatch.setattr(fetching_service, "fetched_content", {})
    monkeypatch.setattr(fetching_service, "fetch_errors", {})
    touched = {"robots": 0, "polite": 0}
    def robots(url, user_agent="*"):
        touched["robots"] += 1
        return True
    async def polite(url):
        touched["polite"] += 1
    monkeypatch.setattr(fetching_service, "_is_allowed", robots)
    monkeypatch.setattr(fetching_service, "_wait_politely", polite)
    return touched


def _set_reuse(monkeypatch, on, max_age_days=0):
    monkeypatch.setattr(fetching_service.config, "reuse_stored_pages", on, raising=False)
    monkeypatch.setattr(fetching_service.config, "reuse_max_age_days", max_age_days, raising=False)


@pytest.mark.asyncio
async def test_a_stored_page_is_served_without_touching_the_site(monkeypatch, reuse_env):
    _set_reuse(monkeypatch, True)
    digest, path = fetching_service.page_store.store("<html>from disk</html>")
    fetching_service.page_store.remember("https://a.example/", digest, path)
    browser = MagicMock()

    html = await fetching_service.get_content("https://a.example/", browser=browser)

    assert html == "<html>from disk</html>"
    browser.new_page.assert_not_called()
    assert reuse_env == {"robots": 0, "polite": 0}   # no request of any kind
    assert fetching_service.fetched_content["https://a.example/"] == (digest, path)


@pytest.mark.asyncio
async def test_with_reuse_off_the_store_is_ignored(monkeypatch, reuse_env):
    _set_reuse(monkeypatch, False)
    digest, path = fetching_service.page_store.store("<html>from disk</html>")
    fetching_service.page_store.remember("https://a.example/", digest, path)
    browser = MagicMock()
    browser.new_page = _async_mock(_working_page("<html>live</html>"))

    html = await fetching_service.get_content("https://a.example/", browser=browser)

    assert html == "<html>live</html>"


@pytest.mark.asyncio
async def test_a_page_not_in_the_store_is_fetched_live(monkeypatch, reuse_env):
    _set_reuse(monkeypatch, True)
    browser = MagicMock()
    browser.new_page = _async_mock(_working_page("<html>live</html>"))

    html = await fetching_service.get_content("https://new.example/", browser=browser)

    assert html == "<html>live</html>"


@pytest.mark.asyncio
async def test_a_live_fetch_is_indexed_for_later_crawls(monkeypatch, reuse_env):
    _set_reuse(monkeypatch, False)
    browser = MagicMock()
    browser.new_page = _async_mock(_working_page("<html>live</html>"))

    await fetching_service.get_content("https://b.example/", browser=browser)

    assert fetching_service.page_store.lookup("https://b.example/") is not None


@pytest.mark.asyncio
async def test_a_round_served_entirely_from_the_store_launches_no_browser(monkeypatch, reuse_env):
    """Starting Chromium costs seconds per round; a development run replaying
    stored pages should not pay it at all."""
    _set_reuse(monkeypatch, True)
    for u in ("https://a.example/", "https://b.example/"):
        d, p = fetching_service.page_store.store(f"<html>{u}</html>")
        fetching_service.page_store.remember(u, d, p)
    launched = []
    monkeypatch.setattr(fetching_service, "async_playwright",
                        lambda: launched.append(1) or (_ for _ in ()).throw(AssertionError("launched")))
    monkeypatch.setattr(fetching_service, "url_queue", ["https://a.example/", "https://b.example/"])

    await fetching_service.parse_queue()

    assert launched == []
    assert fetching_service.processed_urls["https://b.example/"] == "<html>https://b.example/</html>"


def test_links_to_files_are_never_queued(monkeypatch):
    monkeypatch.setattr(fetching_service.config, "skip_url_extensions",
                        [".pdf", ".docx"], raising=False)
    fetching_service.queue_url("https://station.de/uploads/Flyer.PDF")
    fetching_service.queue_url("https://station.de/antrag.docx?download=1")
    fetching_service.queue_url("https://station.de/pdf-infos")
    assert fetching_service.url_queue == ["https://station.de/pdf-infos"]


def test_no_skipped_extensions_blocks_nothing(monkeypatch):
    monkeypatch.setattr(fetching_service.config, "skip_url_extensions", [], raising=False)
    fetching_service.queue_url("https://station.de/flyer.pdf")
    assert fetching_service.url_queue == ["https://station.de/flyer.pdf"]


# ---------------------------------------------------------------------------
# abandoning sites that yield nothing of value
# ---------------------------------------------------------------------------

def _cat(name, relevant=False):
    category = MagicMock()
    category.name = name
    category.is_relevant = relevant
    return category


@pytest.fixture
def _abandon_after_three(monkeypatch):
    monkeypatch.setattr(fetching_service, "_site_low_value", {})
    monkeypatch.setattr(fetching_service, "_productive_sites", set())
    monkeypatch.setattr(fetching_service, "abandoned_sites", set())
    monkeypatch.setattr(fetching_service.config, "abandon_site_after", 3, raising=False)
    monkeypatch.setattr(fetching_service.config, "abandon_site_max_weight", 0.5, raising=False)
    monkeypatch.setattr(fetching_service.config, "referrer_weights",
                        {"IRRELEVANT": -4.0, "COMMERCIAL": -10.0, "AUTHORITY": 0.5,
                         "ADVICE": 5.0, "STATION": 4.0}, raising=False)


def test_a_site_is_abandoned_after_enough_low_value_pages(_abandon_after_three):
    fetching_service.queue_url("https://news.example/a")
    fetching_service.queue_url("https://news.example/b")
    fetching_service.queue_url("https://station.de/kontakt")
    for page in ("x", "y", "z"):
        fetching_service.record_page_value(f"https://news.example/{page}", _cat("IRRELEVANT"))

    assert "news.example" in fetching_service.abandoned_sites
    assert fetching_service.url_queue == ["https://station.de/kontakt"]
    fetching_service.queue_url("https://www.news.example/c")
    assert fetching_service.url_queue == ["https://station.de/kontakt"]


def test_pages_worth_following_do_not_count_against_a_site(_abandon_after_three):
    for page, name in (("a", "COMMERCIAL"), ("b", "ADVICE"), ("c", "ADVICE"), ("d", "AUTHORITY")):
        fetching_service.record_page_value(f"https://mixed.example/{page}", _cat(name))
    assert "mixed.example" not in fetching_service.abandoned_sites


def test_a_site_that_yielded_a_record_is_never_abandoned(_abandon_after_three):
    fetching_service.record_page_value("https://shelter.de/", _cat("STATION", relevant=True))
    for page in ("a", "b", "c", "d"):
        fetching_service.record_page_value(f"https://shelter.de/{page}", _cat("COMMERCIAL"))
    assert "shelter.de" not in fetching_service.abandoned_sites


def test_abandoning_is_off_when_not_configured(_abandon_after_three, monkeypatch):
    monkeypatch.setattr(fetching_service.config, "abandon_site_after", 0, raising=False)
    for page in ("a", "b", "c", "d"):
        fetching_service.record_page_value(f"https://news.example/{page}", _cat("IRRELEVANT"))
    assert fetching_service.abandoned_sites == set()


def test_parse_queue_can_fetch_an_explicit_batch_without_touching_the_queue(monkeypatch):
    fetched = []

    async def fake_fetch_all(urls, max_concurrency):
        fetched.extend(urls)
        for url in urls:
            fetching_service.processed_urls[url] = "<html/>"

    monkeypatch.setattr(fetching_service, "_fetch_all", fake_fetch_all)
    fetching_service.url_queue = ["http://later.example/"]

    asyncio.run(fetching_service.parse_queue(urls=["http://now.example/"]))

    assert fetched == ["http://now.example/"]
    assert fetching_service.url_queue == ["http://later.example/"]


# ---------------------------------------------------------------------------
# a page that never returns must not stop the crawl
# ---------------------------------------------------------------------------

def test_a_fetch_that_never_returns_is_abandoned(monkeypatch):
    # A wedged browser page stalled one overnight run for eleven hours: the
    # per-navigation timeout does not bound the whole fetch.
    monkeypatch.setattr(fetching_service.config, "fetch_timeout_seconds", 0.05, raising=False)
    fetching_service.fetch_errors.clear()

    async def never_returns(url, browser=None):
        await asyncio.sleep(30)

    monkeypatch.setattr(fetching_service, "get_content", never_returns)

    async def run():
        await fetching_service._fetch_all(["http://slow.example/"], 2, browser=object())

    asyncio.run(asyncio.wait_for(run(), timeout=5))

    assert "timed out" in fetching_service.fetch_errors["http://slow.example/"].lower()
    assert fetching_service.processed_urls.get("http://slow.example/") == ""


def test_a_slow_page_does_not_hold_up_the_others(monkeypatch):
    monkeypatch.setattr(fetching_service.config, "fetch_timeout_seconds", 0.05, raising=False)
    done = []

    async def one_slow(url, browser=None):
        if "slow" in url:
            await asyncio.sleep(30)
        done.append(url)

    monkeypatch.setattr(fetching_service, "get_content", one_slow)

    async def run():
        await fetching_service._fetch_all(
            ["http://slow.example/", "http://quick.example/"], 2, browser=object())

    asyncio.run(asyncio.wait_for(run(), timeout=5))

    assert done == ["http://quick.example/"]


# ---------------------------------------------------------------------------
# closeness to an interesting page, carried along links
# ---------------------------------------------------------------------------

def test_queueing_records_how_close_a_url_is_to_something_interesting():
    fetching_service.url_closeness.clear()
    fetching_service.queue_url("http://a.com/x", priority=1.0, closeness=2.0)
    assert fetching_service.closeness_of("http://a.com/x") == 2.0


def test_a_shorter_path_raises_a_urls_closeness_and_priority():
    fetching_service.url_closeness.clear()
    fetching_service.queue_url("http://a.com/x", priority=0.5, closeness=0.25)
    fetching_service.queue_url("http://a.com/x", priority=2.5, closeness=2.0)

    assert fetching_service.closeness_of("http://a.com/x") == 2.0
    assert fetching_service.url_priorities["http://a.com/x"] == 2.5


def test_a_longer_path_does_not_lower_what_is_already_known():
    fetching_service.url_closeness.clear()
    fetching_service.queue_url("http://a.com/x", priority=2.5, closeness=2.0)
    fetching_service.queue_url("http://a.com/x", priority=0.5, closeness=0.25)

    assert fetching_service.closeness_of("http://a.com/x") == 2.0


def test_closeness_is_kept_under_the_canonical_url():
    fetching_service.url_closeness.clear()
    fetching_service.queue_url("http://a.com/x?utm_source=news#top", priority=1.0, closeness=1.5)
    assert fetching_service.closeness_of("http://a.com/x") == 1.5


# ---------------------------------------------------------------------------
# abandoning a site: ban, or demote?
# ---------------------------------------------------------------------------

def test_an_abandoned_site_is_refused_by_default(monkeypatch):
    fetching_service.abandoned_sites.add("dull.example")
    monkeypatch.setattr(fetching_service.config, "abandoned_site_penalty", 0.0)

    fetching_service.queue_url("https://dull.example/page", priority=5.0)

    assert fetching_service.url_queue == []


def test_with_a_penalty_an_abandoned_site_is_demoted_instead(monkeypatch):
    # Bergmark et al. 2002: pages relevant to one topic are separated by 1 to
    # 12 irrelevant ones, so a crawler that refuses a failed host never crosses
    # between clusters. A demoted link drains from the queue last, which gets
    # the same effect, and a strong enough referrer can still pull one through.
    fetching_service.abandoned_sites.add("dull.example")
    monkeypatch.setattr(fetching_service.config, "abandoned_site_penalty", 20.0)

    fetching_service.queue_url("https://dull.example/page", priority=5.0)

    assert fetching_service.url_queue == ["https://dull.example/page"]
    assert fetching_service.url_priorities["https://dull.example/page"] == -15.0


def test_abandoning_a_site_demotes_what_it_already_queued(monkeypatch):
    monkeypatch.setattr(fetching_service.config, "abandoned_site_penalty", 20.0)
    fetching_service.queue_url("https://dull.example/a", priority=4.0)
    fetching_service.queue_url("https://good.example/b", priority=4.0)

    fetching_service.abandon_site("dull.example")

    assert set(fetching_service.url_queue) == {"https://dull.example/a",
                                               "https://good.example/b"}
    assert fetching_service.url_priorities["https://dull.example/a"] == -16.0
    assert fetching_service.url_priorities["https://good.example/b"] == 4.0


def test_abandoning_a_site_still_drops_its_queue_without_a_penalty(monkeypatch):
    monkeypatch.setattr(fetching_service.config, "abandoned_site_penalty", 0.0)
    fetching_service.queue_url("https://dull.example/a", priority=4.0)

    fetching_service.abandon_site("dull.example")

    assert fetching_service.url_queue == []


# ---------------------------------------------------------------------------
# taking the frontier out and putting it back
# ---------------------------------------------------------------------------

def test_a_snapshot_carries_each_url_with_its_score():
    fetching_service.queue_url("https://a.example/", priority=5.0, closeness=1.5)
    fetching_service.queue_url("https://b.example/", priority=-2.0)

    assert fetching_service.snapshot_frontier() == [
        ("https://a.example/", 5.0, 1.5), ("https://b.example/", -2.0, 0.0)]


def test_a_restored_frontier_is_queued_with_its_scores():
    fetching_service.restore_frontier([("https://a.example/", 5.0, 1.5),
                                       ("https://b.example/", -2.0, 0.0)])

    assert fetching_service.url_queue == ["https://a.example/", "https://b.example/"]
    assert fetching_service.url_priorities["https://a.example/"] == 5.0
    assert fetching_service.closeness_of("https://a.example/") == 1.5


def test_restoring_does_not_requeue_what_has_already_been_processed():
    fetching_service.processed_urls["https://done.example/"] = "<html></html>"

    fetching_service.restore_frontier([("https://done.example/", 5.0, 0.0),
                                       ("https://new.example/", 1.0, 0.0)])

    assert fetching_service.url_queue == ["https://new.example/"]
