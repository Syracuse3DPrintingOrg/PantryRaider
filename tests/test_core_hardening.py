"""Launch-audit hardening of the app core (Sep 2026).

Four separate guarantees, all in service/app/main.py and service/app/database.py:

* a state-changing request the browser says came from another site is refused,
  while every headless client (no Sec-Fetch-Site header at all) is untouched;
* the interactive API pages need the password once one is set;
* /health stays public but stops handing its backend error detail to an
  anonymous caller;
* a column backfill that cannot be applied no longer takes the rest of the
  schema down with it, and SQLite runs in WAL where the filesystem allows it.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app import database  # noqa: E402
from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402


@pytest.fixture
def client_factory(monkeypatch, tmp_path):
    """A TestClient on a configured 'server' install, optionally from loopback."""
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True,
                        raising=False)
    from app.services import readiness
    monkeypatch.setattr(readiness, "gate_possible", lambda: False)
    from fastapi.testclient import TestClient
    from app.main import app

    made = []

    def _make(loopback: bool = False):
        # "testclient" is the default client host and is NOT loopback, so the
        # loopback trust path does not mask the behavior under test.
        kwargs = {"client": ("127.0.0.1", 51000)} if loopback else {}
        c = TestClient(app, **kwargs)
        made.append(c)
        return c

    try:
        yield _make
    finally:
        for c in made:
            c.close()
        os.chdir(cwd)


def _with_password(monkeypatch, password="hunter2"):
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret(password),
                        raising=False)


# --- Cross-site write refusal -------------------------------------------------

REFUSED = "came from another site"


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_write_the_browser_calls_cross_site_is_refused(client_factory, site):
    r = client_factory().post("/health", headers={"Sec-Fetch-Site": site})
    assert r.status_code == 403
    assert REFUSED in r.json()["detail"]


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_the_apps_own_pages_and_typed_urls_still_post(client_factory, site):
    r = client_factory().post("/health", headers={"Sec-Fetch-Site": site})
    assert r.status_code == 405  # reached routing, which has no POST /health


def test_a_headless_client_sends_no_such_header_and_is_untouched(client_factory):
    r = client_factory().post("/health")
    assert r.status_code == 405


def test_reading_across_sites_is_still_allowed(client_factory):
    # Only state-changing methods are gated; a cross-site GET of the public
    # fingerprint is how a LAN scan and the fleet dashboard find an instance.
    r = client_factory().get("/health", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200


def test_the_session_cookie_declares_samesite_lax():
    from starlette.middleware.sessions import SessionMiddleware

    from app.main import app
    sessions = [m for m in app.user_middleware if m.cls is SessionMiddleware]
    assert sessions, "the session middleware is no longer installed"
    assert sessions[0].kwargs.get("same_site") == "lax"


# --- The interactive API pages ------------------------------------------------

@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_the_api_schema_needs_the_password_once_one_is_set(
        client_factory, monkeypatch, path):
    _with_password(monkeypatch)
    r = client_factory().get(path, follow_redirects=False)
    assert r.status_code in (302, 303, 307, 401, 403)


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_the_api_schema_is_there_for_an_install_with_no_password(
        client_factory, path):
    assert client_factory().get(path).status_code == 200


def test_the_api_schema_is_readable_from_the_devices_own_screen(
        client_factory, monkeypatch):
    _with_password(monkeypatch)
    assert client_factory(loopback=True).get("/openapi.json").status_code == 200


# --- /health error detail -----------------------------------------------------

@pytest.fixture
def grocy_down(monkeypatch):
    from app import dependencies
    from app.services.grocy import GrocyClient

    async def _no(self):
        return False

    monkeypatch.setattr(GrocyClient, "health_check", _no, raising=False)
    monkeypatch.setattr(dependencies, "get_vision_provider", lambda: object())


def test_health_still_says_which_backend_is_broken(client_factory, grocy_down,
                                                   monkeypatch):
    _with_password(monkeypatch)
    body = client_factory().get("/health").json()
    # The fingerprint and the plain status stay public: that is what a LAN scan
    # and an uptime check read.
    assert body["status"] == "ok" and body["grocy"] == "error"


def test_health_keeps_the_error_detail_from_an_anonymous_caller(
        client_factory, grocy_down, monkeypatch):
    _with_password(monkeypatch)
    body = client_factory().get("/health").json()
    assert "grocy_detail" not in body and "grocy_hint" not in body


def test_health_shows_the_detail_on_the_devices_own_screen(
        client_factory, grocy_down, monkeypatch):
    _with_password(monkeypatch)
    body = client_factory(loopback=True).get("/health").json()
    assert body["grocy_detail"]


def test_health_shows_the_detail_to_an_api_key_client(
        client_factory, grocy_down, monkeypatch):
    _with_password(monkeypatch)
    monkeypatch.setattr(settings, "api_key", "deck-key", raising=False)
    body = client_factory().get("/health", headers={"X-API-Key": "deck-key"}).json()
    assert body["grocy_detail"]


def test_health_shows_the_detail_when_no_password_is_set(client_factory, grocy_down):
    # Nothing to authenticate against, and the detail is the whole point of the
    # troubleshooting docs, so it stays.
    assert client_factory().get("/health").json()["grocy_detail"]


# --- Schema backfill ----------------------------------------------------------

@pytest.fixture
def two_tables(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/schema.db")
    with eng.connect() as conn:
        conn.execute(text("CREATE TABLE alpha (id INTEGER)"))
        conn.execute(text("CREATE TABLE beta (id INTEGER)"))
        conn.commit()
    return eng


def _columns(eng, table):
    with eng.connect() as conn:
        return {r[1] for r in conn.execute(text(f'PRAGMA table_info("{table}")'))}


def test_one_impossible_column_does_not_strand_the_others(
        two_tables, monkeypatch):
    monkeypatch.setattr(database, "_schema_failures", 0, raising=False)
    # SQLite refuses to ALTER a PRIMARY KEY column onto an existing table, so
    # the first entry always fails and the two after it must still land.
    monkeypatch.setattr(database, "_COLUMN_ADDITIONS", {
        "alpha": [("impossible", "INTEGER PRIMARY KEY"), ("later", "VARCHAR")],
        "beta": [("after_the_failure", "VARCHAR")],
    })
    database.ensure_schema(two_tables)
    assert "impossible" not in _columns(two_tables, "alpha")
    assert "later" in _columns(two_tables, "alpha")
    assert "after_the_failure" in _columns(two_tables, "beta")
    assert database.schema_failures() == 1


def test_a_clean_backfill_reports_no_failures(two_tables, monkeypatch):
    monkeypatch.setattr(database, "_schema_failures", 0, raising=False)
    monkeypatch.setattr(database, "_COLUMN_ADDITIONS", {
        "alpha": [("note", "VARCHAR")],
        "absent_table": [("never", "VARCHAR")],
    })
    database.ensure_schema(two_tables)
    assert "note" in _columns(two_tables, "alpha")
    assert database.schema_failures() == 0


def test_the_backfill_is_idempotent(two_tables, monkeypatch):
    monkeypatch.setattr(database, "_schema_failures", 0, raising=False)
    monkeypatch.setattr(database, "_COLUMN_ADDITIONS", {
        "alpha": [("note", "VARCHAR")],
    })
    database.ensure_schema(two_tables)
    database.ensure_schema(two_tables)
    assert database.schema_failures() == 0


def test_health_flags_a_stranded_column_without_naming_anything(
        client_factory, grocy_down, monkeypatch):
    monkeypatch.setattr(database, "_schema_failures", 3, raising=False)
    body = client_factory().get("/health").json()
    assert body["schema"] == "error"
    assert body["schema_columns_pending"] == 3
    assert not any("data_dir" in str(v) or "Traceback" in str(v)
                   for v in body.values())


def test_health_says_nothing_about_the_schema_when_it_is_fine(
        client_factory, grocy_down, monkeypatch):
    monkeypatch.setattr(database, "_schema_failures", 0, raising=False)
    assert "schema" not in client_factory().get("/health").json()


# --- SQLite journal mode ------------------------------------------------------

def test_sqlite_is_put_into_wal_with_the_matching_sync_level(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "_journal_warned", False, raising=False)
    conn = sqlite3.connect(str(tmp_path / "wal.db"))
    try:
        database._sqlite_pragmas(conn, None)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    finally:
        conn.close()


def test_a_filesystem_that_cannot_do_wal_keeps_full_sync(monkeypatch):
    """Unraid's FUSE appdata can refuse WAL shared memory. The mode is read
    back, so synchronous must stay at FULL rather than be lowered blind."""
    monkeypatch.setattr(database, "_journal_warned", False, raising=False)
    ran = []

    class _Cursor:
        def execute(self, sql):
            ran.append(sql)
            return self

        def fetchone(self):
            return ("delete",)

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cursor()

    database._sqlite_pragmas(_Conn(), None)
    assert not any("synchronous" in s for s in ran)


def test_a_connection_that_refuses_pragmas_still_works(monkeypatch):
    monkeypatch.setattr(database, "_journal_warned", False, raising=False)

    class _Conn:
        def cursor(self):
            raise sqlite3.OperationalError("unable to open database file")

    database._sqlite_pragmas(_Conn(), None)  # must not raise
