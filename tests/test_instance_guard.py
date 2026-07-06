"""Multi-process data_dir guard (FoodAssistant-0fho).

The guard is belt-and-braces: the cross-worker state is shared through state
files, so a second worker is a warning, never a refusal. These tests exercise
the heartbeat file and the liveness/freshness checks without starting uvicorn.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import instance_guard as ig  # noqa: E402


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    yield tmp_path


def test_own_heartbeat_is_not_a_conflict(_data_dir):
    ig.write_heartbeat()
    assert (_data_dir / "app-instance.json").exists()
    assert ig.other_live_instance() is None


def test_no_file_is_not_a_conflict():
    assert ig.other_live_instance() is None


def _write_claim(path: Path, pid: int, updated: float) -> None:
    (path / "app-instance.json").write_text(
        json.dumps({"pid": pid, "updated": updated, "started": updated}))


def test_fresh_live_foreign_pid_is_detected(_data_dir):
    # The parent process (pytest's launcher, or init) is alive and not us.
    other = os.getppid()
    _write_claim(_data_dir, other, time.time())
    assert ig.other_live_instance() == other


def test_stale_heartbeat_is_ignored(_data_dir):
    _write_claim(_data_dir, os.getppid(), time.time() - ig.FRESH_SECONDS - 5)
    assert ig.other_live_instance() is None


def test_dead_pid_is_ignored(_data_dir):
    # A pid far above pid_max on any sane box; os.kill(pid, 0) raises.
    _write_claim(_data_dir, 2 ** 22 + 12345, time.time())
    assert ig.other_live_instance() is None


def test_corrupt_guard_file_is_ignored(_data_dir):
    (_data_dir / "app-instance.json").write_text("{not json")
    assert ig.other_live_instance() is None


def test_startup_warns_loudly_and_claims(_data_dir, caplog):
    other = os.getppid()
    _write_claim(_data_dir, other, time.time())
    with caplog.at_level(logging.WARNING, logger="foodassistant.instance"):
        assert ig.check_on_startup() == other
    assert any("MULTIPLE APP PROCESSES" in r.message for r in caplog.records)
    # The heartbeat now belongs to this process.
    data = json.loads((_data_dir / "app-instance.json").read_text())
    assert data["pid"] == os.getpid()


def test_single_instance_startup_is_silent(_data_dir, caplog):
    with caplog.at_level(logging.WARNING, logger="foodassistant.instance"):
        assert ig.check_on_startup() is None
    assert not caplog.records


def test_unwritable_data_dir_is_non_fatal(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", "/nonexistent/nowhere", raising=False)
    # Never raises: the guard just stays silent.
    assert ig.check_on_startup() is None
    ig.write_heartbeat()
