from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from pathlib import Path
from .config import settings
from . import durable
import logging
import os
import shutil
import sqlite3
import tempfile
import time

logger = logging.getLogger(__name__)

os.makedirs(settings.data_dir, exist_ok=True)

DB_NAME = "foodassistant.db"
DATABASE_URL = f"sqlite:///{settings.data_dir}/{DB_NAME}"

# --- Start-up check and rolling snapshots ------------------------------------
#
# WAL keeps the database whole only if the SD card does what it said, and a
# consumer card can lose a write it already acknowledged. SQLite then reports
# the damage only when a query trips over it, so the file is checked before
# the engine opens it, and a few snapshots taken with SQLite's backup API are
# kept to put back. A file copy of a live WAL database is not a snapshot.

SNAPSHOT_DIR = "db-snapshots"
KEEP_SNAPSHOTS = 3
SNAPSHOT_EVERY_SECONDS = 86400.0


def _db_path() -> Path:
    return Path(settings.data_dir) / DB_NAME


def snapshot_dir(data_dir=None) -> Path:
    return Path(data_dir or settings.data_dir) / SNAPSHOT_DIR


def quick_check(path) -> bool | None:
    """True when PRAGMA quick_check passes, False when the file is damaged,
    None when it could not be checked (busy, locked, cannot open). Only False
    is damage: moving a database aside because it was busy would be the real
    harm."""
    try:
        con = sqlite3.connect(str(path), timeout=5)
    except sqlite3.OperationalError:
        return None
    except sqlite3.DatabaseError:
        return False
    try:
        rows = con.execute("PRAGMA quick_check").fetchall()
        return [tuple(r) for r in rows] == [("ok",)]
    except sqlite3.OperationalError as exc:
        # "file is not a database" is a plain DatabaseError; an
        # OperationalError is busy or locked, which is not damage.
        logger.warning("Could not check the database (%s)", exc)
        return None
    except sqlite3.DatabaseError:
        return False
    finally:
        con.close()


def list_snapshots(data_dir=None) -> list[Path]:
    """Snapshot files, newest first. Only *.db: opening one may leave a
    journal beside it, which is never a snapshot."""
    try:
        snaps = [p for p in snapshot_dir(data_dir).glob("*.db") if p.is_file()]
    except OSError:
        return []
    def _key(p: Path):
        try:
            return (p.stat().st_mtime_ns, p.name)
        except OSError:
            return (0, p.name)
    return sorted(snaps, key=_key, reverse=True)


def _changed_since(db: Path, ref: Path) -> bool:
    try:
        ref_mtime = ref.stat().st_mtime_ns
    except OSError:
        return True
    for p in (db, db.with_name(db.name + "-wal")):
        try:
            if p.stat().st_mtime_ns > ref_mtime:
                return True
        except OSError:
            continue
    return False


def take_snapshot(db_path=None, *, now: float | None = None, force: bool = False) -> Path | None:
    """Keep a copy of the database made with SQLite's backup API.

    Taken at most once per SNAPSHOT_EVERY_SECONDS, and only when the database
    (or its WAL) changed since the newest snapshot. The copy is checked with
    quick_check and flushed before it is kept; the oldest beyond
    KEEP_SNAPSHOTS are removed. Returns the new snapshot or None. Never
    raises."""
    db = Path(db_path) if db_path else _db_path()
    t = time.time() if now is None else now
    try:
        if not db.is_file():
            return None
        folder = db.parent / SNAPSHOT_DIR
        snaps = list_snapshots(db.parent)
        if snaps and not force:
            newest = snaps[0]
            try:
                age = t - newest.stat().st_mtime
            except OSError:
                age = SNAPSHOT_EVERY_SECONDS
            if age < SNAPSHOT_EVERY_SECONDS or not _changed_since(db, newest):
                return None
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(t))
        dest = folder / f"{DB_NAME[:-3]}-{stamp}.db"
        n = 1
        while dest.exists():
            dest = folder / f"{DB_NAME[:-3]}-{stamp}-{n}.db"
            n += 1
        fd, tmp = tempfile.mkstemp(prefix=".snapshot.", suffix=".tmp", dir=str(folder))
        os.close(fd)
        try:
            src = sqlite3.connect(str(db), timeout=10)
            try:
                out = sqlite3.connect(tmp)
                try:
                    src.backup(out)
                    # A snapshot in rollback mode opens without -wal and -shm
                    # files; the app turns WAL back on when one is restored.
                    out.execute("PRAGMA journal_mode=DELETE")
                finally:
                    out.close()
            finally:
                src.close()
            if quick_check(tmp) is not True:
                logger.warning("A database snapshot did not pass its check; not kept")
                os.unlink(tmp)
                return None
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp, dest)
            durable.fsync_dir(folder)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        for old in list_snapshots(db.parent)[KEEP_SNAPSHOTS:]:
            try:
                old.unlink()
            except OSError:
                pass
        return dest
    except Exception:  # noqa: BLE001 - a snapshot never breaks the app
        logger.warning("Could not take a database snapshot", exc_info=True)
        return None


