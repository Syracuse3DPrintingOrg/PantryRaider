"""The Alembic baseline must reproduce the full schema and stay in step with
the models.

These run the real migration against a throwaway on-disk SQLite database (an
in-memory StaticPool database cannot be shared with the Alembic connection the
same way across engines, so a temp file keeps it simple and honest)."""
from __future__ import annotations

from alembic import command
from alembic.util.exc import CommandError
from sqlalchemy import create_engine, inspect, text

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


def test_report_dedupe_migration_collapses_duplicates(tmp_path):
    """The one-report-per-reporter migration tolerates a library that already
    holds duplicate flags: it collapses each (recipe, member) pair to one row,
    recomputes report_count from distinct reporters, and then adds the unique
    constraint cleanly."""
    engine = create_engine(f"sqlite:///{tmp_path / 'dedupe.db'}")
    cfg = _alembic_config()

    # Bring the schema up to just before the new constraint, then seed a recipe
    # with duplicate flags from the same member plus one from another.
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "a1b2c3d4e5f6")
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO accounts (id, email, password_hash, auth_provider, "
            "email_verified, disabled, failed_logins, locked_until, "
            "totp_secret, totp_enabled, recipe_upload_authorized, created_at) "
            "VALUES (1, 'a@x', '', 'password', 0, 0, 0, '', '', 0, 0, 't'),"
            "(2, 'b@x', '', 'password', 0, 0, 0, '', '', 0, 0, 't')"))
        c.execute(text(
            "INSERT INTO community_recipes (id, title, description, ingredients, "
            "steps, image_url, attribution, submitter_account_id, status, "
            "rating_count, rating_sum, report_count, created_at, updated_at) "
            "VALUES (1, 'T', '', '[]', '[]', '', 'a', 1, 'approved', 0, 0, 3, "
            "'t', 't')"))
        # Member 1 flags recipe 1 three times; member 2 flags it once.
        c.execute(text(
            "INSERT INTO recipe_reports (recipe_id, account_id, reason, "
            "created_at) VALUES (1, 1, 'x', 't'), (1, 1, 'y', 't'), "
            "(1, 1, 'z', 't'), (1, 2, 'w', 't')"))

    # Now apply the dedupe migration.
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")

    with engine.begin() as c:
        rows = c.execute(text(
            "SELECT account_id FROM recipe_reports WHERE recipe_id = 1 "
            "ORDER BY account_id")).fetchall()
        assert [r[0] for r in rows] == [1, 2]  # one per member
        count = c.execute(text(
            "SELECT report_count FROM community_recipes WHERE id = 1")).scalar()
        assert count == 2  # recomputed from distinct reporters


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


def test_hash_reporter_ips_migration_rewrites_raw_rows(tmp_path, monkeypatch):
    """The reporter-key migration replaces every raw "ip:<address>" with the
    same peppered hash the app now writes, leaves account keys alone, and
    keeps the dedupe identity (same address, same key)."""
    import hashlib
    monkeypatch.setenv("CLOUD_REPORT_IP_PEPPER", "migration-pepper")
    engine = create_engine(f"sqlite:///{tmp_path / 'iphash.db'}")
    cfg = _alembic_config()

    # Schema up to just before the rewrite, then seed raw reporter keys.
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "0a1b2c3d4e5f")
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO shared_recipe_reports (share_id, reporter_key, "
            "created_at) VALUES (1, 'ip:203.0.113.9', 't'), "
            "(2, 'ip:203.0.113.9', 't'), (1, 'ip:2001:db8::1', 't'), "
            "(1, 'acct:42', 't')"))

    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")

    def hashed(ip):
        return "ip:" + hashlib.sha256(
            ("migration-pepper" + ip).encode()).hexdigest()[:16]

    with engine.begin() as c:
        rows = c.execute(text(
            "SELECT share_id, reporter_key FROM shared_recipe_reports "
            "ORDER BY id")).fetchall()
    assert rows[0][1] == hashed("203.0.113.9")
    assert rows[1][1] == hashed("203.0.113.9")  # same address, same key
    assert rows[2][1] == hashed("2001:db8::1")  # IPv6 (colons) handled whole
    assert rows[3][1] == "acct:42"              # member keys untouched
    for _, key in rows[:3]:
        assert "203.0.113" not in key and "2001:db8" not in key
