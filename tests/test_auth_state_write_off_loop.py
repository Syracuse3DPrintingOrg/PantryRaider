"""What a request records in auth-state.json never waits on the disk.

Counters, sightings and gateway notes are recorded on the event loop as part
of serving a request. Their write (several fsyncs, slow on an SD card) goes to
a thread there; memory takes the change at once, the file catches up, and a
forced flush at shutdown still lands everything.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from app import durable
from app.config import settings
from app.services import auth_state

T0 = 1_800_000_000.0


@pytest.fixture
def disk(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    auth_state.reset(None, persist=True)
    auth_state.ensure_initialized(now=T0)
    yield tmp_path
    auth_state.wait_idle()
    auth_state.reset(auth_state.test_state(), persist=False)


def _file(tmp_path):
    return json.loads((tmp_path / auth_state.FILENAME).read_text())


def _slow_writes(monkeypatch, seconds):
    threads = []
    real = durable.write_json

    def slow(*a, **kw):
        threads.append(threading.current_thread().name)
        time.sleep(seconds)
        return real(*a, **kw)

    monkeypatch.setattr(durable, "write_json", slow)
    return threads


def test_a_first_count_on_the_loop_is_written_by_a_thread(disk, monkeypatch):
    threads = _slow_writes(monkeypatch, 0.4)

    async def request():
        t0 = time.perf_counter()
        auth_state.record_keyless_family("ha", "HomeAssistant/2026.10", "192.168.1.5", now=T0 + 5)
        return time.perf_counter() - t0

    assert asyncio.run(request()) < 0.2
    # Visible at once.
    assert auth_state.load()["keyless_families"]["ha"]["count"] == 1
    assert auth_state.wait_idle(5)
    assert threads == ["auth-state-write"]
    assert _file(disk)["keyless_families"]["ha"]["peer"] == "192.168.1.5"


def test_a_new_sighting_does_not_wait_for_the_disk(disk, monkeypatch):
    _slow_writes(monkeypatch, 0.4)

    async def request():
        t0 = time.perf_counter()
        new = auth_state.record_open_sighting("kitchen.example", "name", "10.0.0.9", now=T0 + 9)
        return new, time.perf_counter() - t0

    new, took = asyncio.run(request())
    assert new is True and took < 0.2
    assert auth_state.wait_idle(5)
    assert "kitchen.example" in _file(disk)["open_mode"]["seen"]


def test_counts_made_during_a_write_are_not_lost(disk, monkeypatch):
    _slow_writes(monkeypatch, 0.3)

    async def burst():
        auth_state.record_keyless_family("ha", "HA", "10.0.0.1", now=T0 + 1)
        await asyncio.sleep(0.05)       # the writer is on the disk
        for i in range(5):
            auth_state.record_keyless_family("ha", "HA", "10.0.0.1", now=T0 + 2 + i)
        # Reads during the write see memory, not the older file.
        return auth_state.load()["keyless_families"]["ha"]["count"]

    assert asyncio.run(burst()) == 6
    auth_state.flush(force=True)
    assert auth_state.wait_idle(5)
    assert _file(disk)["keyless_families"]["ha"]["count"] == 6
    assert auth_state.load()["keyless_families"]["ha"]["count"] == 6


def test_a_transition_still_writes_inline(disk, monkeypatch):
    threads = _slow_writes(monkeypatch, 0.0)

    async def request():
        auth_state.bump_session_gen(now=T0 + 20)
    asyncio.run(request())
    assert threads == ["MainThread"]


def test_an_inline_write_after_a_background_one_keeps_both(disk, monkeypatch):
    _slow_writes(monkeypatch, 0.2)

    async def flow():
        auth_state.record_keyless_family("display", "Mozilla", "10.0.0.3", now=T0 + 30)
        auth_state.bump_session_gen(now=T0 + 31)   # waits for the writer, then writes
    asyncio.run(flow())
    assert auth_state.wait_idle(5)
    doc = _file(disk)
    assert doc["keyless_families"]["display"]["count"] == 1
    assert doc.get("session_gen") == auth_state.session_gen()
