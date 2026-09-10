from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings
import logging
import os

logger = logging.getLogger(__name__)

os.makedirs(settings.data_dir, exist_ok=True)

DATABASE_URL = f"sqlite:///{settings.data_dir}/foodassistant.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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