def _set_aside_damaged(db: Path) -> Path | None:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    aside = db.with_name(f"{db.name}{durable.CORRUPT_MARK}{stamp}")
    n = 1
    while aside.exists():
        aside = db.with_name(f"{db.name}{durable.CORRUPT_MARK}{stamp}-{n}")
        n += 1
    try:
        os.replace(db, aside)
    except OSError as exc:
        logger.error("Could not set the damaged database aside: %s", exc)
        return None
    # A stale WAL left beside a restored file would be replayed onto it.
    for side in ("-wal", "-shm", "-journal"):
        p = db.with_name(db.name + side)
        if p.exists():
            try:
                os.replace(p, aside.with_name(aside.name + side))
            except OSError:
                try:
                    p.unlink()
                except OSError:
                    pass
    return aside


def check_and_repair(db_path=None) -> str:
    """Run quick_check on the database before anything opens it.

    Returns 'ok', 'missing', 'unknown' (could not be checked), 'restored'
    (damaged; the newest snapshot that passes was put back and the damaged
    file kept aside), or 'damaged' (no good snapshot: the file stays in use,
    since half a database beats none). Queues an inbox notice for the last two.
    Never raises."""
    db = Path(db_path) if db_path else _db_path()
    try:
        if not db.is_file():
            return "missing"
        result = quick_check(db)
        if result is None:
            return "unknown"
        if result:
            return "ok"
        logger.error("The database failed its start-up check")
        good = next((s for s in list_snapshots(db.parent) if quick_check(s) is True), None)
        if good is None:
            durable.record_notice(
                f"storage_damaged:{DB_NAME}:{db.stat().st_mtime_ns}",
                "The database is damaged",
                "Pantry Raider found damage in its database when it started, most "
                "likely from a power cut, and there was no good snapshot to put "
                "back. It keeps running on what can still be read. Restore a "
                "backup from Settings, Backups & Updates as soon as you can.",
                level="error")
            return "damaged"
        aside = _set_aside_damaged(db)
        if aside is None:
            return "damaged"
        fd, tmp = tempfile.mkstemp(prefix=".restore.", suffix=".tmp", dir=str(db.parent))
        try:
            with os.fdopen(fd, "wb") as out, open(good, "rb") as src:
                shutil.copyfileobj(src, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, db)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        durable.fsync_dir(db.parent)
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(good.stat().st_mtime))
        logger.error("The database was damaged; put back the snapshot %s (damaged file: %s)",
                     good.name, aside.name)
        durable.record_notice(
            f"storage_recovered:{DB_NAME}:{aside.name}",
            "The database was repaired",
            "Pantry Raider found damage in its database when it started, most "
            f"likely from a power cut, and put back the snapshot from {when}. "
            "Anything added or changed in the app after that (pending items, "
            "recipes, the meal plan) may need to be entered again. Inventory "
            f"kept in Grocy is not affected. The damaged file was kept as {aside.name} "
            "in the data folder.",
            level="warning")
        return "restored"
    except Exception:  # noqa: BLE001 - the check never stops the app starting
        logger.warning("The database start-up check could not run", exc_info=True)
        return "unknown"


STARTUP_CHECK = check_and_repair()

# The pool is sized to Starlette's threadpool (40 threads run the sync
# handlers): a pool smaller than that is a queue, and the default 30 second
# timeout froze every page at once while a slow write held the connections.
# A file database gets SQLAlchemy's QueuePool, so these apply; 10 seconds is
# long enough to ride out an SD card stall and short enough to fail a request
# with a log line instead of hanging it.
POOL_SIZE = 20
POOL_MAX_OVERFLOW = 20
POOL_TIMEOUT = 10

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False},
                       pool_size=POOL_SIZE, max_overflow=POOL_MAX_OVERFLOW,
                       pool_timeout=POOL_TIMEOUT)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# One log line per process when WAL could not be turned on, not one per pooled
