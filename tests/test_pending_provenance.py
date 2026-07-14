"""Best-by provenance on the pending-queue commit path (FoodAssistant-vb60).

/inventory/import already records where an item's best-by date came from
(services/best_by_provenance.py) so printed labels can badge "est."/"AI"
honestly. Barcode scans and receipt review go through the PendingItem table
and routers/pending.commit_pending instead, so these tests cover that path:

  * the additive best_by_source column reaches an EXISTING database through
    database.ensure_schema (SQLite create_all never adds a column to a table
    that already exists), idempotently;
  * the source threads scan -> pending row -> commit, where it is recorded to
    best_by_provenance under the imported product id and name;
  * a date the user edits on the Pending review screen becomes manual (no
    badge), even when the row arrived with an AI/estimate date;
  * a label printed for a committed scanned item badges with the recorded
    source, end to end through /printing/label with Grocy mocked.
"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

from app.models.food import FoodItem, FoodCategory  # noqa: E402
from app.services import best_by_provenance as bbp  # noqa: E402

_SERVICE_DIR = Path(__file__).parent.parent / "service"


# -- Schema guard ------------------------------------------------------------


def _columns(engine, table):
    from sqlalchemy import text
    with engine.connect() as conn:
        rows = conn.execute(text(f'PRAGMA table_info("{table}")')).fetchall()
    return [r[1] for r in rows]


def test_ensure_schema_backfills_existing_database(tmp_path):
    """A database created before the column existed gains it via ALTER TABLE,
    and a second run is a clean no-op (idempotent)."""
    import sqlite3
    from sqlalchemy import create_engine
    from app.database import ensure_schema

    dbfile = tmp_path / "old.db"
    conn = sqlite3.connect(dbfile)
    conn.execute(
        "CREATE TABLE pending_items (id INTEGER PRIMARY KEY, name VARCHAR)")
    conn.execute("INSERT INTO pending_items (name) VALUES ('Milk')")
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{dbfile}")
    ensure_schema(engine)
    cols = _columns(engine, "pending_items")
    assert "best_by_source" in cols

    # Idempotent: a second run neither errors nor duplicates the column.
    ensure_schema(engine)
    assert _columns(engine, "pending_items").count("best_by_source") == 1

    # The pre-existing row survives with a NULL (manual, no badge) source.
    import sqlite3 as s2
    conn = s2.connect(dbfile)
    row = conn.execute(
        "SELECT name, best_by_source FROM pending_items").fetchone()
    conn.close()
    assert row == ("Milk", None)


def test_ensure_schema_noop_on_fresh_database(tmp_path):
    """On a fresh database create_all already builds the full table, and
    ensure_schema leaves it alone (the table-absent case is also a no-op)."""
    from sqlalchemy import create_engine
    from app.database import Base, ensure_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    # Table absent entirely: nothing to do, nothing raised.
    ensure_schema(engine)
    Base.metadata.create_all(bind=engine)
    ensure_schema(engine)
    assert _columns(engine, "pending_items").count("best_by_source") == 1


# -- Source threading through scan / review / commit -------------------------


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings

        settings.data_dir = str(tmp_path_factory.mktemp("data"))

        from app.main import app

        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        settings.auth_required = False
        settings.auth_password = ""

        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


@pytest.fixture(autouse=True)
def _mock_grocy(monkeypatch):
    """Keep Grocy off the network; import_item hands out product ids."""
    from app.services.grocy import GrocyClient

    counter = {"pid": 100}

    async def _empty_stock(self):
        return []

    async def _import_ok(self, item):
        counter["pid"] += 1
        return {"product_id": counter["pid"], "name": item.name}

    monkeypatch.setattr(GrocyClient, "get_stock", _empty_stock)
    monkeypatch.setattr(GrocyClient, "import_item", _import_ok)
    bbp.clear_all()
    yield
    bbp.clear_all()


def _clear_pending(client):
    for row in client.get("pending/").json()["items"]:
        client.delete(f"pending/{row['id']}")


def _fake_llm_lookup(best_by, name="Dr Pepper Zero"):
    async def _lookup(barcode, db):
        return FoodItem(
            name=name,
            category=FoodCategory.beverages,
            best_by_date=best_by,
            best_by_source="llm",
        )
    return _lookup


def test_scan_carries_llm_source_and_commit_records_it(client, monkeypatch):
    from app.routers import pending as pending_router
    _clear_pending(client)
    best_by = date.today() + timedelta(days=30)
    monkeypatch.setattr(pending_router, "lookup_barcode", _fake_llm_lookup(best_by))

    r = client.post("pending/scan", json={"barcode": "078000082401"})
    assert r.status_code == 200
    item = r.json()["item"]
    assert item["best_by_source"] == "llm"
    assert item["best_by_date"] == best_by.isoformat()

    commit = client.post("pending/commit", json={"ids": [item["id"]]})
    assert commit.status_code == 200
    result = commit.json()["results"][0]
    assert result["status"] == "ok"
    # Provenance is on record under the new product id AND the name fallback,
    # so the label renderer badges "AI" for this date.
    assert bbp.lookup(result["product_id"], "Dr Pepper Zero",
                      best_by.isoformat()) == "llm"
    assert bbp.lookup(None, "Dr Pepper Zero", best_by.isoformat()) == "llm"


def test_receipt_item_gets_default_source_and_commit_records_it(client):
    _clear_pending(client)
    # A receipt line with no date: apply_defaults fills one from the category
    # rules, which is an estimate ("default").
    saved = client.post(
        "pending/items",
        json={"items": [{"name": "Whole Milk", "quantity": 1}],
              "source": "receipt"},
    )
    item = saved.json()["items"][0]
    assert item["best_by_source"] == "default"
    assert item["best_by_date"]

    commit = client.post("pending/commit", json={"ids": [item["id"]]})
    result = commit.json()["results"][0]
    assert result["status"] == "ok"
    assert bbp.lookup(result["product_id"], "Whole Milk",
                      item["best_by_date"]) == "default"


def test_user_edited_date_commits_as_manual(client, monkeypatch):
    from app.routers import pending as pending_router
    _clear_pending(client)
    best_by = date.today() + timedelta(days=30)
    monkeypatch.setattr(pending_router, "lookup_barcode", _fake_llm_lookup(best_by))

    item = client.post("pending/scan", json={"barcode": "078000082401"}).json()["item"]
    assert item["best_by_source"] == "llm"

    # The user corrects the date on the Pending review screen: the AI origin
    # no longer describes this date.
    edited = (date.today() + timedelta(days=10)).isoformat()
    patched = client.patch(f"pending/{item['id']}", json={"best_by_date": edited})
    assert patched.status_code == 200
    assert patched.json()["best_by_source"] is None

    commit = client.post("pending/commit", json={"ids": [item["id"]]})
    result = commit.json()["results"][0]
    assert result["status"] == "ok"
    # Nothing recorded: the lookup answers manual (no badge).
    assert bbp.lookup(result["product_id"], "Dr Pepper Zero", edited) == "manual"


def test_committed_scan_badges_on_printed_label(client, monkeypatch):
    """End to end: scan (AI date) -> commit -> /printing/label for the new
    product renders a spec whose best_by_source is "llm"."""
    from app.config import settings
    from app.routers import pending as pending_router
    from app.routers import printing as printing_router
    from app.services import printing as printing_service
    from app.services.grocy import GrocyClient

    _clear_pending(client)
    best_by = date.today() + timedelta(days=30)
    monkeypatch.setattr(pending_router, "lookup_barcode", _fake_llm_lookup(best_by))

    item = client.post("pending/scan", json={"barcode": "078000082401"}).json()["item"]
    result = client.post("pending/commit", json={"ids": [item["id"]]}).json()["results"][0]
    pid = result["product_id"]

    # The committed item as Grocy now reports it.
    async def _full_stock(self):
        return [{"product_id": pid, "name": "Dr Pepper Zero",
                 "added_date": date.today().isoformat(),
                 "best_before_date": best_by.isoformat()}]

    monkeypatch.setattr(GrocyClient, "get_full_stock", _full_stock)
    monkeypatch.setattr(settings, "printing_enabled", True, raising=False)
    monkeypatch.setattr(settings, "label_printer_queue", "Zebra", raising=False)
    monkeypatch.setattr(printing_service, "printing_available", lambda: True)
    monkeypatch.setattr(
        printing_service, "print_bytes",
        lambda queue, data, options=None: printing_service.PrintResult(
            ok=True, job_id="Zebra-1"))

    captured = {}
    real_render = printing_router._render_label_image

    def _spy_render(spec):
        captured["source"] = spec.best_by_source
        return real_render(spec)

    monkeypatch.setattr(printing_router, "_render_label_image", _spy_render)

    r = client.post("/printing/label", json={"product_id": pid})
    assert r.status_code == 200
    assert captured["source"] == "llm"
