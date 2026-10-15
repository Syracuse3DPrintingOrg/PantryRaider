"""A request that presents a key never waits on the key store's fsync.

credentials.record_use runs on the event loop for every keyed request. Its
durable write (several fsyncs, slow on an SD card) is handed to a thread when
called from the loop, while the use is visible in memory straight away.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from app import durable
from app.config import settings
from app.services import credentials

T0 = 1_800_000_000.0


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", [], raising=False)
    monkeypatch.setattr(settings, "legacy_mirror", False, raising=False)
    credentials.reset(None, persist=True)
    yield tmp_path
    credentials.wait_idle()
    credentials.reset(persist=False)


def _slow_writes(monkeypatch, seconds: float):
    """Make every durable write take ``seconds`` and record its thread."""
    threads = []
    real = durable.write_json

    def slow(*a, **kw):
        threads.append(threading.current_thread().name)
        time.sleep(seconds)
        return real(*a, **kw)

    monkeypatch.setattr(durable, "write_json", slow)
    return threads


def test_first_use_on_the_loop_does_not_wait_for_the_disk(store, monkeypatch):
    rec, _secret = credentials.mint("ha", "Home Assistant", "manual", now=T0)
    threads = _slow_writes(monkeypatch, 0.4)

    async def request():
        t0 = time.perf_counter()
        credentials.record_use(rec["id"], {"ip": "192.168.1.30", "client": "ha/1"})
        return time.perf_counter() - t0

    took = asyncio.run(request())
    assert took < 0.2
    # Seen in memory at once, on disk once the writer is done.
    assert credentials.get(rec["id"])["last_used"]
    assert credentials.wait_idle(5)
    assert threads == ["credentials-write"]
    on_disk = json.loads((store / credentials.FILENAME).read_text())
    assert on_disk["records"][rec["id"]]["last_ip"] == "192.168.1.30"


def test_off_the_loop_the_write_is_inline(store, monkeypatch):
    rec, _secret = credentials.mint("ha", "Home Assistant", "manual", now=T0)
    threads = _slow_writes(monkeypatch, 0.0)
    credentials.record_use(rec["id"], {"ip": "192.168.1.31"})
    assert threads == ["MainThread"]
    on_disk = json.loads((store / credentials.FILENAME).read_text())
    assert on_disk["records"][rec["id"]]["last_ip"] == "192.168.1.31"


def test_uses_during_a_write_are_kept_and_written_next(store, monkeypatch):
    rec, _secret = credentials.mint("ha", "Home Assistant", "manual", now=T0)
    _slow_writes(monkeypatch, 0.3)

    async def burst():
        credentials.record_use(rec["id"], {"ip": "10.0.0.1"})
        await asyncio.sleep(0.05)       # the first write is on the disk now
        credentials.record_refusal(rec["id"], "/setup/save")
        credentials.flush(force=True)   # waits for the writer, then writes
    asyncio.run(burst())
    assert credentials.wait_idle(5)
    on_disk = json.loads((store / credentials.FILENAME).read_text())["records"][rec["id"]]
    assert on_disk["last_ip"] == "10.0.0.1"
    assert [r["path"] for r in on_disk["refusals"]] == ["/setup/save"]
    # Memory shows each refusal once, not once per layer.
    assert len(credentials.get(rec["id"])["refusals"]) == 1


def test_a_lookup_does_not_wait_for_a_write_in_progress(store, monkeypatch):
    rec, secret = credentials.mint("ha", "Home Assistant", "manual", now=T0)
    _slow_writes(monkeypatch, 0.5)

    async def flow():
        credentials.record_use(rec["id"], {"ip": "10.0.0.2"})
        await asyncio.sleep(0.05)
        t0 = time.perf_counter()
        found = credentials.lookup(secret)
        credentials.record_use(rec["id"], {"ip": "10.0.0.3"})
        return found, time.perf_counter() - t0

    found, took = asyncio.run(flow())
    assert found["id"] == rec["id"]
    assert took < 0.2
    assert credentials.wait_idle(5)


def test_a_failed_write_keeps_the_use_in_memory(store, monkeypatch):
    rec, _secret = credentials.mint("ha", "Home Assistant", "manual", now=T0)

    def broken(*a, **kw):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(durable, "write_json", broken)

    async def request():
        credentials.record_use(rec["id"], {"ip": "10.0.0.4"})
    asyncio.run(request())
    assert credentials.wait_idle(5)
    assert credentials.get(rec["id"])["last_ip"] == "10.0.0.4"
