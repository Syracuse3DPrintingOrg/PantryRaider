"""Every best-by date on an Inventory row.

A product's stock in one place is one Inventory row with the earliest best-by
date, so 2 coconut yogurts due in 3 days and a new pack of 8 due in a month
looked like 10 yogurts all due in 3 days. Asked with ``dates=breakdown``, the
dashboard reads each place from Grocy's raw stock rows and gives a row that
holds more than one date a ``best_by_dates`` list, earliest first. Without it
the reply stays exactly the one the Home Assistant sensor and every other
caller of get_full_stock have always read, and that call keeps taking no
arguments: every get_full_stock fake in the suite takes only ``self``.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services.grocy import (  # noqa: E402
    GrocyClient, best_by_breakdown, stock_by_location_and_date,
)

FRIDGE, FREEZER = 1, 2
DUST = 2.7755575615629e-17   # what a consume across a 0.1 and a 0.2 row leaves


def _d(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


_LOCATIONS = [{"id": FRIDGE, "name": "Kitchen Fridge"}, {"id": FREEZER, "name": "Freezer"}]
_GROUPS = [{"id": 2, "name": "Dairy"}]
_UNITS = [{"id": 3, "name": "cup"}]

# /stock: one row per product, under its default location, with its earliest
# best-by date.
_STOCK = [
    {"product_id": 5, "amount": 14, "amount_opened": 1, "best_before_date": _d(3),
     "product": {"name": "Coconut Yogurt", "location_id": FRIDGE,
                 "product_group_id": 2, "qu_id_stock": 3}},
    {"product_id": 6, "amount": 1, "best_before_date": _d(10),
     "product": {"name": "Butter", "location_id": FRIDGE, "product_group_id": 2}},
]
# /objects/stock: one row per purchase. The yogurt is in two places with two
# dates in each: 2 due in 3 days (one of them open) and a new pack of 8 due in
# a month in the fridge, 1 and 3 more in the freezer.
_STOCK_ROWS = [
    {"id": 11, "product_id": 5, "amount": 1, "location_id": FRIDGE, "open": 1,
     "best_before_date": _d(3), "row_created_timestamp": "2026-10-01 08:00:00"},
    {"id": 12, "product_id": 5, "amount": 1, "location_id": FRIDGE, "open": 0,
     "best_before_date": _d(3), "row_created_timestamp": "2026-10-01 08:00:00"},
    {"id": 13, "product_id": 5, "amount": 8, "location_id": FRIDGE, "open": 0,
     "best_before_date": _d(30), "row_created_timestamp": "2026-10-18 09:30:00"},
    {"id": 14, "product_id": 5, "amount": 1, "location_id": FREEZER, "open": 0,
     "best_before_date": _d(60), "row_created_timestamp": "2026-09-02 17:00:00"},
    {"id": 15, "product_id": 5, "amount": 3, "location_id": FREEZER, "open": 0,
     "best_before_date": _d(90), "row_created_timestamp": "2026-09-20 17:00:00"},
    {"id": 16, "product_id": 6, "amount": 1, "location_id": FRIDGE, "open": 0,
     "best_before_date": _d(10), "row_created_timestamp": "2026-10-10 12:00:00"},
]

_YOGURT = {"product_id": 5, "name": "Coconut Yogurt", "unit": "cup", "category": "Dairy",
           "unit_plural": "cup", "product_group_id": 2, "product_group_name": "Dairy",
           "added_date": "2026-10-18 09:30:00", "product_amount": 14.0}
# The rows get_full_stock() gave for that stock before the breakdown existed,
# key for key.
PLAIN_ROWS = [
    {**_YOGURT, "amount": 10.0, "best_before_date": _d(3), "days_remaining": 3,
     "urgency": "3d", "location_id": FRIDGE, "location_name": "Kitchen Fridge",
     "storage_bucket": "refrigerated", "amount_opened": 1.0},
    {**_YOGURT, "amount": 4.0, "best_before_date": _d(60), "days_remaining": 60,
     "urgency": "ok", "location_id": FREEZER, "location_name": "Freezer",
     "storage_bucket": "frozen", "amount_opened": 0.0},
    {"product_id": 6, "name": "Butter", "unit": None, "category": "Dairy",
     "unit_plural": None, "product_group_id": 2, "product_group_name": "Dairy",
     "added_date": "2026-10-10 12:00:00", "product_amount": 1.0, "amount": 1.0,
     "best_before_date": _d(10), "days_remaining": 10, "urgency": "ok",
     "location_id": FRIDGE, "location_name": "Kitchen Fridge",
     "storage_bucket": "refrigerated", "amount_opened": 0.0},
]


def _grocy_reads(stock, stock_rows):
    async def fake_get(self, path):
        return {"/stock": stock, "/objects/stock": stock_rows}.get(path, [])

    async def fake_cached(self, path):
        return {"/objects/locations": _LOCATIONS, "/objects/product_groups": _GROUPS,
                "/objects/quantity_units": _UNITS}.get(path, [])

    return (patch.object(GrocyClient, "_get", fake_get),
            patch.object(GrocyClient, "_cached_list", fake_cached))


def _full_stock(stock=_STOCK, stock_rows=_STOCK_ROWS, **kwargs) -> list[dict]:
    reads, cached = _grocy_reads(stock, stock_rows)
    with reads, cached:
        return asyncio.run(GrocyClient().get_full_stock(**kwargs))


# --- Grouping the raw stock rows ----------------------------------------------

def test_stock_is_grouped_by_place_and_best_by_date():
    out = stock_by_location_and_date(_STOCK_ROWS)
    assert out[5] == {
        "1": {_d(3): {"amount": 2.0, "amount_opened": 1.0, "added": "2026-10-01 08:00:00"},
              _d(30): {"amount": 8.0, "amount_opened": 0.0, "added": "2026-10-18 09:30:00"}},
        "2": {_d(60): {"amount": 1.0, "amount_opened": 0.0, "added": "2026-09-02 17:00:00"},
              _d(90): {"amount": 3.0, "amount_opened": 0.0, "added": "2026-09-20 17:00:00"}},
    }
    assert out[6] == {
        "1": {_d(10): {"amount": 1.0, "amount_opened": 0.0, "added": "2026-10-10 12:00:00"}}}


def test_undated_stock_and_stock_with_no_place_have_keys_of_their_own():
    rows = [
        {"product_id": 3, "amount": 1, "location_id": None, "best_before_date": None},
        {"product_id": 3, "amount": 2, "location_id": "", "best_before_date": ""},
        {"product_id": 3, "amount": 4, "location_id": 2, "best_before_date": _d(1)},
    ]
    assert stock_by_location_and_date(rows) == {3: {
        "": {None: {"amount": 3.0, "amount_opened": 0.0, "added": None}},
        "2": {_d(1): {"amount": 4.0, "amount_opened": 0.0, "added": None}},
    }}


def test_opened_stock_counts_under_its_own_date():
    rows = [
        {"product_id": 3, "amount": 1, "location_id": 1, "open": 1, "best_before_date": _d(2)},
        {"product_id": 3, "amount": 0.5, "location_id": 1, "open": "1", "best_before_date": _d(2)},
        {"product_id": 3, "amount": 2, "location_id": 1, "open": 0, "best_before_date": _d(2)},
        {"product_id": 3, "amount": 1, "location_id": 1, "open": "0", "best_before_date": _d(6)},
        {"product_id": 3, "amount": 1, "location_id": 1, "best_before_date": _d(6)},
    ]
    parts = stock_by_location_and_date(rows)[3]["1"]
    assert (parts[_d(2)]["amount"], parts[_d(2)]["amount_opened"]) == (3.5, 1.5)
    assert (parts[_d(6)]["amount"], parts[_d(6)]["amount_opened"]) == (2.0, 0.0)


def test_dust_and_rows_without_a_product_are_not_stock():
    rows = [
        {"product_id": 3, "amount": DUST, "location_id": 1, "best_before_date": _d(2)},
        {"product_id": 3, "amount": 0, "location_id": 1, "best_before_date": _d(3)},
        {"product_id": None, "amount": 5, "location_id": 1, "best_before_date": _d(4)},
        {"product_id": 4, "amount": "not a number", "location_id": 1},
        {"product_id": 4, "amount": 1, "location_id": 1, "best_before_date": _d(5)},
    ]
    assert stock_by_location_and_date(rows) == {
        4: {"1": {_d(5): {"amount": 1.0, "amount_opened": 0.0, "added": None}}}}
    assert stock_by_location_and_date(None) == {}


def test_added_is_the_newest_rows_creation_time():
    rows = [
        {"product_id": 3, "amount": 1, "location_id": 1, "best_before_date": _d(2),
         "row_created_timestamp": "2026-10-02 09:00:00"},
        {"product_id": 3, "amount": 1, "location_id": 1, "best_before_date": _d(2),
         "row_created_timestamp": "2026-10-09 18:30:00"},
        {"product_id": 3, "amount": 1, "location_id": 1, "best_before_date": _d(2),
         "row_created_timestamp": "2026-10-05 07:15:00"},
        # No creation time: the purchase date stands in.
        {"product_id": 3, "amount": 1, "location_id": 1, "best_before_date": _d(9),
         "purchased_date": "2026-09-30"},
    ]
    parts = stock_by_location_and_date(rows)[3]["1"]
    assert parts[_d(2)]["added"] == "2026-10-09 18:30:00"
    assert parts[_d(9)]["added"] == "2026-09-30"


def test_the_breakdown_puts_the_earliest_date_first_and_undated_stock_last():
    def part(amount):
        return {"amount": amount, "amount_opened": 0.0, "added": None}

    parts = {None: part(1.0), _d(30): part(8.0), "2999-12-31": part(3.0),
             _d(-2): part(0.5), _d(3): part(2.0)}
    assert best_by_breakdown(parts) == [
        {"best_before_date": _d(-2), "amount": 0.5},
        {"best_before_date": _d(3), "amount": 2.0},
        {"best_before_date": _d(30), "amount": 8.0},
        {"best_before_date": "2999-12-31", "amount": 3.0},
        {"best_before_date": None, "amount": 1.0},
    ]


# --- get_full_stock -----------------------------------------------------------

def test_the_plain_reading_is_unchanged():
    rows = _full_stock()
    assert rows == PLAIN_ROWS
    assert not any("best_by_dates" in r for r in rows)


def test_the_breakdown_lists_each_places_dates_earliest_first():
    rows = _full_stock(dates="breakdown")
    assert rows[0]["best_by_dates"] == [{"best_before_date": _d(3), "amount": 2.0},
                                        {"best_before_date": _d(30), "amount": 8.0}]
    assert rows[1]["best_by_dates"] == [{"best_before_date": _d(60), "amount": 1.0},
                                        {"best_before_date": _d(90), "amount": 3.0}]
    # The butter has one date, and its row already shows it.
    assert "best_by_dates" not in rows[2]
    # Everything else on the rows is the plain reading's.
    assert [{k: v for k, v in r.items() if k != "best_by_dates"} for r in rows] == PLAIN_ROWS


def test_a_parents_row_takes_its_best_by_from_its_own_stock():
    """/stock dates a parent product by its sub-products' stock too, so a
    parent kept in one place showed a date that was none of its own. The
    breakdown reads the parent's own rows, so the row's date is the first one
    its line lists."""
    stock = [
        {"product_id": 20, "amount": 5, "amount_aggregated": 6, "best_before_date": _d(2),
         "product": {"name": "Yogurt", "location_id": FRIDGE}},
        {"product_id": 21, "amount": 1, "best_before_date": _d(2),
         "product": {"name": "Vanilla Yogurt", "location_id": FRIDGE,
                     "parent_product_id": 20}},
    ]
    rows = [
        {"product_id": 20, "amount": 2, "location_id": FRIDGE, "open": 0,
         "best_before_date": _d(5)},
        {"product_id": 20, "amount": 3, "location_id": FRIDGE, "open": 0,
         "best_before_date": _d(12)},
        {"product_id": 21, "amount": 1, "location_id": FRIDGE, "open": 0,
         "best_before_date": _d(2)},
    ]
    plain = {r["name"]: r for r in _full_stock(stock, rows)}
    assert plain["Yogurt"]["best_before_date"] == _d(2)    # the vanilla one's
    out = {r["name"]: r for r in _full_stock(stock, rows, dates="breakdown")}
    assert (out["Yogurt"]["best_before_date"], out["Yogurt"]["days_remaining"]) == (_d(5), 5)
    assert out["Yogurt"]["best_by_dates"] == [{"best_before_date": _d(5), "amount": 2.0},
                                             {"best_before_date": _d(12), "amount": 3.0}]
    assert out["Vanilla Yogurt"]["best_before_date"] == _d(2)


def test_a_product_with_no_stock_of_its_own_is_left_out():
    """/stock lists a parent product whose stock all sits in its sub-products
    with an amount of 0, and a product reduced to float dust as well."""
    stock = [
        {"product_id": 22, "amount": 0, "amount_aggregated": 4, "best_before_date": _d(30),
         "product": {"name": "Sparkling Water", "location_id": FRIDGE}},
        {"product_id": 23, "amount": 4, "best_before_date": _d(30),
         "product": {"name": "Lime Sparkling Water", "location_id": FRIDGE,
                     "parent_product_id": 22}},
        {"product_id": 24, "amount": DUST, "best_before_date": _d(4),
         "product": {"name": "Cream", "location_id": FRIDGE}},
    ]
    rows = [
        {"product_id": 23, "amount": 4, "location_id": FRIDGE, "open": 0,
         "best_before_date": _d(30)},
        {"product_id": 24, "amount": DUST, "location_id": FRIDGE, "open": 0,
         "best_before_date": _d(4)},
    ]
    assert [r["name"] for r in _full_stock(stock, rows)] == [
        "Sparkling Water", "Lime Sparkling Water", "Cream"]
    assert [r["name"] for r in _full_stock(stock, rows, dates="breakdown")] == [
        "Lime Sparkling Water"]


def test_dust_left_in_a_second_place_is_no_row_of_its_own():
    stock = [{"product_id": 7, "amount": 3, "best_before_date": _d(2),
              "product": {"name": "Milk", "location_id": FRIDGE}}]
    rows = [
        {"product_id": 7, "amount": 3, "location_id": FRIDGE, "open": 0,
         "best_before_date": _d(2)},
        {"product_id": 7, "amount": DUST, "location_id": FREEZER, "open": 0,
         "best_before_date": _d(9)},
    ]
    assert [r["location_id"] for r in _full_stock(stock, rows)] == [FRIDGE, FREEZER]
    out = _full_stock(stock, rows, dates="breakdown")
    assert [(r["location_id"], r["amount"]) for r in out] == [(FRIDGE, 3.0)]
    assert "best_by_dates" not in out[0]


def test_a_product_without_readable_stock_rows_keeps_its_plain_row():
    """Stock rows with no amount (the browser suite's mock Grocy sends only a
    product id and a timestamp) leave the row as the plain reading has it."""
    rows = [{"product_id": 5, "row_created_timestamp": "2026-10-18 09:30:00"}]
    plain = _full_stock(_STOCK[:1], rows)
    assert len(plain) == 1 and plain[0]["amount"] == 14.0
    assert _full_stock(_STOCK[:1], rows, dates="breakdown") == plain


# --- The dashboard ------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    # Without an inventory backend the setup redirect answers every request
    # with the wizard page. Grocy itself is faked per test.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "test-key", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_the_dashboard_reads_the_plain_rows_with_no_arguments(client):
    """Like every get_full_stock fake in the suite, this one takes only self,
    so it fails if the plain reading passes anything at all, for an empty or
    an unknown ``dates`` too."""
    calls = []

    async def fake_full_stock(self):
        calls.append("plain")
        return [dict(PLAIN_ROWS[2])]

    with patch.object(GrocyClient, "get_full_stock", fake_full_stock):
        replies = [client.get(url) for url in (
            "/inventory/dashboard", "/inventory/dashboard?sort=name_asc&dates=",
            "/inventory/dashboard?dates=bogus")]
    assert [r.status_code for r in replies] == [200, 200, 200]
    assert calls == ["plain"] * 3
    assert replies[0].json()["refrigerated"] == [PLAIN_ROWS[2]]
    assert replies[0].json() == replies[1].json() == replies[2].json()


def test_the_dashboard_asks_for_the_breakdown_only_by_name(client):
    seen = []

    async def fake_full_stock(self, *, dates=""):
        seen.append(dates)
        return []

    with patch.object(GrocyClient, "get_full_stock", fake_full_stock):
        r = client.get("/inventory/dashboard?sort=expiry_asc&dates=breakdown")
    assert r.status_code == 200
    assert seen == ["breakdown"]


def test_the_dashboard_lists_a_rows_dates_when_asked(client):
    reads, cached = _grocy_reads(_STOCK, _STOCK_ROWS)
    with reads, cached:
        plain = client.get("/inventory/dashboard?sort=expiry_asc").json()
        breakdown = client.get("/inventory/dashboard?sort=expiry_asc&dates=breakdown").json()
    # Without the parameter, the reply Home Assistant reads.
    assert plain["refrigerated"] == [PLAIN_ROWS[0], PLAIN_ROWS[2]]
    assert plain["frozen"] == [PLAIN_ROWS[1]]
    fridge = {r["name"]: r for r in breakdown["refrigerated"]}
    assert fridge["Coconut Yogurt"]["best_by_dates"] == [
        {"best_before_date": _d(3), "amount": 2.0}, {"best_before_date": _d(30), "amount": 8.0}]
    assert "best_by_dates" not in fridge["Butter"]
    assert [r["best_by_dates"] for r in breakdown["frozen"]] == [
        [{"best_before_date": _d(60), "amount": 1.0}, {"best_before_date": _d(90), "amount": 3.0}]]
    assert breakdown["_product_groups"] == plain["_product_groups"] == [{"id": 2, "name": "Dairy"}]
