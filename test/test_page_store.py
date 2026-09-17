import gzip

import pytest

from model.tools import page_store


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(page_store, "STORE_ROOT", tmp_path / "store")
    yield


def test_store_returns_digest_and_relative_path():
    digest, path = page_store.store("<html>hello</html>")
    assert len(digest) == 64
    assert digest in path


def test_stored_content_round_trips():
    _digest, path = page_store.store("<html>hällo ß</html>")
    assert page_store.load(path) == "<html>hällo ß</html>"


def test_identical_content_shares_one_file():
    d1, p1 = page_store.store("<html>same</html>")
    d2, p2 = page_store.store("<html>same</html>")
    assert d1 == d2 and p1 == p2
    assert len(list(page_store.STORE_ROOT.rglob("*.gz"))) == 1


def test_different_content_gets_different_files():
    _d1, p1 = page_store.store("<html>a</html>")
    _d2, p2 = page_store.store("<html>b</html>")
    assert p1 != p2
    assert len(list(page_store.STORE_ROOT.rglob("*.gz"))) == 2


def test_content_is_actually_compressed_on_disk():
    _digest, path = page_store.store("<html>" + "x" * 5000 + "</html>")
    full = page_store.absolute_path(path)
    assert full.stat().st_size < 5000
    with gzip.open(full, "rt", encoding="utf-8") as f:
        assert f.read().startswith("<html>")


def test_exists_reports_presence():
    _digest, path = page_store.store("<html>x</html>")
    assert page_store.exists(path) is True
    assert page_store.exists("store/00/00/deadbeef.html.gz") is False


def test_load_missing_path_returns_none():
    assert page_store.load("store/00/00/missing.html.gz") is None


def test_store_empty_content_returns_none():
    assert page_store.store("") == (None, None)


def test_no_temp_files_left_behind():
    page_store.store("<html>x</html>")
    assert list(page_store.STORE_ROOT.rglob("*.tmp")) == []


def test_store_is_sharded_not_one_flat_directory():
    _digest, path = page_store.store("<html>x</html>")
    # <root>/ab/cd/<digest>.html.gz  -> two shard levels
    assert len(page_store.absolute_path(path).relative_to(page_store.STORE_ROOT).parts) == 3


# ---------------------------------------------------------------------------
# finding a stored page by URL, for reuse across crawls
#
# A development run against a fresh database refetched every page it had
# already fetched before, hitting the same sites again for nothing. The bodies
# were on disk all along, but addressed by content hash, so a new crawl had no
# way to find them. The index lives inside the store rather than in a crawl
# database, which is what lets a crawl with an empty database use it.
# ---------------------------------------------------------------------------

def test_a_remembered_page_is_found_by_url():
    digest, path = page_store.store("<html>kept</html>")
    page_store.remember("https://a.example/", digest, path)

    assert page_store.lookup("https://a.example/") == (digest, path)


def test_an_unknown_url_is_not_found():
    assert page_store.lookup("https://never.example/") is None


def test_remembering_again_replaces_the_older_body():
    old = page_store.store("<html>old</html>")
    new = page_store.store("<html>new</html>")
    page_store.remember("https://a.example/", *old)
    page_store.remember("https://a.example/", *new)

    assert page_store.lookup("https://a.example/") == new


def test_a_page_older_than_the_limit_is_not_reused():
    digest, path = page_store.store("<html>stale</html>")
    page_store.remember("https://a.example/", digest, path, fetched_at=1000.0)

    assert page_store.lookup("https://a.example/", max_age_seconds=60, now=5000.0) is None
    assert page_store.lookup("https://a.example/", max_age_seconds=10_000, now=5000.0) == (digest, path)


def test_an_index_entry_whose_file_is_gone_is_not_returned():
    """Better a refetch than handing back a path that cannot be read."""
    digest, path = page_store.store("<html>gone</html>")
    page_store.remember("https://a.example/", digest, path)
    page_store.absolute_path(path).unlink()

    assert page_store.lookup("https://a.example/") is None


def test_the_index_can_be_seeded_from_an_existing_crawl_database(tmp_path):
    """Pages fetched before the index existed are only recorded in old crawl
    databases; importing them makes that whole history reusable."""
    import sqlite3
    digest, path = page_store.store("<html>historic</html>")
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE crawls (source_url TEXT, content_path TEXT, content_sha256 TEXT)")
        c.execute("INSERT INTO crawls VALUES (?, ?, ?)", ("https://h.example/", path, digest))
        c.execute("INSERT INTO crawls VALUES (?, ?, ?)", ("https://failed.example/", None, None))

    imported = page_store.index_database(db)

    assert imported == 1
    assert page_store.lookup("https://h.example/") == (digest, path)
