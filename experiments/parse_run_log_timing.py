"""
Reconstructs per-page categorize_seconds/extract_seconds (and per-round fetch-phase
seconds) from a raw text log of a real model.orchestrator run - for real crawl
sessions that predate model.tools.monitor_service.page() being wired into
process_batch() (see that function's docstring and
experiments/analyze_session.py's page-timing notes), where the *only* surviving
per-page timing signal is the ordinary log output itself.

Why this is possible at all: process_batch()'s categorize loop and extract loop are
each a straight-line sequence of one page after another - categorize_website() calls
are never interleaved with extract_information() calls - and every page's outcome is
logged exactly once per phase:
  categorize phase: "Categorized %s as %s" (INFO) or
                     "Skipping %s - categorization failed" (WARNING)
  extract phase:    "Extracted %d entries from %s" (INFO, LIST-like categories),
                     "Extracted %d field(s) from %s" (INFO, non-list categories), or
                     "No data extracted from %s (category=%s)" (DEBUG)
The elapsed time between two consecutive per-page log lines in the same phase IS that
page's categorize_website()/extract_information() call duration - no separate timing
instrumentation needed, just the ordinary log output (captured at DEBUG level, so the
extract phase's "no data" case - which covers IRRELEVANT pages, since
extract_information() returns near-instantly without calling the LLM for those - is
present too).

Log line timestamps are HH:MM:SS only (no date) - a run spanning past midnight is
handled by detecting any timestamp smaller than the previous one and adding a day
(86400s) to every timestamp from that point on.

Round boundaries: "Processing batch of %d URL(s) (round %d)" starts a round;
"Fetched %d/%d URL(s) successfully" marks the fetch phase's end / categorize phase's
start for that round (its own duration - "Fetched" minus "Processing batch" - is the
round's fetch-phase time, written separately since fetch happens concurrently across
the whole batch, not per URL). There is no explicit marker for "categorize phase
ended, extract phase begins" - inferred as immediately after that round's last
categorize-phase log line.

Usage: python experiments/parse_run_log_timing.py <log_file> <label>

label is a short tag identifying this run (e.g. "cpu", "gpu"), used in output
filenames so multiple runs don't collide.

Output:
  experiments/data/run_log_<label>_page_timing.csv - same column shape as
    experiments/analyze_session.py's _page_timing.csv (round, url, category,
    categorize_seconds, extract_seconds), so it's a drop-in replacement anywhere that
    format is consumed (see notebooks/session_monitoring.ipynb). A page whose
    categorization failed is included with category="" and extract_seconds=0.0,
    matching model.orchestrator.process_batch()'s own
    monitor_service.page(url, None, categorize_seconds, 0.0) call for that case.
  experiments/data/run_log_<label>_fetch_phase.csv - (round, fetch_phase_seconds),
    the round-level aggregate described above.
"""
import re
import sys
from pathlib import Path

from common import DATA_DIR, write_csv

LINE_RE = re.compile(r"^(\d\d):(\d\d):(\d\d)\s+(\w+)\s+([\w.]+):\s?(.*)$")
ROUND_RE = re.compile(r"Processing batch of \d+ URL\(s\) \(round (\d+)\)")
FETCHED_RE = re.compile(r"Fetched \d+/\d+ URL\(s\) successfully")
CATEGORIZED_RE = re.compile(r"Categorized (\S+) as (\S+)")
SKIP_RE = re.compile(r"Skipping (\S+) - categorization failed")
EXTRACTED_ENTRIES_RE = re.compile(r"Extracted \d+ entries from (\S+)")
EXTRACTED_FIELDS_RE = re.compile(r"Extracted \d+ field\(s\) from (\S+)")
NO_DATA_RE = re.compile(r"No data extracted from (\S+) \(category=(\S+)\)")


def _parse_lines(path):
    """Yield (absolute_seconds, message) for every well-formed log line, handling midnight rollover."""
    day_offset = 0
    prev_seconds_of_day = None
    with open(path, encoding="cp1252", errors="replace") as f:
        for line in f:
            m = LINE_RE.match(line)
            if not m:
                continue
            h, mi, s, _level, _module, message = m.groups()
            seconds_of_day = int(h) * 3600 + int(mi) * 60 + int(s)
            if prev_seconds_of_day is not None and seconds_of_day < prev_seconds_of_day:
                day_offset += 86400
            prev_seconds_of_day = seconds_of_day
            yield day_offset + seconds_of_day, message


