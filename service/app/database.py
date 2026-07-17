from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings
import os

os.makedirs(settings.data_dir, exist_ok=True)

DATABASE_URL = f"sqlite:///{settings.data_dir}/foodassistant.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


# Columns added to a table after it first shipped. create_all only creates
# missing TABLES; it never adds a missing column to an existing SQLite table,
# so an upgraded install keeps its old schema unless we ALTER it here.
# Existing installs are production (see AGENTS.md): every entry must be
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
}


def ensure_schema(bind=None) -> None:
    """Backfill columns that create_all cannot add to an existing table.

    Idempotent: each column in _COLUMN_ADDITIONS is checked against
    PRAGMA table_info and added with ALTER TABLE only when missing. A table
    that does not exist yet is skipped (create_all builds it complete).
    Runs right after create_all at startup; best-effort so a schema
    bookkeeping problem never blocks the app from serving.
    """
    bind = bind or engine
    try:
        with bind.connect() as conn:
            for table, columns in _COLUMN_ADDITIONS.items():
                rows = conn.execute(text(f'PRAGMA table_info("{table}")')).fetchall()
                if not rows:
                    continue  # table absent: create_all makes it complete
                existing = {row[1] for row in rows}
                for name, sql_type in columns:
                    if name in existing:
                        continue
                    conn.execute(text(
                        f'ALTER TABLE "{table}" ADD COLUMN "{name}" {sql_type}'))
            conn.commit()
    except Exception:
        pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
