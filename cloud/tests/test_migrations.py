"""The Alembic baseline must reproduce the full schema and stay in step with
the models.

These run the real migration against a throwaway on-disk SQLite database (an
in-memory StaticPool database cannot be shared with the Alembic connection the
same way across engines, so a temp file keeps it simple and honest)."""
from __future__ import annotations

from alembic import command
from alembic.util.exc import CommandError
from sqlalchemy import create_engine, inspect

from app.database import Base, _alembic_config
from app import models  # noqa: F401 - registers every model on Base.metadata


def _migrated_engine(tmp_path):
    """A fresh SQLite file database brought to head by the baseline migration,
    driven through the exact env.py path the app uses (connection passed via
    config.attributes)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    cfg = _alembic_config()
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    return engine


def test_baseline_creates_every_model_table(tmp_path):
    engine = _migrated_engine(tmp_path)
    tables = set(inspect(engine).get_table_names())

    # Alembic's own bookkeeping table exists, so the database is stamped.
    assert "alembic_version" in tables

    # Every model table the app declares was created by the migration.
    model_tables = set(Base.metadata.tables.keys())
    assert model_tables, "expected models to be registered on Base.metadata"
    assert model_tables <= tables

    # The migration creates exactly the model tables (plus alembic_version),
    # nothing more and nothing less.
    assert tables - {"alembic_version"} == model_tables


def test_baseline_matches_create_all(tmp_path):
    """The migrated schema is identical to Base.metadata.create_all, table for
    table and index for index."""
    migrated = inspect(_migrated_engine(tmp_path))

    fresh = create_engine("sqlite://")
    Base.metadata.create_all(fresh)
    created = inspect(fresh)

    def signature(insp, table):
        cols = {c["name"]: (str(c["type"]), c["nullable"])
                for c in insp.get_columns(table)}
        idx = {i["name"]: (tuple(i["column_names"]), i["unique"])
               for i in insp.get_indexes(table)}
        uq = {tuple(sorted(u["column_names"]))
              for u in insp.get_unique_constraints(table)}
        fk = {(tuple(f["constrained_columns"]), f["referred_table"],
               tuple(f["referred_columns"]),
               f.get("options", {}).get("ondelete"))
              for f in insp.get_foreign_keys(table)}
        pk = tuple(insp.get_pk_constraint(table)["constrained_columns"])
        return cols, idx, uq, fk, pk

    for table in Base.metadata.tables:
        assert signature(migrated, table) == signature(created, table), table


def test_alembic_check_reports_no_drift(tmp_path):
    """`alembic check` on the migrated database finds no difference from the
    models: the migrations and app/models.py are in step."""
    engine = _migrated_engine(tmp_path)
    cfg = _alembic_config()
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        # command.check raises when it detects new upgrade operations; a clean
        # run returns without error.
        try:
            command.check(cfg)
        except CommandError as exc:  # pragma: no cover - failure path
            raise AssertionError(f"alembic check reported drift: {exc}")
