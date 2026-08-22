"""
Turns a model.tools.monitor_service session log (sessions/session_*.jsonl,
written continuously while python src/main.py runs) into per-round and
per-discovery-batch CSVs, for analyzing a *real* crawl session: how many
URLs were fetched/failed per round, how round composition shifted between
useful (STATION/LIST) and IRRELEVANT categories, how round size changed
over the run, and how much each discovery batch actually contributed.

Fetch/failure/category counts are not stored in the session log itself -
monitor_service only records round_start/round_end timestamps (see its
docstring for why). This script re-derives those counts by reading the
crawls table (model.tools.data_service) and binning each crawl's crawl_time
into the round window it falls into. That keeps the crawls table the single
source of truth for that data instead of a second, potentially-diverging
copy of it living in the log.

Timestamp note: crawl_time is time.monotonic(), not wall-clock (see
fetching_service.get_crawl_time()) - which is exactly what monitor_service's
"monotonic_time" field also is, so the two are directly comparable without
any timezone/clock-skew handling.

Interrupted-session handling: if the log has no session_end (Ctrl-C, crash,
kill), or the last round has no matching round_end, the last round's window
is left open-ended - it's given every crawl at or after its round_start,
instead of being dropped. That's the whole point of monitor_service logging
continuously rather than only at the end (see its docstring): a session cut
short still produces a usable partial report.

Usage: python experiments/analyze_session.py [session_log.jsonl] [db_path]

session_log.jsonl defaults to the most recent file under sessions/ (as
written by monitor_service.start_session(), which is the default location
model.orchestrator.run() writes to when started from src/main.py). db_path
defaults to the active bot.config's `database` setting.

Output: experiments/data/<session_log_stem>_rounds.csv and
_discovery.csv, plus a summary printed to stdout.
"""
import json
import sqlite3
import sys
from pathlib import Path

from common import DATA_DIR, configure_logging, write_csv

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_SESSIONS_DIR = PROJECT_ROOT / "sessions"


def _find_latest_session_log(sessions_dir: Path) -> Path:
    logs = sorted(sessions_dir.glob("session_*.jsonl"))
    if not logs:
        raise FileNotFoundError(f"No session_*.jsonl files found in {sessions_dir}")
    return logs[-1]


def _read_events(path: Path) -> list[dict]:
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _round_windows(events: list[dict]) -> list[dict]:
    """
    Pair up round_start/round_end events into [start, end) monotonic windows.
    If a round's round_end is missing (session was interrupted mid-round),
    its window is left open (end=None, meaning "till the end of the log").
    """
    starts = {e["round"]: e for e in events if e["event"] == "round_start"}
    ends = {e["round"]: e for e in events if e["event"] == "round_end"}
    windows = []
    for round_number in sorted(starts):
        start = starts[round_number]
        end = ends.get(round_number)
        windows.append({
            "round": round_number,
            "queued": start["queued"],
            "start_mono": start["monotonic_time"],
            "end_mono": end["monotonic_time"] if end else None,
        })
    return windows


def _discovery_rows(events: list[dict]) -> list[dict]:
    """
    One row per discovery event, with batch_seconds approximated as the gap
    since the *previous* logged event (of any kind) - monitor_service has no
    separate discovery-batch-started marker, so this is the closest available
    proxy for how long that batch took.
    """
    rows = []
    prev_mono = None
    for e in events:
        if e["event"] != "discovery":
            prev_mono = e["monotonic_time"]
            continue
        batch_seconds = e["monotonic_time"] - prev_mono if prev_mono is not None else None
        rows.append({
            "discovery_batch": e["discovery_batch"],
            "queries_used": e["queries_used"],
            "discovered_unique": e["discovered_unique"],
            "newly_queued": e["newly_queued"],
            "queries_remaining": e["queries_remaining"],
            "batch_seconds": round(batch_seconds, 3) if batch_seconds is not None else "",
        })
        prev_mono = e["monotonic_time"]
    return rows


def _load_crawls(db_path: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT crawl_time, fetch_success, category FROM crawls"
        ).fetchall()
    finally:
        connection.close()


