"""
The command line, checked where it decides something.

main.py is a thin shell around the orchestrator, but two of its flags change
what a run starts from - and getting those wrong silently resumes a frontier
the user wanted gone, or throws away one they wanted kept.
"""
import sys

import main


def _args(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["main.py", *argv])
    return main._parse_args()


def test_the_frontier_is_neither_kept_nor_dropped_unless_asked(monkeypatch):
    args = _args(monkeypatch)
    assert args.keep_frontier is False
    assert args.drop_frontier is False


def test_keep_frontier_can_be_asked_for(monkeypatch):
    assert _args(monkeypatch, "--keep-frontier").keep_frontier is True


def test_drop_frontier_can_be_asked_for(monkeypatch):
    assert _args(monkeypatch, "--drop-frontier").drop_frontier is True


def test_keeping_the_frontier_overrides_the_config(monkeypatch):
    assert main._persist_frontier(_args(monkeypatch, "--keep-frontier"), False) is True


def test_dropping_the_frontier_overrides_the_config(monkeypatch):
    assert main._persist_frontier(_args(monkeypatch, "--drop-frontier"), True) is False


def test_without_a_flag_the_config_decides(monkeypatch):
    args = _args(monkeypatch)
    assert main._persist_frontier(args, True) is True
    assert main._persist_frontier(args, False) is False
