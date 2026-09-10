"""Tests for the Pending duplicate hint and the separate-by-day add behavior.

Covers:
  - stock_has_product: the pure duplicate detector.
  - add_stock: a regression check that two scans of the same product on
    different days post distinct best_before_date values, so Grocy lands them as
    separate stock entries (each keeps its own expiration). Grocy keys stock
    entries by best-before date, so distinct dates means distinct entries.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services.grocy import GrocyClient, stock_has_product
from app.models.food import FoodItem


# --- stock_has_product (pure) ------------------------------------------------

def _stock(*names_amounts):
    return [
        {"product": {"name": n}, "amount": a}
        for n, a in names_amounts
    ]


def test_duplicate_matches_case_insensitively():
    stock = _stock(("Whole Milk", 2))
    assert stock_has_product("whole milk", stock) is True
    assert stock_has_product("WHOLE MILK", stock) is True


def test_duplicate_no_match_for_unknown_name():
    stock = _stock(("Whole Milk", 2))
    assert stock_has_product("Eggs", stock) is False


def test_duplicate_ignores_zero_amount_entries():
    stock = _stock(("Whole Milk", 0))
    assert stock_has_product("Whole Milk", stock) is False


def test_duplicate_empty_name_or_stock():
    assert stock_has_product("", _stock(("Whole Milk", 2))) is False
    assert stock_has_product("Whole Milk", []) is False
    assert stock_has_product("Whole Milk", None) is False


def test_duplicate_handles_flat_name_field():
    # Some stock shapes carry the name at the top level rather than nested.
    stock = [{"name": "Whole Milk", "amount": 1}]
    assert stock_has_product("whole milk", stock) is True


# --- add_stock separate-by-day regression ------------------------------------

class _CaptureClient(GrocyClient):
    """A GrocyClient that records add_stock POST bodies instead of hitting HTTP."""

    def __init__(self):
        # Skip the network-config __init__; we only exercise add_stock.
        self.posts: list[tuple[str, dict]] = []

    async def _post(self, path, body):
        self.posts.append((path, body))
        return {"created_object_id": len(self.posts)}


@pytest.mark.anyio
async def test_add_stock_separates_entries_by_best_before_day():
    """Same product scanned on two days posts two distinct best_before_date
    values. Grocy keys stock by best-before date, so these become separate stock
    entries and each keeps its own expiration."""
    client = _CaptureClient()
    item_day1 = FoodItem(name="Whole Milk", quantity=1, best_by_date=date(2026, 7, 5))
    item_day2 = FoodItem(name="Whole Milk", quantity=1, best_by_date=date(2026, 7, 12))

    await client.add_stock(42, item_day1)
    await client.add_stock(42, item_day2)

    assert len(client.posts) == 2
    bests = [body["best_before_date"] for _, body in client.posts]
    assert bests == ["2026-07-05", "2026-07-12"]
    # Distinct dates => Grocy creates distinct stock entries (separate expiry).
    assert bests[0] != bests[1]


@pytest.mark.anyio
async def test_has_in_stock_empty_name_is_false():
    client = _CaptureClient()
    assert await client.has_in_stock("") is False
