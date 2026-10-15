"""Run-time state files are written through durable.py.

Timers, the scanner mode, the audit session, the on-screen event ring and the
other small stores used to write a fixed ``<name>.tmp`` and rename it without a
flush. They now share durable.py's writer: a private temp name, a flush before
the rename, and the folder flushed after. The event ring is the one hot store,
so its flush is rate-limited.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import durable
from app.config import settings

SERVICES = Path(__file__).resolve().parents[1] / "service" / "app" / "services"

RUNTIME_STORES = (
    "timers.py", "scanner_mode.py", "audit.py", "ha_events.py", "usage.py",
    "receipt_jobs.py", "current_recipe.py", "expiry_learning.py",
    "community_expiry.py", "satellite.py", "gadgets.py",
    "scan_session.py", "best_by_provenance.py", "gadgets_buttons.py",
    "readiness.py", "ha_client.py", "bluetooth_print_setup.py",
    "deck_config.py", "instance_guard.py",
)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    return tmp_path


@pytest.fixture
def writes(monkeypatch):
    """Record (file name, sync) for every durable write."""
    seen: list[tuple[str, bool]] = []
    real = durable.write_bytes_atomic

    def spy(path, data, *, mode=0o600, sync=True):
        seen.append((Path(path).name, sync))
        return real(path, data, mode=mode, sync=sync)

    monkeypatch.setattr(durable, "write_bytes_atomic", spy)
    return seen


@pytest.mark.parametrize("rel", RUNTIME_STORES)
def test_store_has_no_fixed_temp_file(rel):
    text = (SERVICES / rel).read_text()
    assert 'name + ".tmp"' not in text, rel
    assert 'with_suffix(".json.tmp")' not in text, rel
    # A store written from the event loop may hand the write to the
    # background writer, whose write is the same durable one.
    assert ("durable.write_json(" in text or "_writer.write(" in text
            or "durable.write_bytes_atomic(" in text), rel


def test_no_service_writes_a_fixed_temp_file():
    """A new store copying the old pattern fails here, not after a power cut."""
    offenders = [p.name for p in sorted(SERVICES.rglob("*.py"))
                 if 'name + ".tmp"' in p.read_text()]
    assert offenders == []


def test_timers_flush_every_save_and_survive_a_restart(data_dir, writes):
    from app.services import timers
    timers.clear_all()
    writes.clear()
    created = timers.create_timer("Eggs", 300)
    assert writes == [("timers.json", True)]
    assert not list(data_dir.glob("*.tmp"))
    # A fresh process reads it back from disk.
    timers._timers = {}
    timers._mtime = None
    assert [t["id"] for t in timers.list_timers()] == [created["id"]]
    timers.clear_all()


def test_scanner_mode_flushes(data_dir, writes):
    from app.services import scanner_mode
    scanner_mode.reset()
    scanner_mode.set_mode("consume")
    assert ("scanner_mode.json", True) in writes
    scanner_mode._state["mtime"] = None
    scanner_mode._state["mode"] = "inventory"
    assert scanner_mode.get_mode() == "consume"
    scanner_mode.reset()


def test_usage_flushes_and_reads_back(data_dir, writes):
    from app.services import usage
    usage.record("gemini", 42)
    assert ("ai_usage.json", True) in writes
    assert usage._load()["total"] == 42


def test_event_ring_flush_is_rate_limited(data_dir, writes, monkeypatch):
    from app.services import ha_events
    ha_events.reset()
    clock = [1000.0]
    monkeypatch.setattr(ha_events.time, "monotonic", lambda: clock[0])
    for i in range(20):
        ha_events.add_notification(f"burst {i}")
    ring = [s for name, s in writes if name == "ha_events.json"]
    assert len(ring) == 20
    # Only the first write of the burst is flushed; the rest are still written
    # (every poller sees them) but not forced to the card.
    assert ring.count(True) == 1
    clock[0] += ha_events._SYNC_INTERVAL
    ha_events.add_notification("later")
    assert writes[-1] == ("ha_events.json", True)
    # Unflushed writes still reach other readers through the file.
    ha_events._events = []
    ha_events._mtime = None
    assert len(ha_events.poll(0)["events"]) == 21
    assert not list(data_dir.glob("*.tmp"))
    ha_events.reset()


def test_unflushed_write_is_still_atomic(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(durable.os, "fsync", lambda fd: calls.append(fd))
    path = tmp_path / "x.json"
    durable.write_json(path, {"a": 1}, keep_last_good=False, sync=False)
    assert calls == []
    assert path.read_text() == '{"a": 1}'
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]
    durable.write_json(path, {"a": 2}, keep_last_good=False)
    assert calls  # the default still flushes
