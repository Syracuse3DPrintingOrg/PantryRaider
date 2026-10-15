"""Community shelf life, end to end through the pending queue (FoodAssistant-ezkh).

The capture point lives on the pending review path: PATCHing an item's date
stashes what the app had suggested (suggested_best_by / suggested_source),
and the commit turns a real correction into one anonymous queued point, only
when the sharing opt-in is on. These tests drive the real routes with Grocy
mocked, mirroring tests/test_pending_provenance.py.
"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

from app.models.food import FoodItem, FoodCategory  # noqa: E402
from app.services import expiry_learning  # noqa: E402

_SERVICE_DIR = Path(__file__).parent.parent / "service"


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
def _mock_grocy_and_upload(monkeypatch):
    """Keep Grocy and the Forager upload off the network."""
    from app.services.grocy import GrocyClient

    counter = {"pid": 500}

    async def _empty_stock(self):
        return []

    async def _import_ok(self, item):
        counter["pid"] += 1
        return {"product_id": counter["pid"], "name": item.name}

    async def _no_flush():
        return {"sent": 0}

    monkeypatch.setattr(GrocyClient, "get_stock", _empty_stock)
    monkeypatch.setattr(GrocyClient, "import_item", _import_ok)
    monkeypatch.setattr(expiry_learning, "flush", _no_flush)
    expiry_learning.clear_queue()
    yield
    expiry_learning.clear_queue()


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


def _scan_and_correct(client, monkeypatch, new_days=60):
    """Scan an item (AI-suggested date), correct the date, return the row.

    Fast-ack (FoodAssistant-x61t) queues a placeholder and looks up the date in
    the background, so the enrichment is driven to completion before the
    correction, which is what the learning capture measures against."""
    import asyncio
    from app.routers import pending as pending_router
    suggested = date.today() + timedelta(days=30)
    monkeypatch.setattr(pending_router, "lookup_barcode",
                        _fake_llm_lookup(suggested))
    monkeypatch.setattr(pending_router, "_spawn_enrichment", lambda *a: None)
    item = client.post("pending/scan",
                       json={"barcode": "078000082401"}).json()["item"]
    asyncio.run(pending_router.enrich_pending_item(item["id"], "078000082401"))
    corrected = (date.today() + timedelta(days=new_days)).isoformat()
    r = client.patch(f"pending/{item['id']}",
                     json={"best_by_date": corrected})
    assert r.status_code == 200
    return r.json()


def test_correction_is_captured_when_sharing_is_on(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "share_expiry_learning", True, raising=False)
    _clear_pending(client)
    row = _scan_and_correct(client, monkeypatch, new_days=60)

    commit = client.post("pending/commit", json={"ids": [row["id"]]})
    assert commit.json()["results"][0]["status"] == "ok"

    points = expiry_learning.queued_points()
    assert len(points) == 1
    point = points[0]
    assert point["name_key"] == "dr pepper zero"
    assert point["barcode"] == "078000082401"
    assert point["storage"] == "fridge"
    assert point["shelf_life_days"] == 60
    assert point["suggested_days"] == 30
    assert point["suggestion_source"] == "llm"
    # Exactly the anonymous fields, nothing extra rides along.
    assert set(point) == {"barcode", "name_key", "storage",
                          "shelf_life_days", "suggested_days",
                          "suggestion_source"}


def test_nothing_is_captured_when_sharing_is_off(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "share_expiry_learning", False, raising=False)
    _clear_pending(client)
    row = _scan_and_correct(client, monkeypatch, new_days=60)

    commit = client.post("pending/commit", json={"ids": [row["id"]]})
    assert commit.json()["results"][0]["status"] == "ok"
    assert expiry_learning.queued_points() == []
    assert not (Path(settings.data_dir) / "expiry_learning_queue.json").exists()


def test_an_unedited_suggestion_is_not_captured(client, monkeypatch):
    """Committing an item whose date the user never touched is not a signal."""
    from app.config import settings
    from app.routers import pending as pending_router
    monkeypatch.setattr(settings, "share_expiry_learning", True, raising=False)
    _clear_pending(client)
    suggested = date.today() + timedelta(days=30)
    monkeypatch.setattr(pending_router, "lookup_barcode",
                        _fake_llm_lookup(suggested))
    monkeypatch.setattr(pending_router, "_spawn_enrichment", lambda *a: None)
    item = client.post("pending/scan",
                       json={"barcode": "078000082401"}).json()["item"]
    commit = client.post("pending/commit", json={"ids": [item["id"]]})
    assert commit.json()["results"][0]["status"] == "ok"
    assert expiry_learning.queued_points() == []
