"""SQLAlchemy engine and session factory.

The engine is created lazily on first use, so importing the app does not
require the Postgres driver (tests and local smoke imports run on SQLite;
the container installs psycopg2 for production).

Schema management is Alembic (see migrations/README.md). init_db handles the
three states a deployment can be in safely, and in particular never touches an
existing pre-Alembic database beyond the additive create_all that always ran:
the live production database is brought under Alembic control by a documented
one-time human `alembic stamp head`, not automatically.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    if url.startswith("sqlite"):
        # Test configuration: one shared in-memory database across the app's
        # sessions. Prod is Postgres and takes the plain path below.
        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    return create_engine(url, pool_pre_ping=True)


_engine = None
SessionLocal = sessionmaker(autoflush=False, autocommit=False)


def get_engine():
    global _engine
    if _engine is None:
        _engine = _make_engine(settings.database_url)
        SessionLocal.configure(bind=_engine)
    return _engine


def __getattr__(name: str):
    # `from .database import engine` still works, just lazily.
    if name == "engine":
        return get_engine()
    raise AttributeError(name)


def _alembic_config():
    """An Alembic Config resolved to the files shipped next to the app.

    alembic.ini and migrations/ sit at cloud/ (the container workdir and the
    parent of this app package). script_location is pinned to an absolute path
    so it resolves the same whatever the process's working directory is.
    """
    from alembic.config import Config

    cloud_dir = Path(__file__).resolve().parent.parent
    cfg = Config(str(cloud_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(cloud_dir / "migrations"))
    return cfg


def _run_alembic(command_fn) -> None:
    """Drive an Alembic command against the app's own engine/connection.

    Passing the live connection through config.attributes makes Alembic operate
    on the same database the app uses, which is essential for the in-memory
    SQLite StaticPool (a second engine would be a different database)."""
    cfg = _alembic_config()
    engine = get_engine()
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command_fn(cfg, "head")


def init_db() -> None:
    """Bring the database to the current schema safely.

    Three states, handled distinctly so a live production database is never
    reset or force-stamped:

    (a) Empty database: create every table (create_all) and stamp the Alembic
        baseline, so the fresh database starts life under migration control.
    (b) Already under Alembic (alembic_version present): apply any pending
        migrations with `alembic upgrade head`. Purely additive going forward.
    (c) Existing pre-Alembic database (the live production case: app tables but
        no alembic_version): run the same additive create_all that has always
        run at startup, and STOP. Do NOT auto-stamp and never drop anything.
        Adopting Alembic here is a one-time human `alembic stamp head`, see
        migrations/README.md.
    """
    from . import models  # noqa: F401 - registers models with Base

    from alembic import command

    engine = get_engine()
    tables = set(inspect(engine).get_table_names())
    has_alembic_version = "alembic_version" in tables
    app_tables = tables - {"alembic_version"}

    if not app_tables and not has_alembic_version:
        # (a) Empty database.
        Base.metadata.create_all(bind=engine)
        _run_alembic(command.stamp)
    elif has_alembic_version:
        # (b) Already under Alembic control.
        _run_alembic(command.upgrade)
    else:
        # (c) Existing pre-Alembic database. Additive only; the one-time stamp
        # is a deliberate human step, not an automatic one.
        Base.metadata.create_all(bind=engine)
