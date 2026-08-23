import json

import model.tools.monitor_service as monitor_service


def _read_events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_round_start_and_round_end_are_no_ops_before_start_session():
    # module-level state persists across tests - explicitly confirm the
    # no-file-open state doesn't blow up rather than relying on import order
    monitor_service._file = None
    assert monitor_service.round_start(3) >= 1
    monitor_service.round_end()  # must not raise even with no open file


def test_start_session_creates_a_jsonl_file_with_a_session_start_event(tmp_path):
    log_dir = tmp_path / "sessions"
    path = monitor_service.start_session(str(log_dir))

    assert path is not None
    assert path.exists()
    events = _read_events(path)
    assert events[0]["event"] == "session_start"
    assert "monotonic_time" in events[0]
    assert "wall_time" in events[0]


def test_round_start_assigns_sequential_round_numbers_and_resets_per_session(tmp_path):
    monitor_service.start_session(str(tmp_path / "sessions"))

    assert monitor_service.round_start(queued=5) == 1
    assert monitor_service.round_start(queued=2) == 2

    # starting a new session resets the round counter
    monitor_service.start_session(str(tmp_path / "sessions2"))
    assert monitor_service.round_start(queued=1) == 1


def test_round_events_are_flushed_to_disk_immediately(tmp_path):
    path = monitor_service.start_session(str(tmp_path / "sessions"))

    monitor_service.round_start(queued=4)
    monitor_service.round_end()

    events = _read_events(path)
    assert [e["event"] for e in events] == ["session_start", "round_start", "round_end"]
    assert events[1]["round"] == 1
    assert events[1]["queued"] == 4
    assert events[2]["round"] == 1


def test_page_event_is_tied_to_the_current_round(tmp_path):
    path = monitor_service.start_session(str(tmp_path / "sessions"))

    monitor_service.round_start(queued=2)
    monitor_service.page("http://a.com", "STATION", 1.5, 3.25)
    monitor_service.page("http://b.com", None, 0.8, 0.0)
    monitor_service.round_end()

    events = _read_events(path)
    page_events = [e for e in events if e["event"] == "page"]
    assert len(page_events) == 2
    assert all(e["round"] == 1 for e in page_events)
    assert page_events[0]["url"] == "http://a.com"
    assert page_events[0]["category"] == "STATION"
    assert page_events[0]["categorize_seconds"] == 1.5
    assert page_events[0]["extract_seconds"] == 3.25
    assert page_events[1]["category"] is None


def test_discovery_event_records_batch_outcome_and_increments_batch_number(tmp_path):
    path = monitor_service.start_session(str(tmp_path / "sessions"))

    monitor_service.discovery(queries_used=5, discovered_unique=12, newly_queued=7, queries_remaining=20)
    monitor_service.discovery(queries_used=5, discovered_unique=3, newly_queued=0, queries_remaining=15)

    events = _read_events(path)
    discovery_events = [e for e in events if e["event"] == "discovery"]
    assert [e["discovery_batch"] for e in discovery_events] == [1, 2]
    assert discovery_events[0]["newly_queued"] == 7
    assert discovery_events[1]["newly_queued"] == 0


def test_session_end_writes_summary_and_closes_the_file(tmp_path):
    path = monitor_service.start_session(str(tmp_path / "sessions"))

    monitor_service.session_end(rounds=3, discovery_batches=1, elapsed_seconds=12.345, stop_reason="test stop")

    events = _read_events(path)
    summary = events[-1]
    assert summary["event"] == "session_end"
    assert summary["rounds"] == 3
    assert summary["discovery_batches"] == 1
    assert summary["elapsed_seconds"] == 12.345
    assert summary["stop_reason"] == "test stop"
    assert monitor_service._file is None  # closed


def test_start_session_falls_back_to_disabled_monitoring_when_dir_cannot_be_created(tmp_path):
    # a file in the way of the intended directory makes mkdir fail
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")

    path = monitor_service.start_session(str(blocker / "sessions"))

    assert path is None
    # subsequent calls are silent no-ops, not crashes
    monitor_service.round_start(1)
    monitor_service.round_end()
