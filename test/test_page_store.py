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