def parse(path):
    """
    Returns (page_rows, fetch_phase_rows), both lists of dicts, by replaying the log
    exactly as described in this module's docstring.
    """
    page_rows = []
    fetch_phase_rows = []

    current_round = None
    round_start_ts = None
    fetched_ts = None  # end of fetch phase / start of categorize phase, this round
    last_categorize_ts = None  # running boundary within the categorize phase
    last_extract_ts = None  # running boundary within the extract phase
    extract_phase_started = False

    # categorize-phase results for the round in progress, in log order
    categorize_results = []  # (url, category_or_None, categorize_seconds)
    extract_seconds_by_url = {}

    def _flush_round():
        """
        Merge this round's categorize + extract results into page_rows and reset round
        state. Critical: a categorized page with no observed extraction-outcome log
        line is NOT the same as one that extracted in ~0s - it means the run was
        stopped (or the log was captured) before its extract_information() call ever
        completed, most commonly for a round's tail end when extraction lags behind a
        much-faster-finishing categorize loop (see this module's docstring). Such
        pages get extract_seconds="" (unknown), never a fabricated 0.0.
        """
        for url, category, cat_secs in categorize_results:
            if category is None:
                page_rows.append({"round": current_round, "url": url, "category": "",
                                   "categorize_seconds": round(cat_secs, 3), "extract_seconds": 0.0})
            elif url in extract_seconds_by_url:
                page_rows.append({"round": current_round, "url": url, "category": category,
                                   "categorize_seconds": round(cat_secs, 3),
                                   "extract_seconds": round(extract_seconds_by_url[url], 3)})
            else:
                page_rows.append({"round": current_round, "url": url, "category": category,
                                   "categorize_seconds": round(cat_secs, 3), "extract_seconds": ""})
        categorize_results.clear()
        extract_seconds_by_url.clear()

    for ts, message in _parse_lines(path):
        m = ROUND_RE.search(message)
        if m:
            if current_round is not None:
                _flush_round()
            current_round = int(m.group(1))
            round_start_ts = ts
            fetched_ts = None
            last_categorize_ts = None
            last_extract_ts = None
            extract_phase_started = False
            continue

        if current_round is None:
            continue  # log lines before the first round (model loading, config) - not page/round data

        if FETCHED_RE.search(message):
            fetched_ts = ts
            last_categorize_ts = ts
            fetch_phase_rows.append({"round": current_round,
                                      "fetch_phase_seconds": round(ts - round_start_ts, 3)})
            continue

        m = CATEGORIZED_RE.search(message)
        if m:
            url, category = m.group(1), m.group(2)
            categorize_results.append((url, category, ts - last_categorize_ts))
            last_categorize_ts = ts
            continue

        m = SKIP_RE.search(message)
        if m:
            url = m.group(1)
            categorize_results.append((url, None, ts - last_categorize_ts))
            last_categorize_ts = ts
            continue

        m = EXTRACTED_ENTRIES_RE.search(message) or EXTRACTED_FIELDS_RE.search(message)
        if m:
            url = m.group(1)
            boundary = last_categorize_ts if not extract_phase_started else last_extract_ts
            extract_seconds_by_url[url] = ts - boundary
            last_extract_ts = ts
            extract_phase_started = True
            continue

        m = NO_DATA_RE.search(message)
        if m:
            url = m.group(1)
            boundary = last_categorize_ts if not extract_phase_started else last_extract_ts
            extract_seconds_by_url[url] = ts - boundary
            last_extract_ts = ts
            extract_phase_started = True
            continue

    if current_round is not None:
        _flush_round()

    return page_rows, fetch_phase_rows


def main():
    if len(sys.argv) < 3:
        print("Usage: python experiments/parse_run_log_timing.py <log_file> <label>")
        sys.exit(1)
    log_path = Path(sys.argv[1])
    label = sys.argv[2]

    page_rows, fetch_phase_rows = parse(log_path)

    page_out = DATA_DIR / f"run_log_{label}_page_timing.csv"
    fetch_out = DATA_DIR / f"run_log_{label}_fetch_phase.csv"
    write_csv(page_out, page_rows, ["round", "url", "category", "categorize_seconds", "extract_seconds"])
    write_csv(fetch_out, fetch_phase_rows, ["round", "fetch_phase_seconds"])

    print(f"Parsed {log_path}: {len(page_rows)} page(s) across "
          f"{len({r['round'] for r in page_rows})} round(s), {len(fetch_phase_rows)} fetch-phase row(s)")
    print(f"Wrote {page_out}")
    print(f"Wrote {fetch_out}")

    unknown_extract = sum(1 for r in page_rows if r["extract_seconds"] == "")
    total_cat = sum(r["categorize_seconds"] for r in page_rows)
    total_ext = sum(r["extract_seconds"] for r in page_rows if r["extract_seconds"] != "")
    total_fetch = sum(r["fetch_phase_seconds"] for r in fetch_phase_rows)
    total = total_cat + total_ext + total_fetch
    if total:
        print(f"Totals: fetch={total_fetch:.0f}s ({100*total_fetch/total:.1f}%), "
              f"categorize={total_cat:.0f}s ({100*total_cat/total:.1f}%), "
              f"extract={total_ext:.0f}s ({100*total_ext/total:.1f}%)")
    if unknown_extract:
        print(f"{unknown_extract}/{len(page_rows)} page(s) have unknown extract_seconds "
              f"(no extraction-outcome log line observed before the log ended - most "
              f"likely the run's final, still-in-progress round) - excluded from the "
              f"extract total above and left blank in the CSV, not fabricated as 0.")


if __name__ == "__main__":
    main()
