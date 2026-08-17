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

def test_is_allowed_true_when_robots_parser_permits(monkeypatch):
    fake_rp = MagicMock()
    fake_rp.can_fetch.return_value = True
    monkeypatch.setattr(fetching_service, "RobotFileParser", lambda: fake_rp)

    assert fetching_service._is_allowed("http://example.com/page") is True


def test_is_allowed_false_when_robots_parser_denies(monkeypatch):
    fake_rp = MagicMock()
    fake_rp.can_fetch.return_value = False
    monkeypatch.setattr(fetching_service, "RobotFileParser", lambda: fake_rp)

    assert fetching_service._is_allowed("http://example.com/page") is False


def test_is_allowed_defaults_to_true_when_robots_txt_unreachable(monkeypatch):
    class ExplodingRobotFileParser:
        def set_url(self, url):
            pass

        def read(self):
            raise OSError("connection refused")

    monkeypatch.setattr(fetching_service, "RobotFileParser", ExplodingRobotFileParser)

    assert fetching_service._is_allowed("http://example.com/page") is True


def test_is_allowed_caches_parser_per_domain(monkeypatch):
    call_count = {"n": 0}

    def make_rp():
        call_count["n"] += 1
        rp = MagicMock()
        rp.can_fetch.return_value = True
        return rp

    monkeypatch.setattr(fetching_service, "RobotFileParser", make_rp)

    fetching_service._is_allowed("http://example.com/a")
    fetching_service._is_allowed("http://example.com/b")

    assert call_count["n"] == 1  # second call reused the cached parser for the same domain


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


def _async_mock(return_value):
    async def _mock(*args, **kwargs):
        return return_value
    return _mock


# ---------------------------------------------------------------------------
# read_starting_urls
# ---------------------------------------------------------------------------

def test_read_starting_urls_queues_each_url(tmp_path):
    csv_file = tmp_path / "starting_urls.csv"
    csv_file.write_text("http://example.com/a\nhttp://example.com/b\n")

    fetching_service.read_starting_urls(path=csv_file)

    assert fetching_service.url_queue == ["http://example.com/a", "http://example.com/b"]


def test_read_starting_urls_skips_duplicates_and_already_processed(tmp_path):
    csv_file = tmp_path / "starting_urls.csv"
    csv_file.write_text("http://example.com/a\nhttp://example.com/a\n")
    fetching_service.processed_urls["http://example.com/a"] = "<html></html>"

    fetching_service.read_starting_urls(path=csv_file)

    assert fetching_service.url_queue == []
