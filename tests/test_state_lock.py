"""Hammer test for the shared cross-process state-file lock (FoodAssistant-k7cw).

state_lock.state_write_lock wraps a state file's read-modify-write with an
fcntl.flock on a sidecar ``<file>.lock``. flock is enforced by the kernel per
open file description across PROCESSES (not just threads in one interpreter),
so the only honest proof that it prevents a lost update is a real
multi-process race: N worker processes all incrementing the same counter in
the same JSON state file, each protected by the lock. Without the lock this
reliably loses updates (two workers read the same value, both add one, one
increment vanishes); with it, every increment must land.

A deliberate short sleep between the read and the write, inside the locked
section, widens the race window so the test would reliably fail if the lock
were a no-op, while keeping total runtime small (a handful of workers, a
handful of iterations each).
"""
from __future__ import annotations

import json
import multiprocessing
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

from app.services.state_lock import state_write_lock  # noqa: E402

WORKERS = 8
ITERS_PER_WORKER = 20
TOTAL_EXPECTED = WORKERS * ITERS_PER_WORKER


def _increment_worker(state_file: str, iterations: int) -> None:
    """Child-process target: read-modify-write a JSON counter under the lock.

    Module-level (picklable) so it works under the multiprocessing "spawn"
    start method, not just "fork". The sleep between read and write is the
    deliberate race window described above.
    """
    sf = Path(state_file)
    for _ in range(iterations):
        with state_write_lock(sf):
            try:
                data = json.loads(sf.read_text())
            except (OSError, ValueError):
                data = {"count": 0}
            count = int(data.get("count", 0))
            time.sleep(0.003)  # widen the race window
            count += 1
            tmp = sf.with_name(sf.name + ".tmp")
            tmp.write_text(json.dumps({"count": count}))
            os.replace(tmp, sf)


def _append_worker(state_file: str, worker_id: int, iterations: int) -> None:
    """Child-process target: append this worker's unique ids to a shared list
    under the lock, proving no append is lost and none is duplicated."""
    sf = Path(state_file)
    for i in range(iterations):
        with state_write_lock(sf):
            try:
                data = json.loads(sf.read_text())
            except (OSError, ValueError):
                data = {"items": []}
            items = list(data.get("items", []))
            time.sleep(0.002)  # widen the race window
            items.append(f"{worker_id}:{i}")
            tmp = sf.with_name(sf.name + ".tmp")
            tmp.write_text(json.dumps({"items": items}))
            os.replace(tmp, sf)


@pytest.fixture
def mp_ctx():
    # "spawn" is the honest cross-platform proof: each worker is a genuinely
    # separate interpreter/process, not a fork sharing memory by accident.
    return multiprocessing.get_context("spawn")


def test_concurrent_increments_lose_none(tmp_path, mp_ctx):
    """N processes incrementing a shared counter: every increment must land."""
    state_file = tmp_path / "counter.json"
    state_file.write_text(json.dumps({"count": 0}))

    procs = [
        mp_ctx.Process(target=_increment_worker,
                        args=(str(state_file), ITERS_PER_WORKER))
        for _ in range(WORKERS)
    ]
    start = time.monotonic()
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
    elapsed = time.monotonic() - start

    for p in procs:
        assert p.exitcode == 0, "a worker process crashed"
    assert elapsed < 20, f"hammer test took too long ({elapsed:.1f}s)"

    data = json.loads(state_file.read_text())
    assert data["count"] == TOTAL_EXPECTED, (
        f"lost update(s): expected {TOTAL_EXPECTED}, got {data['count']}"
    )


def test_concurrent_appends_lose_none_and_dedupe(tmp_path, mp_ctx):
    """N processes each appending their own tagged entries: every single one
    must survive (no lost update), and none is duplicated (no torn read)."""
    state_file = tmp_path / "items.json"
    state_file.write_text(json.dumps({"items": []}))

    procs = [
        mp_ctx.Process(target=_append_worker,
                        args=(str(state_file), worker_id, ITERS_PER_WORKER))
        for worker_id in range(WORKERS)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
    for p in procs:
        assert p.exitcode == 0

    data = json.loads(state_file.read_text())
    items = data["items"]
    assert len(items) == TOTAL_EXPECTED
    assert len(set(items)) == TOTAL_EXPECTED  # no duplicates, no lost entries
    expected = {f"{w}:{i}" for w in range(WORKERS) for i in range(ITERS_PER_WORKER)}
    assert set(items) == expected


def test_lock_file_created_next_to_state_file(tmp_path):
    """A sidecar ``<name>.lock`` appears next to the state file, never the
    state file's own inode (which os.replace swaps out from under a lock)."""
    state_file = tmp_path / "some_state.json"
    lock_file = tmp_path / "some_state.json.lock"
    assert not lock_file.exists()
    with state_write_lock(state_file):
        assert lock_file.exists()
    # The lock file persists (it is reused across calls); it must never be
    # the same path as the state file itself.
    assert lock_file != state_file


def test_degrades_gracefully_when_sidecar_cannot_be_created(tmp_path):
    """When the lock file cannot be created (e.g. an unwritable/missing
    parent directory), the context manager still yields and the caller's
    mutation still runs, mirroring the state files' own quiet degradation."""
    missing_dir = tmp_path / "does-not-exist"
    state_file = missing_dir / "state.json"  # parent dir never created

    ran = False
    with state_write_lock(state_file):
        ran = True
    assert ran, "the caller's block must still execute when the lock degrades"
    # Degradation must not have magically created the missing directory.
    assert not missing_dir.exists()


def test_degrades_gracefully_on_unwritable_directory(tmp_path):
    """Same degradation path, this time via a directory whose permissions
    forbid creating the sidecar file (rather than a missing parent)."""
    locked_dir = tmp_path / "readonly"
    locked_dir.mkdir()
    state_file = locked_dir / "state.json"
    state_file.write_text("{}")
    os.chmod(locked_dir, 0o555)  # read + execute only: cannot create files
    try:
        ran = False
        with state_write_lock(state_file):
            ran = True
        assert ran
    finally:
        os.chmod(locked_dir, 0o755)  # restore so tmp_path cleanup can remove it
