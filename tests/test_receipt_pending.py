"""Photo-receipt intake persists parsed items into Pending (FoodAssistant-dq4j).

A receipt scan used to drop its parsed items into a browser-only queue, so they
vanished if the page was left before importing. These tests drive the real app:
posting a receipt image now lands the items in the Pending store (reviewable and
importable from there, like a barcode scan), the food-photo path still returns
items without touching Pending, and committing a pending row removes it so an
accept never double-creates.
"""
import io
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.models.food import AnalysisResult, FoodItem
from app.providers.base import VisionProvider

_SERVICE_DIR = Path(__file__).parent.parent / "service"


class _FakeProvider(VisionProvider):
    """Returns fixed items so no network / real vision model is needed."""

    async def analyze_food(self, image_data, mime_type):
        return AnalysisResult(
            items=[FoodItem(name="Banana", quantity=3)],
            image_type="food",
        )

    async def analyze_receipt(self, image_data, mime_type):
        return AnalysisResult(
            items=[
                FoodItem(name="Whole Milk", quantity=1),
                FoodItem(name="Eggs", quantity=2),
                FoodItem(name="Sourdough Bread", quantity=1),
            ],
            image_type="receipt",
            store="Trader Joe's",
        )

    async def health_check(self):
        return True


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (12, 12), (200, 180, 160)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings

        settings.data_dir = str(tmp_path_factory.mktemp("data"))

        from app.main import app
        from app.dependencies import get_vision_provider

        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        settings.vision_provider = "gemini"
        settings.gemini_api_key = "test-gemini-key"
        settings.auth_required = False
        settings.auth_password = ""

        app.dependency_overrides[get_vision_provider] = lambda: _FakeProvider()
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()
    finally:
        os.chdir(cwd)


@pytest.fixture(autouse=True)
def _mock_grocy(monkeypatch):
    """Keep every Grocy call off the network for the pending endpoints."""
    from app.services.grocy import GrocyClient

    async def _empty_stock(self):
        return []

    async def _import_ok(self, item):
        return {"product_id": 1, "name": item.name}

    monkeypatch.setattr(GrocyClient, "get_stock", _empty_stock)
    monkeypatch.setattr(GrocyClient, "import_item", _import_ok)
    # Budget guard should never trip in tests.
    from app.services import usage
    monkeypatch.setattr(usage, "over_budget", lambda *a, **k: False)


def _clear_pending(client):
    for row in client.get("pending/").json()["items"]:
        client.delete(f"pending/{row['id']}")


def test_receipt_scan_populates_pending(client):
    _clear_pending(client)
    files = {"file": ("receipt.png", _png_bytes(), "image/png")}
    r = client.post("analyze/receipt", files=files)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 3

    # The front end forwards the parsed items straight to Pending.
    saved = client.post("pending/items", json={"items": items, "source": "receipt"})
    assert saved.status_code == 200
    assert saved.json()["added"] == 3

    # They are now durable in the Pending store, reviewable after leaving the page.
    pending = client.get("pending/").json()["items"]
    names = {p["name"] for p in pending}
    assert {"Whole Milk", "Eggs", "Sourdough Bread"} <= names
    milk = next(p for p in pending if p["name"] == "Whole Milk")
    assert milk["source"] == "receipt"
    # Defaults (unit / category / storage / expiry) carry onto the pending rows.
    assert milk["unit"]
    assert milk["category"]
    assert milk["storage_type"]


def test_blank_named_receipt_rows_are_skipped(client):
    _clear_pending(client)
    items = [
        {"name": "Cheddar", "quantity": 1},
        {"name": "   ", "quantity": 1},
    ]
    saved = client.post("pending/items", json={"items": items, "source": "receipt"})
    assert saved.json()["added"] == 1
    assert len(client.get("pending/").json()["items"]) == 1


def test_food_photo_does_not_touch_pending(client):
    _clear_pending(client)
    files = {"file": ("food.png", _png_bytes(), "image/png")}
    r = client.post("analyze/food", files=files)
    assert r.status_code == 200
    assert r.json()["items"][0]["name"] == "Banana"
    # The food-photo path still returns items for the in-page import queue and
    # writes nothing to Pending on its own.
    assert client.get("pending/").json()["items"] == []


def test_committing_pending_removes_the_row(client):
    """Accepting an item imports it and clears the pending row, so a receipt that
    is later accepted from Pending never double-creates."""
    _clear_pending(client)
    saved = client.post(
        "pending/items",
        json={"items": [{"name": "Yogurt", "quantity": 1}], "source": "receipt"},
    )
    row_id = saved.json()["items"][0]["id"]

    commit = client.post("pending/commit", json={"ids": [row_id]})
    assert commit.status_code == 200
    assert commit.json()["imported"] == 1
    assert client.get("pending/").json()["items"] == []