def _bin_crawls_into_rounds(windows: list[dict], crawls: list[sqlite3.Row]):
    """
    Assign each crawl row to the round window containing its crawl_time.
    Returns (per-round stats dict keyed by round number, list of crawls that
    matched no window - e.g. crawl_time == 0.0, which fetching_service
    records for URLs skipped via robots.txt before any request timestamp
    was ever set for their domain).
    """
    stats = {
        w["round"]: {"fetched": 0, "fetch_success": 0, "fetch_failed": 0, "categories": {}}
        for w in windows
    }
    unassigned = []
    for row in crawls:
        crawl_time = row["crawl_time"]
        matched = None
        for w in windows:
            if crawl_time is None:
                break
            if crawl_time < w["start_mono"]:
                continue
            if w["end_mono"] is not None and crawl_time >= w["end_mono"]:
                continue
            matched = w["round"]
            break
        if matched is None:
            unassigned.append(row)
            continue
        s = stats[matched]
        s["fetched"] += 1
        if row["fetch_success"]:
            s["fetch_success"] += 1
        else:
            s["fetch_failed"] += 1
        category = row["category"] or "(uncategorized)"
        s["categories"][category] = s["categories"].get(category, 0) + 1
    return stats, unassigned


def main():
    configure_logging()
    import logging
    logger = logging.getLogger("experiments.analyze_session")

    session_log = Path(sys.argv[1]) if len(sys.argv) > 1 else _find_latest_session_log(DEFAULT_SESSIONS_DIR)

    if len(sys.argv) > 2:
        db_path = Path(sys.argv[2])
    else:
        # Deliberately read the raw config dict (config_service._read_config())
        # rather than config_service.get_config() - the latter builds a fully
        # validated Config, which loads every configured LLM model as a side
        # effect (see model.tools.llm_service.get_model_id()). This script
        # only needs the database path string, so that cost isn't worth paying.
        import model.tools.config_service as config_service
        db_path = PROJECT_ROOT / config_service._read_config()["database"]

    logger.info("Reading session log %s", session_log)
    events = _read_events(session_log)
    windows = _round_windows(events)
    discovery_rows = _discovery_rows(events)

    logger.info("Reading crawls from %s", db_path)
    crawls = _load_crawls(db_path)
    round_stats, unassigned = _bin_crawls_into_rounds(windows, crawls)
    if unassigned:
        logger.warning(
            "%d crawl(s) fell outside every round window (likely robots.txt-disallowed "
            "URLs, whose domain never got a real request timestamp) - excluded from per-round stats",
            len(unassigned),
        )

    all_categories = sorted({c for s in round_stats.values() for c in s["categories"]})
    round_fieldnames = ["round", "queued", "round_seconds", "fetched", "fetch_success", "fetch_failed"] + \
                       [f"cat_{c}" for c in all_categories]
    round_rows = []
    for w in windows:
        s = round_stats[w["round"]]
        round_seconds = (w["end_mono"] - w["start_mono"]) if w["end_mono"] is not None else ""
        row = {
            "round": w["round"], "queued": w["queued"],
            "round_seconds": round(round_seconds, 3) if round_seconds != "" else "",
            "fetched": s["fetched"], "fetch_success": s["fetch_success"], "fetch_failed": s["fetch_failed"],
        }
        for c in all_categories:
            row[f"cat_{c}"] = s["categories"].get(c, 0)
        round_rows.append(row)

    discovery_fieldnames = ["discovery_batch", "queries_used", "discovered_unique", "newly_queued",
                             "queries_remaining", "batch_seconds"]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rounds_out = DATA_DIR / f"{session_log.stem}_rounds.csv"
    discovery_out = DATA_DIR / f"{session_log.stem}_discovery.csv"
    write_csv(rounds_out, round_rows, round_fieldnames)
    write_csv(discovery_out, discovery_rows, discovery_fieldnames)
    logger.info("Wrote %d round row(s) to %s", len(round_rows), rounds_out)
    logger.info("Wrote %d discovery row(s) to %s", len(discovery_rows), discovery_out)

    total_fetched = sum(s["fetched"] for s in round_stats.values())
    total_failed = sum(s["fetch_failed"] for s in round_stats.values())
    total_by_category = {}
    for s in round_stats.values():
        for c, n in s["categories"].items():
            total_by_category[c] = total_by_category.get(c, 0) + n
    total_newly_queued = sum(r["newly_queued"] for r in discovery_rows)

    print("\n=== Session summary ===")
    print(f"Session log:        {session_log}")
    print(f"Rounds:             {len(round_rows)}")
    print(f"URLs fetched:       {total_fetched} ({total_failed} failed)")
    print(f"Categories:         {dict(sorted(total_by_category.items()))}")
    print(f"Discovery batches:  {len(discovery_rows)} ({total_newly_queued} URL(s) newly queued total)")
    if unassigned:
        print(f"Unassigned crawls:  {len(unassigned)} (see log warning above)")
    session_end = next((e for e in events if e["event"] == "session_end"), None)
    if session_end:
        print(f"Stop reason:        {session_end['stop_reason']} ({session_end['elapsed_seconds']}s elapsed)")
    else:
        print("Stop reason:        (no session_end event - session appears to have been interrupted)")


if __name__ == "__main__":
    main()
