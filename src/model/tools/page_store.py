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