# connection.
_journal_warned = False


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    """Put SQLite in WAL mode when the filesystem can support it.

    Commits run on the event loop, and under the default rollback journal a
    reader blocks behind every writer. WAL needs shared memory (a -shm file)
    that FUSE-backed appdata mounts (Unraid) and network shares cannot always
    provide, and journal_mode is persistent inside the database file, so the
    mode is read back and the default is kept when the switch did not take.
    synchronous=NORMAL is only durable-enough under WAL, so it follows the
    read-back rather than being set blind.
    """
    global _journal_warned
    try:
        cur = dbapi_connection.cursor()
        try:
            row = cur.execute("PRAGMA journal_mode=WAL").fetchone()
            mode = str((row or [""])[0] or "").lower()
            if mode == "wal":
                cur.execute("PRAGMA synchronous=NORMAL")
            elif not _journal_warned:
                _journal_warned = True
                logger.warning(
                    "SQLite stayed in %s journal mode: this filesystem does not "
                    "support WAL shared memory.", mode or "the default")
        finally:
            cur.close()
    except Exception:
        if not _journal_warned:
            _journal_warned = True
            logger.warning("Could not set the SQLite journal mode; "
                           "keeping the default.", exc_info=True)


class Base(DeclarativeBase):
    pass


# Columns added to a table after it first shipped. create_all only creates
# missing TABLES; it never adds a missing column to an existing SQLite table,
# so an upgraded install keeps its old schema unless we ALTER it here.
# Existing installs are production: every entry must be
# additive and nullable so old rows stay valid untouched.
_COLUMN_ADDITIONS: dict[str, list[tuple[str, str]]] = {
    # FoodAssistant-vb60: best-by provenance for scanned/receipt items.
    # FoodAssistant-ezkh: the pre-edit suggestion, stashed when the user first
    # changes the date, so the commit can learn from the correction.
    # FoodAssistant-x61t: fast-ack background enrichment flag (1 while the
    # name lookup is still running after a queued scan).
    "pending_items": [("best_by_source", "VARCHAR"),
                      ("suggested_best_by", "VARCHAR"),
                      ("suggested_source", "VARCHAR"),
                      ("enriching", "INTEGER")],
    # FoodAssistant-v7gj: cook time alongside the existing prep/total time.
    "recipes": [("cook_time", "VARCHAR")],
    # FoodAssistant-zq7k: ingredient section headings (grouped recipes). NULL on
    # every existing row, which reads as ungrouped, exactly as before.
    "recipe_ingredients": [("section", "VARCHAR")],
    # 0.21: what a satellite reports on its config pull (relay caps, client,
    # the observed and reported addresses, release-key fingerprint). NULL on
    # existing rows reads as "not reported"; an older server ignores them.
    "satellite_devices": [("caps", "VARCHAR"), ("client", "VARCHAR"),
                          ("peer", "VARCHAR"), ("reported_ip", "VARCHAR"),
                          ("trust", "VARCHAR"), ("ip_unverified", "INTEGER")],
}

# How many column backfills the last ensure_schema() run could not apply.
_schema_failures = 0


def schema_failures() -> int:
    """Columns the last ensure_schema() run could not add. 0 when all applied."""
    return _schema_failures


def ensure_schema(bind=None) -> None:
    """Backfill columns that create_all cannot add to an existing table.

    Idempotent: each column in _COLUMN_ADDITIONS is checked against
    PRAGMA table_info and added with ALTER TABLE only when missing. A table
    that does not exist yet is skipped (create_all builds it complete).
    Runs right after create_all at startup; best-effort so a schema
    bookkeeping problem never blocks the app from serving.

    Each ALTER is committed on its own: one column that cannot be added must
    not roll back the columns that already succeeded, nor stop the tables after
    it. Failures are logged with the table and column named and counted for
    schema_failures().
    """
    global _schema_failures
    bind = bind or engine
    failures = 0
    try:
        with bind.connect() as conn:
            for table, columns in _COLUMN_ADDITIONS.items():
                try:
                    rows = conn.execute(
                        text(f'PRAGMA table_info("{table}")')).fetchall()
                except Exception:
                    conn.rollback()
                    logger.exception("Could not read the schema of table %s", table)
                    failures += len(columns)
                    continue
                if not rows:
                    continue  # table absent: create_all makes it complete
                existing = {row[1] for row in rows}
                for name, sql_type in columns:
                    if name in existing:
                        continue
                    try:
                        conn.execute(text(
                            f'ALTER TABLE "{table}" ADD COLUMN "{name}" {sql_type}'))
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        failures += 1
                        logger.exception(
                            "Could not add column %s to table %s", name, table)
    except Exception:
        failures += 1
        logger.exception("The schema backfill could not run")
    _schema_failures = failures


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
