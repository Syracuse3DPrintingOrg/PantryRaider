"""The database is checked at start and put back from a snapshot when damaged
(FoodAssistant-nw31c), and the connection pool covers every request thread."""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from app import database, durable

STARLETTE_THREADPOOL = 40   # sync handlers run on anyio's default 40 threads


@pytest.fixture(autouse=True)
def _clear_notices():
    durable.take_notices()
    yield
    durable.take_notices()


def _make_db(path: Path, rows=("a", "b")) -> None:
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    con.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    con.commit()
    con.close()


def _rows(path: Path) -> list:
    con = sqlite3.connect(str(path))
    try:
        return [r[0] for r in con.execute("SELECT v FROM t ORDER BY v")]
    finally:
        con.close()


def _damage(path: Path) -> None:
    # Fold the WAL in and drop the sidecars, then wreck the header.
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    with open(path, "r+b") as fh:
        fh.write(b"\x00" * 100)


def test_the_pool_can_serve_every_request_thread():
    pool = database.engine.pool
    assert pool.size() + pool._max_overflow >= STARLETTE_THREADPOOL
    assert pool._timeout <= 10


def test_quick_check_tells_damage_from_good(tmp_path):
    db = tmp_path / "foodassistant.db"
    _make_db(db)
    assert database.quick_check(db) is True
    _damage(db)
    assert database.quick_check(db) is False


def test_snapshot_is_taken_daily_and_three_are_kept(tmp_path):
    db = tmp_path / "foodassistant.db"
    _make_db(db)
    t = time.time()
    first = database.take_snapshot(db, now=t)
    assert first is not None and database.quick_check(first) is True
    # Not again the same day.
    assert database.take_snapshot(db, now=t + 60) is None
    for day in range(1, 5):
        # Age the snapshots taken so far by two days, then change the data.
        for snap in database.list_snapshots(tmp_path):
            st = snap.stat().st_mtime
            os.utime(snap, (st - 2 * 86400, st - 2 * 86400))
        _make_db(db, rows=(f"d{day}",))
        assert database.take_snapshot(db, now=t + day * 86400) is not None
    snaps = database.list_snapshots(tmp_path)
    assert len(snaps) == database.KEEP_SNAPSHOTS
    # A snapshot is a plain rollback-journal file: no sidecars beside it.
    assert all(p.suffix == ".db" for p in (tmp_path / database.SNAPSHOT_DIR).iterdir())


def test_unchanged_database_is_not_snapshotted_again(tmp_path):
    db = tmp_path / "foodassistant.db"
    _make_db(db)
    first = database.take_snapshot(db)
    old = time.time() - 3 * 86400
    os.utime(db, (old, old))
    os.utime(first, (old + 10, old + 10))
    assert database.take_snapshot(db) is None


def test_damaged_database_is_put_back_from_the_newest_good_snapshot(tmp_path):
    db = tmp_path / "foodassistant.db"
    _make_db(db, rows=("old",))
    older = database.take_snapshot(db, force=True)
    time.sleep(1.1)
    _make_db(db, rows=("new",))
    newer = database.take_snapshot(db, force=True)
    assert older != newer
    # The newest snapshot is itself damaged: the one before it is used.
    with open(newer, "r+b") as fh:
        fh.write(b"\x00" * 100)
    _damage(db)

    assert database.check_and_repair(db) == "restored"
    assert database.quick_check(db) is True
    assert _rows(db) == ["old"]
    aside = list(tmp_path.glob("foodassistant.db.corrupt-*"))
    assert len(aside) == 1
    notes = durable.pending_notices()
    assert notes and notes[0]["title"] == "The database was repaired"
    assert aside[0].name in notes[0]["body"]


def test_damage_with_no_snapshot_keeps_the_file_and_says_so(tmp_path):
    db = tmp_path / "foodassistant.db"
    _make_db(db)
    _damage(db)
    before = db.read_bytes()
    assert database.check_and_repair(db) == "damaged"
    assert db.read_bytes() == before
    assert durable.pending_notices()[0]["title"] == "The database is damaged"


def test_healthy_and_missing_databases_are_left_alone(tmp_path):
    db = tmp_path / "foodassistant.db"
    assert database.check_and_repair(db) == "missing"
    _make_db(db)
    assert database.check_and_repair(db) == "ok"
    assert not durable.pending_notices()
