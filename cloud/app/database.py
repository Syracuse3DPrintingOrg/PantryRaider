"""SQLAlchemy engine and session factory.

The engine is created lazily on first use, so importing the app does not
require the Postgres driver (tests and local smoke imports run on SQLite;
the container installs psycopg2 for production).

Schema management is create_all at startup; before the first real
deployment this switches to Alembic (the upgrade path is documented in
docs/design/cloud-platform.md).
"""
from __future__ import annotations

from sqlalchemy import create_engine
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


def init_db() -> None:
    """Create all tables. Idempotent; replaced by `alembic upgrade head` once
    migrations exist."""
    from . import models  # noqa: F401 - registers models with Base

    Base.metadata.create_all(bind=get_engine())
