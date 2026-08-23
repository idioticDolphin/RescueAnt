"""
Continuous, crash-safe session monitoring for real-condition crawl runs.

model.orchestrator writes two kinds of markers here as a run progresses:

  - round_start/round_end: bracket every model.orchestrator.process_batch()
    call with a timestamp. Deliberately doesn't duplicate the fetch/failure/
    category counts already recorded per-crawl in the crawls table
    (model.tools.data_service) - experiments/analyze_session.py re-derives
    those by binning crawls.crawl_time into these round windows, so the
    crawls table stays the single source of truth for that data and this
    log can't drift out of sync with it.
  - discovery: one line per model.orchestrator.run_discovery() call,
    recording what the database alone can't reconstruct - how many URLs a
    discovery batch turned up and how many were newly queued, since
    already-queued/already-crawled URLs are filtered out before they'd ever
    reach the crawls table.
  - page: one line per page process_batch() categorizes, timing the
    categorize_website() and extract_information() calls individually. This
    also isn't reconstructable from the database (crawls only records
    crawl_time, not how long categorization/extraction took), and it's the
    only source for "how much time did this round spend on productive vs.
    irrelevant pages" / "LIST vs. STATION extraction time" style analysis -
    see experiments/analyze_session.py and notebooks/session_monitoring.ipynb.

Every event is appended to a JSON-lines file and flushed immediately - not
batched in memory for the run's duration - so a session interrupted by
Ctrl-C, a crash, or a killed process still leaves usable data behind (see
experiments/analyze_session.py, the companion analysis script, which is
also written to tolerate a log with no closing session_end/round_end).

Timestamps: model.crawler.fetching_service records each crawl's crawl_time
as time.monotonic() (see fetching_service.get_crawl_time()), not wall-clock
time - a monotonic clock with an arbitrary epoch that's only meaningful
relative to another time.monotonic() call in the same process run. For
round windows to be usable as bin boundaries against crawl_time, events
here are timestamped the same way (the "monotonic_time" field); a wall-clock
ISO timestamp is included too, but only for human-readability in the raw log.
"""
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_file = None
_log_path: Path | None = None
_round = 0
_discovery_batch = 0


def _wall_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(record: dict):
    if _file is None:
        return  # monitoring not started (or failed to start) - callers don't need to guard every call
    _file.write(json.dumps(record) + "\n")
    _file.flush()


def start_session(log_dir: str = "sessions") -> Path | None:
    """
    Open a new session log file (named after the current time) and write a
    session_start event to it. round_start()/round_end()/discovery() are
    no-ops until this has been called (or if it failed to open the file).

    :return: the path to the opened log file, or None if it couldn't be opened.
    """
    global _file, _log_path, _round, _discovery_batch
    _round = 0
    _discovery_batch = 0
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        _log_path = Path(log_dir) / f"session_{stamp}.jsonl"
        # line-buffered + flush() after every write below, so events survive
        # an interruption instead of sitting in an OS/library buffer
        _file = open(_log_path, "a", buffering=1, encoding="utf-8")
    except OSError:
        logger.warning("Could not open session log in %r - session monitoring disabled", log_dir)
        _file = None
        _log_path = None
        return None
    _write({"event": "session_start", "wall_time": _wall_time(), "monotonic_time": time.monotonic()})
    logger.info("Session monitoring: writing to %s", _log_path)
    return _log_path


def round_start(queued: int) -> int:
    """
    Mark the start of a new round (one process_batch() call).

    :param queued: number of URLs about to be fetched this round.
    :return: the round number assigned to this round.
    """
    global _round
    _round += 1
    _write({
        "event": "round_start", "round": _round,
        "wall_time": _wall_time(), "monotonic_time": time.monotonic(),
        "queued": queued,
    })
    return _round


def round_end():
    """Mark the end of the current round (the one started by the last round_start() call)."""
    _write({
        "event": "round_end", "round": _round,
        "wall_time": _wall_time(), "monotonic_time": time.monotonic(),
    })


def page(url: str, category: str | None, categorize_seconds: float, extract_seconds: float):
    """
    Record one page's categorize/extract timing, tied to the current round
    (the one started by the last round_start() call).

    :param url: the page's URL
    :param category: the category name it was classified as, or None if
                      categorization itself failed (see
                      model.analyzer.category_service.categorize_website())
    :param categorize_seconds: wall-clock time spent in categorize_website()
    :param extract_seconds: wall-clock time spent in extract_information() -
                             0.0 for pages categorization failed on (no
                             extraction attempted) and near-0 for irrelevant
                             categories (extract_information() returns
                             immediately without calling the LLM)
    """
    _write({
        "event": "page", "round": _round,
        "wall_time": _wall_time(), "monotonic_time": time.monotonic(),
        "url": url, "category": category,
        "categorize_seconds": round(categorize_seconds, 3),
        "extract_seconds": round(extract_seconds, 3),
    })


def discovery(queries_used: int, discovered_unique: int, newly_queued: int, queries_remaining: int):
    """Record the outcome of one run_discovery() call."""
    global _discovery_batch
    _discovery_batch += 1
    _write({
        "event": "discovery", "discovery_batch": _discovery_batch,
        "wall_time": _wall_time(), "monotonic_time": time.monotonic(),
        "queries_used": queries_used,
        "discovered_unique": discovered_unique,
        "newly_queued": newly_queued,
        "queries_remaining": queries_remaining,
    })


def session_end(rounds: int, discovery_batches: int, elapsed_seconds: float, stop_reason: str):
    """Mark the end of the session and close the log file."""
    _write({
        "event": "session_end",
        "wall_time": _wall_time(), "monotonic_time": time.monotonic(),
        "rounds": rounds, "discovery_batches": discovery_batches,
        "elapsed_seconds": round(elapsed_seconds, 3), "stop_reason": stop_reason,
    })
    global _file
    if _file:
        _file.close()
        _file = None
