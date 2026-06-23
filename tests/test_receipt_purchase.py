"""Receipt purchase-date / store extraction and threading into Grocy."""
import sys
import types
from datetime import date

import pytest

# gemini.py imports google.generativeai at module load; that native dependency
# is not installed in the pure-logic test environment. Stub it so the shared
# receipt parsing helpers (which need no SDK) stay importable.
if "google.generativeai" not in sys.modules:
    sys.modules["google.generativeai"] = types.ModuleType("google.generativeai")

from app.providers.gemini import _parse_receipt, _safe_date
from app.models.food import FoodItem
from app.services.grocy import GrocyClient


def test_safe_date_parses_iso():
    assert _safe_date("2026-01-15") == date(2026, 1, 15)


def test_safe_date_rejects_garbage():
    assert _safe_date("not a date") is None
    assert _safe_date(None) is None
    assert _safe_date("") is None


def test_safe_date_passthrough_date_object():
    d = date(2025, 12, 1)
    assert _safe_date(d) is d


def test_parse_receipt_object_form_flows_date_and_store():
    data = {
        "store": "Trader Joe's",
        "purchase_date": "2026-01-15",
        "items": [
            {"name": "Milk", "quantity": 1},
            {"name": "Eggs", "quantity": 2},
        ],
    }
    result = _parse_receipt(data, default_confidence=0.8, raw="{}")

    assert result.image_type == "receipt"
    assert result.store == "Trader Joe's"
    assert result.purchased_on == date(2026, 1, 15)
    assert len(result.items) == 2
    # The extracted date is threaded onto every item.
    assert all(it.purchased_on == date(2026, 1, 15) for it in result.items)


def test_parse_receipt_object_form_without_date():
    data = {"store": None, "purchase_date": None,
            "items": [{"name": "Bread", "quantity": 1}]}
    result = _parse_receipt(data, default_confidence=0.8, raw="{}")

    assert result.purchased_on is None
    assert result.store is None
    assert result.items[0].purchased_on is None


def test_parse_receipt_bad_date_falls_back_to_none():
    data = {"purchase_date": "yesterday", "items": [{"name": "Cheese"}]}
    result = _parse_receipt(data, default_confidence=0.8, raw="{}")
    assert result.purchased_on is None


def test_parse_receipt_legacy_array_form():
    # Older prompt / model that returns a bare array still works, with no date.
    data = [{"name": "Apples", "quantity": 3}, {"name": "Bananas"}]
    result = _parse_receipt(data, default_confidence=0.85, raw="[]")

    assert len(result.items) == 2
    assert result.purchased_on is None
    assert result.store is None
    assert all(it.purchased_on is None for it in result.items)


def test_parse_receipt_legacy_single_object_form():
    data = {"name": "Yogurt", "quantity": 1}
    result = _parse_receipt(data, default_confidence=0.85, raw="{}")

    assert len(result.items) == 1
    assert result.items[0].name == "Yogurt"
    assert result.purchased_on is None


@pytest.mark.anyio
async def test_add_stock_uses_purchased_on(anyio_backend):
    sent = {}

    class FakeGrocy(GrocyClient):
        def __init__(self):
            pass

        async def _post(self, path, body):
            sent["path"] = path
            sent["body"] = body
            return {}

    item = FoodItem(name="Steak", quantity=1, purchased_on=date(2026, 1, 10))
    await FakeGrocy().add_stock(42, item)

    assert sent["path"] == "/stock/products/42/add"
    assert sent["body"]["purchased_date"] == "2026-01-10"


@pytest.mark.anyio
async def test_add_stock_falls_back_to_today(anyio_backend):
    sent = {}

    class FakeGrocy(GrocyClient):
        def __init__(self):
            pass

        async def _post(self, path, body):
            sent["body"] = body
            return {}

    item = FoodItem(name="Steak", quantity=1)  # no purchased_on
    await FakeGrocy().add_stock(7, item)

    assert sent["body"]["purchased_date"] == date.today().isoformat()
