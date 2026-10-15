"""State files changed inside a request are written off the event loop
(services/state_writer.py), starting with the timer registry."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import pytest

from app.config import settings
from app.services import state_writer, timers


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    timers._writer.wait_idle()
    timers._timers.clear()
    timers._mtime = None
    yield tmp_path
    timers._writer.wait_idle()
    timers.clear_all()
    timers._writer.wait_idle()


def _slow_replace(monkeypatch, seconds):
    names = []
    real = os.replace

    def slow(src, dst):
        names.append(threading.current_thread().name)
        time.sleep(seconds)
        return real(src, dst)

    # The writer saves through durable.write_bytes_atomic, which renames
    # with os.replace.
    from app import durable
    monkeypatch.setattr(durable.os, "replace", slow)
    return names


def test_a_timer_made_on_the_loop_does_not_wait_for_the_disk(data_dir, monkeypatch):
    names = _slow_replace(monkeypatch, 0.4)

    async def request():
        t0 = time.perf_counter()
        made = timers.create_timer("pasta", 600)
        took = time.perf_counter() - t0
        # A poll right after sees it, even though the file is not written yet.
        listed = timers.list_timers()
        return made, took, listed

    made, took, listed = asyncio.run(request())
    assert took < 0.2
    assert [t["id"] for t in listed] == [made["id"]]
    assert timers._writer.wait_idle(5)
    assert names == ["timers-write"]
    on_disk = json.loads((data_dir / "timers.json").read_text())
    assert [t["label"] for t in on_disk["timers"]] == ["pasta"]


def test_changes_during_a_write_land_in_order(data_dir, monkeypatch):
    _slow_replace(monkeypatch, 0.2)

    async def burst():
        a = timers.create_timer("a", 60)
        await asyncio.sleep(0.05)          # the first write is in progress
        b = timers.create_timer("b", 60)
        timers.cancel_timer(a["id"])
        return b

    b = asyncio.run(burst())
    assert timers._writer.wait_idle(5)
    on_disk = json.loads((data_dir / "timers.json").read_text())
    assert [t["id"] for t in on_disk["timers"]] == [b["id"]]
    # And a fresh read from the file agrees with memory.
    timers._mtime = None
    assert [t["id"] for t in timers.list_timers()] == [b["id"]]


def test_off_the_loop_the_write_is_inline(data_dir, monkeypatch):
    names = _slow_replace(monkeypatch, 0.0)
    timers.create_timer("eggs", 300)
    assert names == ["MainThread"]
    assert (data_dir / "timers.json").exists()


def test_an_inline_write_waits_for_a_background_one(tmp_path, monkeypatch):
    _slow_replace(monkeypatch, 0.2)
    w = state_writer.StateWriter("probe")
    path = tmp_path / "probe.json"
    seen = []

    async def from_loop():
        w.write(path, b"old", seen.append)
    asyncio.run(from_loop())
    w.write(path, b"new", seen.append)       # inline, after the thread's write
    assert w.wait_idle(5)
    assert path.read_bytes() == b"new"
    assert 1 <= len(seen) <= 2      # the older write may have been dropped unwritten


def test_an_unwritable_dir_degrades_quietly(tmp_path):
    w = state_writer.StateWriter("probe")
    got = []
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder")
    w.write(blocker / "x.json", b"{}", got.append)
    assert got == [None]


def test_the_background_write_is_durable(tmp_path, monkeypatch):
    from app import durable
    calls = []
    real = durable.write_bytes_atomic
    monkeypatch.setattr(durable, "write_bytes_atomic",
                        lambda p, d, **kw: calls.append(kw.get("sync", True)) or real(p, d, **kw))
    assert state_writer.write_replace(tmp_path / "x.json", b"{}") is not None
    assert calls == [True]


def test_a_timer_that_went_off_a_day_ago_is_dropped(data_dir, monkeypatch):
    old = timers.create_timer("forgotten", 60)
    fresh = timers.create_timer("just rang", 60)
    now = time.time()
    with timers._lock:
        timers._timers[old["id"]].deadline_epoch = now - timers.STALE_AFTER_SECONDS - 5
        timers._timers[fresh["id"]].deadline_epoch = now - 30
    ids = [t["id"] for t in timers.list_timers()]
    assert ids == [fresh["id"]]          # the recent alert stays until dismissed
    timers.create_timer("next", 60)      # and the next change writes the file
    on_disk = json.loads((data_dir / "timers.json").read_text())
    assert old["id"] not in [t["id"] for t in on_disk["timers"]]
