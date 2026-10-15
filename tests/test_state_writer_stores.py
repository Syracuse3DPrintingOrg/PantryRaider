"""The scanner mode, the on-screen event ring, the audit session, the current
recipe and the thermometer state are saved off the event loop
(services/state_writer.py), the way the timer registry is, and the per-request
check of the key store and access state is rate-limited."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import pytest

from app.config import settings
from app.services import audit, current_recipe, gadgets, ha_events, scanner_mode


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    scanner_mode.reset()
    ha_events.reset()
    audit.reset()
    gadgets.reset()
    yield tmp_path
    scanner_mode.reset()
    ha_events.reset()
    audit.reset()
    gadgets.reset()


def _slow_replace(monkeypatch, seconds):
    names = []
    real = os.replace

    def slow(src, dst):
        names.append(threading.current_thread().name)
        time.sleep(seconds)
        return real(src, dst)

    from app import durable
    monkeypatch.setattr(durable.os, "replace", slow)
    return names


def test_a_scanner_mode_change_on_the_loop_does_not_wait_for_the_disk(data_dir, monkeypatch):
    names = _slow_replace(monkeypatch, 0.4)

    async def request():
        t0 = time.perf_counter()
        scanner_mode.set_mode("consume")
        took = time.perf_counter() - t0
        return took, scanner_mode.get_mode()

    took, mode = asyncio.run(request())
    assert took < 0.2
    assert mode == "consume"                 # the next read sees it at once
    assert scanner_mode._writer.wait_idle(5)
    assert names == ["scanner-mode-write"]
    on_disk = json.loads((data_dir / "scanner_mode.json").read_text())
    assert on_disk == {"mode": "consume"}
    # A fresh read from the file agrees with memory.
    scanner_mode._state["mtime"] = None
    scanner_mode._state["mode"] = "inventory"
    assert scanner_mode.get_mode() == "consume"


def test_a_scanner_mode_cycle_burst_lands_last_one_wins(data_dir, monkeypatch):
    _slow_replace(monkeypatch, 0.2)

    async def burst():
        scanner_mode.cycle_mode()            # consume
        await asyncio.sleep(0.05)            # the first write is in progress
        scanner_mode.cycle_mode()            # shopping
        return scanner_mode.get_mode()

    assert asyncio.run(burst()) == "shopping"
    assert scanner_mode._writer.wait_idle(5)
    assert json.loads((data_dir / "scanner_mode.json").read_text()) == {"mode": "shopping"}


def test_off_the_loop_a_scanner_mode_write_is_inline(data_dir, monkeypatch):
    names = _slow_replace(monkeypatch, 0.0)
    scanner_mode.set_mode("shopping")
    assert names == ["MainThread"]
    assert json.loads((data_dir / "scanner_mode.json").read_text()) == {"mode": "shopping"}


def test_an_event_added_on_the_loop_does_not_wait_for_the_disk(data_dir, monkeypatch):
    names = _slow_replace(monkeypatch, 0.4)

    async def request():
        t0 = time.perf_counter()
        eid = ha_events.add_notification("Doorbell")
        took = time.perf_counter() - t0
        return eid, took, ha_events.poll(0)

    eid, took, polled = asyncio.run(request())
    assert took < 0.2
    assert [e["id"] for e in polled["events"]] == [eid]
    assert ha_events._writer.wait_idle(5)
    assert names == ["ha-events-write"]
    on_disk = json.loads((data_dir / "ha_events.json").read_text())
    assert [e["message"] for e in on_disk["events"]] == ["Doorbell"]
    assert not list(data_dir.glob("*.tmp"))


def test_events_added_during_a_write_all_land(data_dir, monkeypatch):
    _slow_replace(monkeypatch, 0.2)

    async def burst():
        ha_events.add_notification("one")
        await asyncio.sleep(0.05)
        ha_events.add_notification("two")
        ha_events.add_notification("three")

    asyncio.run(burst())
    assert ha_events._writer.wait_idle(5)
    on_disk = json.loads((data_dir / "ha_events.json").read_text())
    assert [e["message"] for e in on_disk["events"]] == ["one", "two", "three"]
    ha_events._events = []
    ha_events._mtime = None
    assert len(ha_events.poll(0)["events"]) == 3


def test_a_dropped_write_keeps_the_flush_it_asked_for(tmp_path, monkeypatch):
    from app.services import state_writer
    _slow_replace(monkeypatch, 0.2)
    syncs = []
    real = state_writer.write_replace
    monkeypatch.setattr(state_writer, "write_replace",
                        lambda p, d, sync=True: syncs.append(sync) or real(p, d, sync))
    w = state_writer.StateWriter("probe")
    path = tmp_path / "probe.json"

    async def burst():
        w.write(path, b"1", lambda m: None, sync=False)
        await asyncio.sleep(0.05)             # the first write is in progress
        w.write(path, b"2", lambda m: None, sync=True)
        w.write(path, b"3", lambda m: None, sync=False)   # replaces "2"
    asyncio.run(burst())
    assert w.wait_idle(5)
    assert path.read_bytes() == b"3"
    assert syncs == [False, True]


def test_an_audit_scan_on_the_loop_is_written_in_the_background(data_dir, monkeypatch):
    names = _slow_replace(monkeypatch, 0.3)

    async def request():
        audit.start("Pantry", [{"name": "Rice", "amount": 1}])
        audit.record_scan("Rice")
        return audit.status()

    status = asyncio.run(request())
    assert status["active"] is True
    assert audit._writer.wait_idle(5)
    assert set(names) == {"audit-write"}
    on_disk = json.loads((data_dir / "audit_session.json").read_text())
    assert on_disk["active"] is True and on_disk["location"] == "Pantry"


def test_clearing_the_current_recipe_on_the_loop_removes_the_file(data_dir, monkeypatch):
    names = _slow_replace(monkeypatch, 0.2)
    current_recipe._writer.wait_idle()
    current_recipe.clear_active()

    async def request():
        current_recipe.set_active({"title": "Soup", "ingredients": [], "steps": []})
        assert current_recipe.get_active()["title"] == "Soup"
        current_recipe.clear_active()
        return current_recipe.get_active()

    assert asyncio.run(request()) is None
    assert current_recipe._writer.wait_idle(5)
    assert "current-recipe-write" in names
    assert not (data_dir / "current_recipe.json").exists()


def test_a_thermometer_reading_on_the_loop_is_written_in_the_background(data_dir, monkeypatch):
    names = _slow_replace(monkeypatch, 0.3)

    async def request():
        t0 = time.perf_counter()
        gadgets.mark_reader_seen(now=time.time())
        return time.perf_counter() - t0

    assert asyncio.run(request()) < 0.2
    assert gadgets._writer.wait_idle(5)
    assert names == ["gadgets-write"]
    assert json.loads((data_dir / "gadgets.json").read_text())["reader_seen"] > 0


@pytest.mark.parametrize("modname", ["credentials", "auth_state"])
def test_the_outside_change_check_runs_at_most_once_a_second(modname, tmp_path, monkeypatch):
    import importlib
    mod = importlib.import_module(f"app.services.{modname}")
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    clock = [5000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    with mod._lock:
        mod._state["mtime"] = 1
        mod._state["stat_checked"] = None
        path = mod._path()
        assert mod._stat_due(path) is True
        assert mod._stat_due(path) is False          # within the second
        clock[0] += mod.STAT_INTERVAL
        assert mod._stat_due(path) is True
        # A different data folder (a test, a restore elsewhere) is checked at once.
        assert mod._stat_due(tmp_path / "elsewhere" / path.name) is True
        # A file not seen yet is always checked.
        assert mod._stat_due(path) is True
        mod._state["mtime"] = None
        assert mod._stat_due(path) is True
        mod._state["stat_checked"] = None
