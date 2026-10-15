"""Alembic environment for the Forager cloud service.

The database URL is never hard-coded. It comes from the app's settings
(``CLOUD_DATABASE_URL``), and the engine is built with the app's own
``_make_engine`` so the SQLite StaticPool used by tests and the Postgres
engine used in production behave exactly as they do at runtime.

The autogenerate target is ``Base.metadata`` with every model imported, so a
diff compares the live database against the full ORM schema. ``render_as_batch``
is on for SQLite, which lacks most in-place ALTER support; it is a no-op for
Postgres.
"""
from __future__ import annotations

from alembic import context

# Import the app's engine builder, settings, and metadata. Importing app.models
# registers every table on Base.metadata, which is what autogenerate compares
# against.
from app.config import settings
from app.database import Base, _make_engine
from app import models  # noqa: F401 - registers all models on Base.metadata

# Alembic Config object (values from alembic.ini). May be None if env.py is
# ever run outside the CLI, so guard access to it.
config = context.config

target_metadata = Base.metadata


def _database_url() -> str:
    return settings.database_url


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Emit SQL to the script output without a live connection."""
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(url),
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=_is_sqlite(_database_url()),
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection.

    When the app drives Alembic in-process (database.init_db) it passes its own
    connection via ``config.attributes['connection']`` so the migration runs on
    the very same database (this matters for the in-memory SQLite StaticPool in
    tests, where a second engine would be a different database). The standalone
    CLI has no such connection and builds one the app's way from the settings
    URL.
    """
    connection = config.attributes.get("connection", None) if config else None
    if connection is not None:
        _run(connection)
        return

    connectable = _make_engine(_database_url())
    with connectable.connect() as connection:
        _run(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
