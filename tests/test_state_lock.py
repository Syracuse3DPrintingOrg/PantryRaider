"""The shared state-file write lock (FoodAssistant-k7cw, n6mta).

Pantry Raider runs as one uvicorn worker, so state_lock.state_write_lock is an
in-process threading.RLock per state file, looked up by the file's resolved
path. These tests pin what that buys: two threads doing a read-modify-write of
the same file never lose an update, nesting the lock for one file in one call
path does not deadlock, and nothing is created on disk (no sidecar .lock
files, and a missing or unwritable data dir is no problem).

A deliberate short sleep between the read and the write, inside the locked
section, widens the race window so the hammer tests would reliably fail if the
lock were a no-op.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

from app.services.state_lock import state_write_lock  # noqa: E402

THREADS = 8
ITERS_PER_THREAD = 20
TOTAL_EXPECTED = THREADS * ITERS_PER_THREAD


def _write(sf: Path, data: dict) -> None:
    tmp = sf.with_name(sf.name + f".{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, sf)


def _run_threads(target, *args) -> None:
    errors: list[BaseException] = []

    def wrap(i):
        try:
            target(*args, i)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=wrap, args=(i,)) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a thread hung on the lock"
    assert not errors, errors


def _increment(sf: Path, _i: int) -> None:
    for _ in range(ITERS_PER_THREAD):
        with state_write_lock(sf):
            count = int(json.loads(sf.read_text()).get("count", 0))
            time.sleep(0.001)  # widen the race window
            _write(sf, {"count": count + 1})


def _append(sf: Path, worker_id: int) -> None:
    for i in range(ITERS_PER_THREAD):
        with state_write_lock(sf):
            items = list(json.loads(sf.read_text()).get("items", []))
            time.sleep(0.001)
            items.append(f"{worker_id}:{i}")
            _write(sf, {"items": items})


def test_two_threads_serialize_a_read_modify_write(tmp_path):
    """Threads incrementing one counter: every increment must land."""
    sf = tmp_path / "counter.json"
    sf.write_text(json.dumps({"count": 0}))
    _run_threads(_increment, sf)
    assert json.loads(sf.read_text())["count"] == TOTAL_EXPECTED


def test_concurrent_appends_lose_none_and_dedupe(tmp_path):
    sf = tmp_path / "items.json"
    sf.write_text(json.dumps({"items": []}))
    _run_threads(_append, sf)
    items = json.loads(sf.read_text())["items"]
    assert len(items) == TOTAL_EXPECTED
    assert set(items) == {f"{w}:{i}" for w in range(THREADS)
                          for i in range(ITERS_PER_THREAD)}


def test_nesting_the_lock_for_one_file_does_not_deadlock(tmp_path):
    """The old flock deadlocked here; the RLock lets the same thread re-enter."""
    sf = tmp_path / "state.json"
    done = threading.Event()

    def nested():
        with state_write_lock(sf):
            with state_write_lock(sf):
                # A second spelling of the same path is the same lock too.
                with state_write_lock(tmp_path / "." / "state.json"):
                    done.set()

    t = threading.Thread(target=nested, daemon=True)
    t.start()
    t.join(timeout=5)
    assert done.is_set(), "nesting state_write_lock for one file deadlocked"


def test_the_lock_excludes_other_threads_while_held(tmp_path):
    sf = tmp_path / "state.json"
    entered = threading.Event()

    def take():
        with state_write_lock(sf):
            entered.set()

    with state_write_lock(sf):
        t = threading.Thread(target=take, daemon=True)
        t.start()
        time.sleep(0.1)
        assert not entered.is_set(), "another thread got in while the lock was held"
    t.join(timeout=5)
    assert entered.is_set()


def test_different_files_do_not_block_each_other(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    got_b = threading.Event()
    with state_write_lock(a):
        def take_b():
            with state_write_lock(b):
                got_b.set()
        t = threading.Thread(target=take_b, daemon=True)
        t.start()
        t.join(timeout=5)
    assert got_b.is_set()


def test_no_sidecar_lock_file_is_created(tmp_path):
    sf = tmp_path / "some_state.json"
    with state_write_lock(sf):
        pass
    assert list(tmp_path.iterdir()) == []


def test_works_when_the_data_dir_is_missing(tmp_path):
    missing_dir = tmp_path / "does-not-exist"
    ran = False
    with state_write_lock(missing_dir / "state.json"):
        ran = True
    assert ran
    assert not missing_dir.exists()


def test_works_on_an_unwritable_directory(tmp_path):
    locked_dir = tmp_path / "readonly"
    locked_dir.mkdir()
    os.chmod(locked_dir, 0o555)
    try:
        ran = False
        with state_write_lock(locked_dir / "state.json"):
            ran = True
        assert ran
    finally:
        os.chmod(locked_dir, 0o755)


def test_accumulating_state_writers_hold_the_write_lock():
    """Every service that READ-MODIFY-WRITES a shared state file must hold the
    state_lock write lock around it.

    Two requests (threadpool routes, background tasks) can each load the file,
    each apply their own change, and the second write silently drops the
    first. os.replace keeps a reader from seeing a torn file but does nothing
    about a lost update (FoodAssistant-dxl0). Services that replace a file
    wholesale, like community_expiry's feed refresh, are safe on os.replace
    alone and are deliberately not listed here.
    """
    services = Path(__file__).parent.parent / "service" / "app" / "services"
    accumulating = (
        "timers.py", "current_recipe.py", "scanner_mode.py", "audit.py",
        "ha_events.py", "gadgets.py", "scan_session.py",
        "best_by_provenance.py", "expiry_learning.py",
        "auth_state.py", "credentials.py", "local_agents.py",
    )
    missing = [n for n in accumulating
               if "state_write_lock" not in (services / n).read_text()]
    assert not missing, (
        f"these accumulate shared state without the write lock: {missing}. "
        "Two concurrent requests would silently lose one update.")
