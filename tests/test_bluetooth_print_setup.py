"""Unit tests for the Bluetooth label printer setup status (FoodAssistant-h2j6).

services/bluetooth_print_setup.py persists not_set_up/installing/ready/failed
across uvicorn workers (same shape as scanner_mode.py, timers.py, etc.) and
combines that with a live bridge check in resolve_status(). Pure logic; no
network, no Docker.

Run: python -m pytest tests/test_bluetooth_print_setup.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402
from app.services import bluetooth_print_setup as bps  # noqa: E402


def _reset(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    # Reset the in-process cache so tests don't see a prior test's state.
    bps._state.update({**bps._DEFAULT_STATE, "mtime": None})


def test_current_defaults_to_not_set_up(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    assert bps.current() == {"status": bps.NOT_SET_UP, "started_at": None, "log_tail": ""}
    assert bps.is_installing() is False


def test_mark_installing_persists_and_is_visible_to_a_fresh_load(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    bps.mark_installing()
    assert bps.is_installing() is True
    # Simulate a different worker: drop the in-process cache and reread.
    bps._state["mtime"] = None
    state = bps.current()
    assert state["status"] == bps.INSTALLING
    assert state["started_at"] is not None


def test_mark_result_ok_sets_ready_with_log_tail(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    bps.mark_installing()
    bps.mark_result(True, "installed via source path")
    state = bps.current()
    assert state["status"] == bps.READY
    assert state["log_tail"] == "installed via source path"


def test_mark_result_failure_sets_failed(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    bps.mark_installing()
    bps.mark_result(False, "cargo build failed")
    state = bps.current()
    assert state["status"] == bps.FAILED
    assert "cargo build failed" in state["log_tail"]


def test_mark_result_truncates_long_log_tail(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    long_log = "x" * 10000
    bps.mark_result(False, long_log)
    assert len(bps.current()["log_tail"]) == bps._LOG_TAIL_CHARS


# -- resolve_status: combining persisted state with a live bridge check -----


def test_resolve_status_live_active_wins_ready():
    out = bps.resolve_status({"status": bps.NOT_SET_UP}, live_active=True)
    assert out == {"status": bps.READY}


def test_resolve_status_live_active_carries_log_tail():
    out = bps.resolve_status({"status": bps.READY, "log_tail": "ok"}, live_active=True)
    assert out == {"status": bps.READY, "log_tail": "ok"}


def test_resolve_status_installing_reports_started_at():
    now = 1000.0
    out = bps.resolve_status(
        {"status": bps.INSTALLING, "started_at": now - 60}, live_active=None, now=now)
    assert out == {"status": bps.INSTALLING, "started_at": now - 60}


def test_resolve_status_stale_installing_becomes_failed():
    now = 10000.0
    out = bps.resolve_status(
        {"status": bps.INSTALLING, "started_at": now - bps._STALE_INSTALL_SECONDS - 1},
        live_active=None, now=now)
    assert out["status"] == bps.FAILED


def test_resolve_status_failed_carries_log_tail():
    out = bps.resolve_status(
        {"status": bps.FAILED, "log_tail": "boom"}, live_active=None)
    assert out == {"status": bps.FAILED, "log_tail": "boom"}


def test_resolve_status_ready_downgrades_when_confirmed_inactive():
    out = bps.resolve_status({"status": bps.READY, "log_tail": ""}, live_active=False)
    assert out["status"] == bps.FAILED
    assert out["log_tail"]  # a helpful default message, not empty


def test_resolve_status_ready_trusts_persisted_state_when_bridge_unreachable():
    out = bps.resolve_status({"status": bps.READY, "log_tail": "ok"}, live_active=None)
    assert out == {"status": bps.READY, "log_tail": "ok"}


def test_resolve_status_not_set_up_default():
    out = bps.resolve_status({}, live_active=None)
    assert out == {"status": bps.NOT_SET_UP}
    out2 = bps.resolve_status({}, live_active=False)
    assert out2 == {"status": bps.NOT_SET_UP}
