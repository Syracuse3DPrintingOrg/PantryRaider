"""One Inventory row per best-by date.

A place that holds a product with more than one best-by date gets a row per
date, asked for with ``dates=split``, so 2 coconut yogurts due in 3 days and a
pack of 8 due in a month are two rows, each with its own amount, opened
count and badge. A place holding one date keeps its one row, and so does
stock with no recorded place. ``dates=auto`` follows the "One row per best-by
date" setting, on by default, which a Pi Remote takes from its server.

Each per-date row's buttons then name the row's place and date, and the
routes check that scope before Grocy hears of it: a malformed one is a 422,
Grocy taking other stock first is a 409 and a row that has gone is a 404,
both with nothing changed. Without a scope every route makes the call it
always made (tests/test_grocy_best_by_groups.py has what the scoped calls
do in Grocy).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config  # noqa: E402
from app.config import Settings, settings  # noqa: E402
from app.services.grocy import GrocyClient, GrocyError  # noqa: E402
from test_satellite_config_auth import _run_sync, _signed  # noqa: E402

FRIDGE, FREEZER, CELLAR = 1, 2, 3
DUST = 2.7755575615629e-17


def _d(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


_LOCATIONS = [{"id": FRIDGE, "name": "Kitchen Fridge"}, {"id": FREEZER, "name": "Freezer"},
              {"id": CELLAR, "name": "Basement Fridge"}]
_GROUPS = [{"id": 2, "name": "Dairy"}]
_UNITS = [{"id": 3, "name": "cup"}]

_STOCK = [
    {"product_id": 5, "amount": 14, "amount_opened": 1, "best_before_date": _d(3),
     "product": {"name": "Coconut Yogurt", "location_id": FRIDGE,
                 "product_group_id": 2, "qu_id_stock": 3}},
    {"product_id": 6, "amount": 4, "best_before_date": _d(10),
     "product": {"name": "Butter", "location_id": FRIDGE, "product_group_id": 2}},
]
# The yogurt has two dates in each place. Its row 13 carries the stock id of
# row 12 (a partial transfer copies it), so a label for either date avoids it
# where it can. The butter has one date in the fridge and one in the freezer.
_STOCK_ROWS = [
    {"id": 11, "product_id": 5, "stock_id": "y11", "amount": 1, "location_id": FRIDGE,
     "open": 1, "best_before_date": _d(3), "row_created_timestamp": "2026-10-01 08:00:00"},
    {"id": 12, "product_id": 5, "stock_id": "y12", "amount": 1, "location_id": FRIDGE,
     "open": 0, "best_before_date": _d(3), "row_created_timestamp": "2026-10-02 09:00:00"},
    {"id": 13, "product_id": 5, "stock_id": "y12", "amount": 8, "location_id": FRIDGE,
     "open": 0, "best_before_date": _d(30), "row_created_timestamp": "2026-10-18 09:30:00"},
    {"id": 14, "product_id": 5, "stock_id": "y14", "amount": 1, "location_id": FREEZER,
     "open": 0, "best_before_date": _d(60), "row_created_timestamp": "2026-09-02 17:00:00"},
    {"id": 15, "product_id": 5, "stock_id": "y15", "amount": 3, "location_id": FREEZER,
     "open": 0, "best_before_date": _d(90), "row_created_timestamp": "2026-09-20 17:00:00"},
    {"id": 16, "product_id": 6, "stock_id": "b16", "amount": 1, "location_id": FRIDGE,
     "open": 0, "best_before_date": _d(10), "row_created_timestamp": "2026-10-10 12:00:00"},
    {"id": 17, "product_id": 6, "stock_id": "b17", "amount": 3, "location_id": FREEZER,
     "open": 0, "best_before_date": _d(80), "row_created_timestamp": "2026-10-11 12:00:00"},
]

_YOGURT = {"product_id": 5, "name": "Coconut Yogurt", "unit": "cup", "category": "Dairy",
           "unit_plural": "cup", "product_group_id": 2, "product_group_name": "Dairy",
           "added_date": "2026-10-18 09:30:00", "product_amount": 14.0}
_BUTTER = {"product_id": 6, "name": "Butter", "unit": None, "category": "Dairy",
           "unit_plural": None, "product_group_id": 2, "product_group_name": "Dairy",
           "added_date": "2026-10-11 12:00:00", "product_amount": 4.0}


def _place(location_id, location_name, bucket):
    return {"location_id": location_id, "location_name": location_name,
            "storage_bucket": bucket}


_IN_FRIDGE = _place(FRIDGE, "Kitchen Fridge", "refrigerated")
_IN_FREEZER = _place(FREEZER, "Freezer", "frozen")

SPLIT_YOGURT = [
    {**_YOGURT, "added_date": "2026-10-02 09:00:00", "amount": 2.0,
     "best_before_date": _d(3), "days_remaining": 3, "urgency": "3d", **_IN_FRIDGE,
     "amount_opened": 1.0, "by_date": True, "stock_entry_id": "y11"},
    {**_YOGURT, "added_date": "2026-10-18 09:30:00", "amount": 8.0,
     "best_before_date": _d(30), "days_remaining": 30, "urgency": "ok", **_IN_FRIDGE,
     "amount_opened": 0.0, "by_date": True, "stock_entry_id": "y12"},
    {**_YOGURT, "added_date": "2026-09-02 17:00:00", "amount": 1.0,
     "best_before_date": _d(60), "days_remaining": 60, "urgency": "ok", **_IN_FREEZER,
     "amount_opened": 0.0, "by_date": True, "stock_entry_id": "y14"},
    {**_YOGURT, "added_date": "2026-09-20 17:00:00", "amount": 3.0,
     "best_before_date": _d(90), "days_remaining": 90, "urgency": "ok", **_IN_FREEZER,
     "amount_opened": 0.0, "by_date": True, "stock_entry_id": "y15"},
]
# One date in each place: the butter's rows are the breakdown's, key for key.
BUTTER_ROWS = [
    {**_BUTTER, "amount": 1.0, "best_before_date": _d(10), "days_remaining": 10,
     "urgency": "ok", **_IN_FRIDGE, "amount_opened": 0.0},
    {**_BUTTER, "amount": 3.0, "best_before_date": _d(80), "days_remaining": 80,
     "urgency": "ok", **_IN_FREEZER, "amount_opened": 0.0},
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


def _rows(product_id, location_id, dates_and_amounts, *, start=100):
    return [{"id": start + n, "product_id": product_id, "stock_id": f"s{start + n}",
             "amount": amount, "location_id": location_id, "open": 0,
             "best_before_date": day}
            for n, (day, amount) in enumerate(dates_and_amounts)]


def _product(product_id, name, amount, best_before):
    return {"product_id": product_id, "amount": amount, "best_before_date": best_before,
            "product": {"name": name, "location_id": FRIDGE}}


# --- The split rows -------------------------------------------------------------

def test_a_place_with_more_than_one_date_gets_a_row_per_date():
    rows = _full_stock(dates="split")
    assert [r for r in rows if r["product_id"] == 5] == SPLIT_YOGURT


def test_a_place_with_one_date_keeps_its_breakdown_row():
    split = [r for r in _full_stock(dates="split") if r["product_id"] == 6]
    breakdown = [r for r in _full_stock(dates="breakdown") if r["product_id"] == 6]
    assert split == breakdown == BUTTER_ROWS


def test_no_row_lists_its_dates_once_they_are_rows():
    assert not any("best_by_dates" in r for r in _full_stock(dates="split"))
    # The other readings carry no per-date keys at all.
    for rows in (_full_stock(), _full_stock(dates="breakdown")):
        assert not any({"by_date", "stock_entry_id"} & set(r) for r in rows)


def test_a_rows_label_avoids_a_stock_id_another_row_carries():
    """y12 sits on a row of each fridge date: the earlier date's label takes
    y11, which only that date has, and the later date has no other choice."""
    labels = {r["best_before_date"]: r["stock_entry_id"]
              for r in _full_stock(dates="split") if r.get("by_date")}
    assert labels == {_d(3): "y11", _d(30): "y12", _d(60): "y14", _d(90): "y15"}


def test_a_products_rows_come_earliest_date_first():
    stock = [_product(7, "Milk", 10, _d(2))]
    rows = (_rows(7, FRIDGE, [(_d(9), 1), (None, 2)])
            + _rows(7, CELLAR, [(_d(9), 3), (_d(2), 4)], start=200))
    out = _full_stock(stock, rows, dates="split")
    # Undated last, and the same date in two places by place.
    assert [(r["location_id"], r["best_before_date"]) for r in out] == [
        (CELLAR, _d(2)), (FRIDGE, _d(9)), (CELLAR, _d(9)), (FRIDGE, None)]


def test_stock_with_no_recorded_place_is_never_split():
    """Grocy cannot take stock with no place by date, so it keeps one row,
    and a product kept nowhere else is filed under its default location."""
    stock = [_product(8, "Kefir", 10, _d(2)), _product(9, "Ghee", 5, _d(3))]
    rows = (_rows(8, FRIDGE, [(_d(2), 1), (_d(5), 2)])
            + _rows(8, None, [(_d(3), 3), (_d(7), 4)], start=200)
            + _rows(9, None, [(_d(3), 2), (_d(30), 3)], start=300))
    out = _full_stock(stock, rows, dates="split")
    kefir = [(r["location_id"], r["best_before_date"], r["amount"], r.get("by_date"))
             for r in out if r["product_id"] == 8]
    assert kefir == [(FRIDGE, _d(2), 1.0, True), (None, _d(3), 7.0, None),
                     (FRIDGE, _d(5), 2.0, True)]
    (ghee,) = [r for r in out if r["product_id"] == 9]
    assert (ghee["location_id"], ghee["amount"], ghee["best_before_date"]) == (FRIDGE, 5.0, _d(3))
    assert "by_date" not in ghee and "best_by_dates" not in ghee


def test_dust_is_no_date_of_its_own():
    stock = [_product(7, "Milk", 2, _d(4))]
    rows = _rows(7, FRIDGE, [(_d(4), 2), (_d(8), DUST)])
    (row,) = _full_stock(stock, rows, dates="split")
    assert (row["amount"], row["best_before_date"]) == (2.0, _d(4))
    assert "by_date" not in row


def test_rows_without_readable_stock_keep_the_plain_row():
    """The browser suite's mock Grocy sends stock rows with only a product id
    and a timestamp."""
    rows = [{"product_id": 5, "row_created_timestamp": "2026-10-18 09:30:00"}]
    plain = _full_stock(_STOCK[:1], rows)
    assert _full_stock(_STOCK[:1], rows, dates="split") == plain
    assert len(plain) == 1 and plain[0]["amount"] == 14.0


def test_a_product_with_no_stock_of_its_own_is_left_out():
    stock = [_product(22, "Sparkling Water", 0, _d(30)), _product(24, "Cream", DUST, _d(4))]
    rows = _rows(24, FRIDGE, [(_d(4), DUST)])
    assert _full_stock(stock, rows, dates="split") == []


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


def _dashboard_readings(client, *urls):
    seen = []

    async def fake_full_stock(self, *, dates=""):
        seen.append(dates)
        return []

    with patch.object(GrocyClient, "get_full_stock", fake_full_stock):
        assert all(client.get(url).status_code == 200 for url in urls)
    return seen


def test_the_dashboard_asks_for_split_and_breakdown_by_name(client):
    assert _dashboard_readings(client, "/inventory/dashboard?sort=name_asc&dates=split",
                               "/inventory/dashboard?dates=breakdown") == [
        "split", "breakdown"]


def test_auto_follows_the_setting_both_ways(client, monkeypatch):
    monkeypatch.setattr(settings, "inventory_rows_per_best_by", True)
    assert _dashboard_readings(client, "/inventory/dashboard?dates=auto") == ["split"]
    monkeypatch.setattr(settings, "inventory_rows_per_best_by", False)
    assert _dashboard_readings(client, "/inventory/dashboard?dates=auto") == ["breakdown"]


def test_the_plain_dashboard_still_calls_get_full_stock_with_no_arguments(client):
    """Every get_full_stock fake in the suite takes only self, and so does
    this one: no ``dates``, an empty one or one it does not know."""
    calls = []

    async def fake_full_stock(self):
        calls.append("plain")
        return []

    with patch.object(GrocyClient, "get_full_stock", fake_full_stock):
        replies = [client.get(url) for url in (
            "/inventory/dashboard", "/inventory/dashboard?dates=",
            "/inventory/dashboard?dates=Split", "/inventory/dashboard?dates=AUTO")]
    assert [r.status_code for r in replies] == [200] * 4
    assert calls == ["plain"] * 4


def test_per_date_rows_reach_their_panels_soonest_first(client, monkeypatch):
    monkeypatch.setattr(settings, "inventory_rows_per_best_by", True)
    reads, cached = _grocy_reads(_STOCK, _STOCK_ROWS)
    with reads, cached:
        auto = client.get("/inventory/dashboard?sort=expiry_asc&dates=auto").json()
        split = client.get("/inventory/dashboard?sort=expiry_asc&dates=split").json()
        by_name = client.get("/inventory/dashboard?sort=name_asc&dates=split").json()
    assert auto == split
    assert [(r["name"], r["best_before_date"]) for r in split["refrigerated"]] == [
        ("Coconut Yogurt", _d(3)), ("Butter", _d(10)), ("Coconut Yogurt", _d(30))]
    assert split["frozen"] == [SPLIT_YOGURT[2], BUTTER_ROWS[1], SPLIT_YOGURT[3]]
    # Sorted by name, a product's rows keep their earliest-first order.
    assert [(r["name"], r["best_before_date"]) for r in by_name["refrigerated"]] == [
        ("Butter", _d(10)), ("Coconut Yogurt", _d(3)), ("Coconut Yogurt", _d(30))]


# --- The scoped routes ----------------------------------------------------------

_BAD_SCOPES = [
    ("POST", "/inventory/consume/1?amount=1&location_id=1&best_before_date=2026-13-01", None),
    ("POST", "/inventory/consume/1?amount=1&location_id=1&best_before_date=20261025", None),
    ("POST", "/inventory/consume/1?amount=1&best_before_date=2026-10-25", None),
    ("POST", "/inventory/consume/1?amount=0&location_id=1&best_before_date=2026-10-25", None),
    ("POST", "/inventory/consume/1?amount=1&location_id=0&best_before_date=none", None),
    ("POST", "/inventory/open/1?location_id=1", None),
    ("POST", "/inventory/open/1?best_before_date=none", None),
    ("POST", "/inventory/open/1?location_id=1&best_before_date=soon", None),
    ("PATCH", "/inventory/edit/1", {"best_before_date": "2026-11-01", "from_location_id": 1}),
    ("PATCH", "/inventory/edit/1", {"best_before_date": "2026-11-01",
                                    "from_best_before_date": "none"}),
    ("PATCH", "/inventory/edit/1", {"best_before_date": "11/01/2026", "from_location_id": 1,
                                    "from_best_before_date": "none"}),
    ("POST", "/inventory/move/1", {"bucket": "frozen", "from_best_before_date": "2026-10-25"}),
    ("POST", "/inventory/move/1", {"bucket": "frozen", "from_location_id": 1,
                                   "from_best_before_date": "yesterday"}),
    ("POST", "/inventory/move/1", {"bucket": "frozen", "from_location_id": 1, "amount": 0}),
    ("POST", "/inventory/move/1", {"bucket": "frozen", "from_location_id": 1, "amount": -2}),
    ("POST", "/inventory/move/1", {"bucket": "frozen", "amount": 2}),
]


@pytest.mark.parametrize("method, url, body", _BAD_SCOPES)
def test_a_malformed_scope_is_refused_before_grocy_hears_of_it(client, method, url, body):
    calls = []

    async def no_grocy(self, method, path, body=None):
        calls.append((method, path))
        raise AssertionError("Grocy was asked")

    with patch.object(GrocyClient, "_request", no_grocy):
        r = client.request(method, url, json=body)
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)
    assert calls == []


def test_the_scoped_routes_hand_the_row_to_the_scoped_calls(client):
    seen = {}

    async def consume_group(self, product_id, amount, location_id, best_before_date,
                            spoiled=False):
        seen["consume"] = (product_id, amount, location_id, best_before_date)
        return {"product_id": product_id, "consumed": 1.0, "requested": 1.0,
                "complete": True}

    async def open_group(self, product_id, location_id, best_before_date):
        seen["open"] = (product_id, location_id, best_before_date)
        return {"product_id": product_id, "opened": 1.0, "new_best_by": None,
                "complete": True}

    async def edit(self, product_id, category=None, best_before_date=None, *,
                   from_location_id=None, from_best_before_date=None):
        seen["edit"] = (product_id, category, best_before_date, from_location_id,
                        from_best_before_date)
        return {"product_id": product_id, "updated": 1}

    async def move(self, product_id, bucket, propose_best_by=None, from_location_id=None,
                   from_best_before_date=None):
        seen["move"] = (product_id, bucket, from_location_id, from_best_before_date)
        return {"product_id": product_id, "moved_amount": 1.0, "left_behind": 0.0}

    async def name_and_group(self, product_id):
        return "Coconut Yogurt", "Dairy"

    with patch.object(GrocyClient, "consume_stock_group", consume_group), \
            patch.object(GrocyClient, "open_stock_group", open_group), \
            patch.object(GrocyClient, "edit_product", edit), \
            patch.object(GrocyClient, "move_product", move), \
            patch.object(GrocyClient, "product_name_and_group", name_and_group):
        replies = [
            client.post("/inventory/consume/5?amount=1&location_id=2&best_before_date=none"),
            client.post("/inventory/open/5?location_id=2&best_before_date=2026-10-25"),
            client.patch("/inventory/edit/5", json={
                "category": "Snacks", "best_before_date": "2026-11-01",
                "from_location_id": 2, "from_best_before_date": "none"}),
            client.post("/inventory/move/5", json={
                "bucket": "frozen", "from_location_id": 2,
                "from_best_before_date": "2026-10-25"}),
        ]
    assert [r.status_code for r in replies] == [200] * 4
    assert seen == {
        "consume": (5, 1.0, 2, "none"),
        "open": (5, 2, "2026-10-25"),
        "edit": (5, "Snacks", "2026-11-01", 2, "none"),
        "move": (5, "frozen", 2, "2026-10-25"),
    }
    assert replies[0].json()["complete"] is True


def test_a_row_without_a_date_makes_the_call_it_always_made(client):
    """These fakes have the signatures the routes always called, so a new
    argument sent along would be a TypeError, and a 500."""
    seen = []

    async def consume(self, product_id, amount=1.0, spoiled=False, location_id=None):
        seen.append(("consume", product_id, amount, location_id))
        return [{"transaction_id": "t"}]

    async def open_one(self, product_id, amount=1.0):
        seen.append(("open", product_id, amount))
        return [{"transaction_id": "t"}]

    async def edit(self, product_id, category=None, best_before_date=None):
        seen.append(("edit", product_id, category, best_before_date))
        return {"product_id": product_id}

    async def move(self, product_id, bucket, propose_best_by=None, from_location_id=None):
        seen.append(("move", product_id, bucket, from_location_id))
        return {"product_id": product_id, "moved_amount": 2.0}

    with patch.object(GrocyClient, "consume_stock", consume), \
            patch.object(GrocyClient, "open_stock", open_one), \
            patch.object(GrocyClient, "edit_product", edit), \
            patch.object(GrocyClient, "move_product", move):
        replies = [
            client.post("/inventory/consume/5?amount=2&location_id=2"),
            client.post("/inventory/open/5"),
            client.patch("/inventory/edit/5", json={"category": "Snacks",
                                                    "best_before_date": None}),
            client.post("/inventory/move/5", json={"bucket": "other", "from_location_id": 2}),
        ]
    assert [r.status_code for r in replies] == [200] * 4
    assert seen == [("consume", 5, 2.0, 2), ("open", 5, 1.0), ("edit", 5, "Snacks", None),
                    ("move", 5, "other", 2)]


_SCOPED_CALLS = [
    ("consume_stock_group", "POST",
     "/inventory/consume/5?amount=1&location_id=2&best_before_date=none", None),
    ("open_stock_group", "POST",
     "/inventory/open/5?location_id=2&best_before_date=2026-10-25", None),
    ("edit_product", "PATCH", "/inventory/edit/5",
     {"best_before_date": "2026-11-01", "from_location_id": 2,
      "from_best_before_date": "2026-10-25"}),
    ("move_product", "POST", "/inventory/move/5",
     {"bucket": "other", "from_location_id": 2, "from_best_before_date": "2026-10-25"}),
]


@pytest.mark.parametrize("name, method, url, body", _SCOPED_CALLS)
@pytest.mark.parametrize("kind, status", [("group", 409), ("gone", 404), ("http", 500)])
def test_a_refusal_keeps_its_own_status(client, name, method, url, body, kind, status):
    """A 409 or 404 says nothing changed and the page reloads; any other
    Grocy failure is still the 500 it always was."""
    async def refuse(self, *args, **kwargs):
        raise GrocyError("Grocy would use other stock first, so nothing was used.", kind=kind)

    with patch.object(GrocyClient, name, refuse):
        r = client.request(method, url, json=body)
    assert r.status_code == status
    assert r.json() == {"detail": "Grocy would use other stock first, so nothing was used."}


# --- The setting ----------------------------------------------------------------

def test_the_switch_is_saved_and_on_by_default(client):
    from app.routers.setup import SetupPayload

    assert Settings.model_fields["inventory_rows_per_best_by"].default is True
    assert "inventory_rows_per_best_by" in config._SAVEABLE
    assert SetupPayload.model_fields["inventory_rows_per_best_by"].default is True
    # A pane that does not post it leaves it alone.
    assert "inventory_rows_per_best_by" not in SetupPayload().model_dump(exclude_unset=True)

    r = client.post("/setup/save", json={"inventory_rows_per_best_by": False})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert settings.inventory_rows_per_best_by is False
    saved = json.loads((Path(settings.data_dir) / "settings.json").read_text())
    assert saved["inventory_rows_per_best_by"] is False


@pytest.fixture
def satellite_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://server:9284")
    monkeypatch.setattr(settings, "upstream_api_key", "shared-key")
    monkeypatch.setattr(settings, "device_id", "dev-1")
    monkeypatch.setattr(settings, "inventory_rows_per_best_by", True)
    object.__setattr__(settings, "server_sourced_fields", set())


def test_a_pi_remote_takes_the_servers_choice(satellite_mode):
    assert "inventory_rows_per_best_by" in config.SATELLITE_PULL_FIELDS
    assert "inventory_rows_per_best_by" in config.SATELLITE_READONLY_FIELDS
    out = _run_sync(lambda nonce: _signed({"inventory_rows_per_best_by": False}, nonce))
    assert out["ok"] is True
    assert "inventory_rows_per_best_by" in out["applied"]
    assert settings.inventory_rows_per_best_by is False


def test_an_unsigned_reply_cannot_flip_it(satellite_mode):
    assert "inventory_rows_per_best_by" not in config.UNSIGNED_COSMETIC_FIELDS
    out = _run_sync(lambda nonce: {"ok": True,
                                   "config": {"inventory_rows_per_best_by": False}})
    assert out["ok"] is True
    assert "inventory_rows_per_best_by" not in out["applied"]
    assert settings.inventory_rows_per_best_by is True


def test_a_move_of_part_hands_the_amount_on(client):
    seen = []

    async def move(self, product_id, bucket, propose_best_by=None, from_location_id=None,
                   from_best_before_date=None, amount=None):
        seen.append((product_id, bucket, from_location_id, from_best_before_date, amount))
        return {"product_id": product_id, "moved_amount": amount, "left_behind": 0.0}

    with patch.object(GrocyClient, "move_product", move):
        replies = [
            client.post("/inventory/move/5", json={
                "bucket": "other", "from_location_id": 2, "amount": 4}),
            client.post("/inventory/move/5", json={
                "bucket": "other", "from_location_id": 2,
                "from_best_before_date": "2026-10-25", "amount": 0.5}),
        ]
    assert [r.status_code for r in replies] == [200] * 2
    assert seen == [(5, "other", 2, None, 4.0), (5, "other", 2, "2026-10-25", 0.5)]
