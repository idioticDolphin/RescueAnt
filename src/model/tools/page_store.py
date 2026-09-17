"""
Durable, content-addressed storage for fetched pages.

Fetching is by far the slowest and least repeatable part of a crawl, yet the
pipeline previously held page HTML only in memory: an interrupted run threw
away every page it had fetched but not yet processed. Storing the raw page
makes a run resumable, and makes re-processing (new prompt, new model, new
schema) possible without touching the network at all.

Files are named by the SHA-256 of their content, so two URLs serving the same
bytes share one file, and a stored page can always be verified against its
digest.

Write ordering matters and is deliberate: content is written to a temporary
file, flushed and fsync'd, then atomically renamed into place. Only after
that does the caller record the path in the database. A crash can therefore
leave an unreferenced file (harmless, and removable by a later sweep) but can
never leave a database row pointing at a file that does not exist.
"""
import gzip
import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Overridden from config by orchestrator.init(); a module-level default keeps
# the store usable (and testable) without a full Config present.
STORE_ROOT = Path("store")


def configure(root):
    """Point the store at `root` (called once at startup from the config)."""
    global STORE_ROOT
    STORE_ROOT = Path(root)


def _relative_path(digest: str) -> str:
    """Shard by the first four hex characters so no directory gets huge."""
    return f"{digest[:2]}/{digest[2:4]}/{digest}.html.gz"


def absolute_path(relative: str) -> Path:
    """Resolve a stored relative path against the configured store root."""
    return STORE_ROOT / relative


def store(html: str):
    """
    Persist page content and return (sha256_hex, relative_path).

    Returns (None, None) for empty content - there is nothing to store for a
    failed fetch, and the caller records the failure in the database instead.
    Storing the same content twice is a no-op that returns the same path.
    """
    if not html:
        return None, None

    encoded = html.encode("utf-8", errors="replace")
    digest = hashlib.sha256(encoded).hexdigest()
    relative = _relative_path(digest)
    target = absolute_path(relative)

    if target.exists():
        return digest, relative

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        # fsync the write handle before publishing under the final name, so a
        # crash can never expose a partially written file. (fsync must be
        # called on the writable descriptor - on Windows a read-only handle
        # raises EBADF.)
        with open(temporary, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb") as compressed:
                compressed.write(encoded)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, target)
    except Exception as e:
        logger.warning("Failed to store page content (%s)", e)
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass
        return None, None

    return digest, relative


def load(relative: str):
    """Return stored content for a relative path, or None if it is missing."""
    if not relative:
        return None
    path = absolute_path(relative)
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("Failed to read stored page %s (%s)", relative, e)
        return None


def exists(relative: str) -> bool:
    """True if a stored page is present on disk."""
    return bool(relative) and absolute_path(relative).is_file()


# ---------------------------------------------------------------------------
# URL index
#
# Bodies are addressed by content hash, which is what lets two URLs share one
# file - and also what left a new crawl unable to find anything: the only
# record of which URL a body came from lived in the crawl database that
# fetched it. A development run against a fresh database therefore refetched
# every page it had fetched before.
#
# This index lives inside the store directory, so the store describes itself
# and a crawl with an empty database can still use it.
# ---------------------------------------------------------------------------

import sqlite3  # noqa: E402
import time  # noqa: E402

_INDEX_NAME = "urls.sqlite"


def _index():
    STORE_ROOT.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(STORE_ROOT / _INDEX_NAME)
    connection.execute("""CREATE TABLE IF NOT EXISTS pages (
        url TEXT PRIMARY KEY, sha256 TEXT, path TEXT NOT NULL, fetched_at REAL)""")
    return connection


def remember(url: str, digest: str, relative: str, fetched_at: float = None):
    """Record that `url` was fetched as the stored body at `relative`.

    A later fetch of the same URL replaces the entry, so lookups return the
    most recent body."""
    if not url or not relative:
        return
    with _index() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO pages (url, sha256, path, fetched_at) VALUES (?, ?, ?, ?)",
            (url, digest, relative, time.time() if fetched_at is None else fetched_at))


def lookup(url: str, max_age_seconds: float = None, now: float = None):
    """Return (sha256, relative_path) of the stored body for `url`, or None.

    None when the URL was never stored, when its entry is older than
    max_age_seconds, or when the file it names has gone - a refetch is better
    than handing back a path that cannot be read."""
    if not url or not (STORE_ROOT / _INDEX_NAME).exists():
        return None
    with _index() as connection:
        row = connection.execute(
            "SELECT sha256, path, fetched_at FROM pages WHERE url = ?", (url,)).fetchone()
    if row is None:
        return None
    digest, relative, fetched_at = row
    if max_age_seconds and fetched_at is not None:
        if (time.time() if now is None else now) - fetched_at > max_age_seconds:
            return None
    if not exists(relative):
        return None
    return digest, relative


def index_database(db_path) -> int:
    """Seed the index from an existing crawl database and return the count.

    Pages fetched before the index existed are recorded only in the crawl
    databases that fetched them. The file's modification time stands in for
    when it was fetched, since the crawl database's own timestamps are
    monotonic-clock values with no fixed origin."""
    imported = 0
    with sqlite3.connect(db_path) as source:
        rows = source.execute(
            """SELECT source_url, content_sha256, content_path FROM crawls
               WHERE content_path IS NOT NULL""").fetchall()
    for url, digest, relative in rows:
        if not exists(relative):
            continue
        remember(url, digest, relative,
                 fetched_at=absolute_path(relative).stat().st_mtime)
        imported += 1
    return imported


def indexed_count() -> int:
    """How many distinct URLs the index can serve."""
    if not (STORE_ROOT / _INDEX_NAME).exists():
        return 0
    with _index() as connection:
        return connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
