"""Sniff test passed (FoodAssistant-fxnr): the Expiring page's "still good"
action, which pushes an item's best-by out 1, 3, or 5 days.

Covers the pure date math, the per-entry Grocy write path, the /expiring/extend
endpoint (all three deltas, count-cache refresh, outage handling), and the
page markup.
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
    GrocyClient, GrocyError, expiring_by_date, sniff_test_new_date,
    stock_entry_edit_body,
)

TODAY = date(2026, 7, 15)


# --- Pure date math -----------------------------------------------------------

def test_future_date_gains_the_extra_days():
    old = (TODAY + timedelta(days=2)).isoformat()
    assert sniff_test_new_date(old, 3, TODAY) == (TODAY + timedelta(days=5)).isoformat()


def test_expired_date_extends_from_today_not_the_stale_date():
    # The sniff happened today and the item is good NOW: four days past plus
    # +3 must land in the future, never still in the past.
    old = (TODAY - timedelta(days=4)).isoformat()
    assert sniff_test_new_date(old, 3, TODAY) == (TODAY + timedelta(days=3)).isoformat()


def test_expiring_today_extends_from_today():
    assert sniff_test_new_date(TODAY.isoformat(), 1, TODAY) == (
        (TODAY + timedelta(days=1)).isoformat())


def test_undated_entry_stays_undated():
    assert sniff_test_new_date(None, 3, TODAY) is None
    assert sniff_test_new_date("", 5, TODAY) is None


# --- Per-entry Grocy write path -------------------------------------------------

def _client_with_entries(monkeypatch, entries):
    calls = []

    async def fake_get(self, path):
        assert path == "/stock/products/7/entries"
        return entries

    async def fake_request(self, method, path, body=None):
        calls.append((method, path, body))
        return {}

    monkeypatch.setattr(GrocyClient, "_get", fake_get)
    monkeypatch.setattr(GrocyClient, "_request", fake_request)
    return GrocyClient(), calls


def test_extend_best_by_updates_each_entry_from_its_own_date(monkeypatch):
    fresh = (date.today() + timedelta(days=2)).isoformat()
    stale = (date.today() - timedelta(days=1)).isoformat()
    c, calls = _client_with_entries(monkeypatch, [
        {"id": 11, "best_before_date": fresh, "amount": 1},
        {"id": 12, "best_before_date": stale, "amount": 2},
        {"id": 13, "best_before_date": None, "amount": 1},  # undated: untouched
    ])
    result = asyncio.run(c.extend_best_by(7, 3))
    assert result["updated"] == 2
    dates = {path: body["best_before_date"] for _m, path, body in calls}
    assert dates["/stock/entry/11"] == (date.today() + timedelta(days=5)).isoformat()
    assert dates["/stock/entry/12"] == (date.today() + timedelta(days=3)).isoformat()
    assert "/stock/entry/13" not in dates
    # The earliest resulting date drives the expiring row.
    assert result["new_best_by"] == (date.today() + timedelta(days=3)).isoformat()


def test_extend_best_by_with_no_dated_entries_reports_none(monkeypatch):
    c, calls = _client_with_entries(monkeypatch, [{"id": 9, "amount": 1}])
    result = asyncio.run(c.extend_best_by(7, 5))
    assert result == {"product_id": 7, "updated": 0, "new_best_by": None}
    assert calls == []


def _chicken(fridge_date, freezer_date):
    # One chicken going off in the fridge, one kept good in the freezer.
    return [
        {"id": 21, "stock_id": "a", "amount": 1, "location_id": 2,
         "best_before_date": fridge_date, "open": 0},
        {"id": 22, "stock_id": "b", "amount": 1, "location_id": 3,
         "best_before_date": freezer_date, "open": 0},
    ]


def test_a_row_sniff_test_moves_only_that_rows_stock_never_the_freezer(monkeypatch):
    """The fridge chicken passed the sniff test; the frozen one must not
    gain a day it never needed (it moved along with it before)."""
    fridge = (date.today() + timedelta(days=2)).isoformat()
    c, calls = _client_with_entries(monkeypatch, _chicken(fridge, "2027-10-27"))
    result = asyncio.run(c.extend_best_by(7, 1, location_id=2, best_before_date=fridge))
    assert [path for _m, path, _b in calls] == ["/stock/entry/21"]
    assert calls[0][2]["best_before_date"] == (date.today() + timedelta(days=3)).isoformat()
    assert result["updated"] == 1


def test_a_row_sniff_test_leaves_the_same_date_in_another_place(monkeypatch):
    day = (date.today() + timedelta(days=1)).isoformat()
    c, calls = _client_with_entries(monkeypatch, _chicken(day, day))
    asyncio.run(c.extend_best_by(7, 3, location_id=3, best_before_date=day))
    assert [path for _m, path, _b in calls] == ["/stock/entry/22"]


def test_a_sniff_test_without_a_row_never_moves_stock_that_is_not_expiring(monkeypatch):
    # The Review screen's chips name no row: only the stock due within the
    # Expiring page's default week moves.
    fridge = (date.today() - timedelta(days=1)).isoformat()
    c, calls = _client_with_entries(monkeypatch, _chicken(fridge, "2027-10-27"))
    result = asyncio.run(c.extend_best_by(7, 1))
    assert [path for _m, path, _b in calls] == ["/stock/entry/21"]
    assert result["new_best_by"] == (date.today() + timedelta(days=1)).isoformat()


def test_a_row_sniff_test_on_stock_that_is_gone_changes_nothing(monkeypatch):
    c, calls = _client_with_entries(monkeypatch, _chicken("2026-07-10", "2027-10-27"))
    with pytest.raises(GrocyError) as e:
        asyncio.run(c.extend_best_by(7, 1, location_id=2, best_before_date="2026-07-11"))
    assert e.value.kind == "gone"
    assert calls == []


def test_a_row_sniff_test_skips_an_entry_that_moved_since_the_read(monkeypatch):
    # Another writer moved entry 12 between the list read and its turn: it is
    # no longer the row's stock, so it is not extended a second time.
    day = (date.today() + timedelta(days=1)).isoformat()
    later = (date.today() + timedelta(days=4)).isoformat()
    first = [
        {"id": 11, "amount": 1, "location_id": 2, "best_before_date": day},
        {"id": 12, "amount": 2, "location_id": 2, "best_before_date": day},
    ]
    moved = [
        {"id": 11, "amount": 1, "location_id": 2, "best_before_date": day},
        {"id": 12, "amount": 2, "location_id": 2, "best_before_date": later},
    ]
    reads = iter([first, moved, moved])
    calls = []

    async def fake_get(self, path):
        return next(reads)

    async def fake_request(self, method, path, body=None):
        calls.append(path)
        return {}

    monkeypatch.setattr(GrocyClient, "_get", fake_get)
    monkeypatch.setattr(GrocyClient, "_request", fake_request)
    result = asyncio.run(GrocyClient().extend_best_by(7, 1, location_id=2, best_before_date=day))
    assert calls == ["/stock/entry/11"]
    assert result["updated"] == 1


def test_the_write_goes_where_grocy_actually_exposes_stock_entries(monkeypatch):
    """Grocy does NOT expose ``stock`` through its generic /objects API.

    Every sniff test used to PUT /objects/stock/<id> and get back a 400,
    "Entity does not exist or is not exposed", so the button reported success
    while the date never moved. Pin the real endpoint so it cannot regress.
    """
    fresh = (date.today() + timedelta(days=2)).isoformat()
    c, calls = _client_with_entries(monkeypatch, [
        {"id": 11, "best_before_date": fresh, "amount": 1},
    ])
    asyncio.run(c.extend_best_by(7, 3))
    assert [path for _m, path, _b in calls] == ["/stock/entry/11"]
    assert not any(path.startswith("/objects/stock") for _m, path, _b in calls)


def test_an_unpriced_entry_is_not_given_a_zero_price():
    """The edit endpoint writes whatever price it is handed.

    Defaulting an unpriced entry to 0 stamps a real 0 onto it, which silently
    corrupts Grocy's inventory value and price history across the pantry every
    time someone passes a sniff test. Omitting the field leaves it null.
    """
    body = stock_entry_edit_body(
        {"id": 11, "amount": 1, "location_id": 3, "price": None, "open": 0},
        "2026-07-21")
    assert "price" not in body
    assert body == {"amount": 1, "best_before_date": "2026-07-21",
                    "location_id": 3, "open": 0}


def test_a_priced_entry_keeps_its_price_and_place():
    # The endpoint restates the whole row, so anything not passed through is
    # lost: an entry must come back with its own amount, shelf, and price.
    body = stock_entry_edit_body(
        {"id": 12, "amount": 2, "location_id": 5, "price": 3.49, "open": 1},
        "2026-07-21")
    assert body == {"amount": 2, "best_before_date": "2026-07-21",
                    "location_id": 5, "open": 1, "price": 3.49}


def test_an_edit_keeps_the_purchase_date_brand_and_store():
    """The whole row is restated, so anything left out is written null.

    Passing a sniff test, moving an item to the freezer, or stamping a receipt
    price used to blank the purchase date a back-dated receipt set, the note
    holding the brand, and the store the item came from.
    """
    body = stock_entry_edit_body(
        {"id": 13, "amount": 1, "location_id": 3, "open": 0,
         "purchased_date": "2026-07-01", "note": "Cabot",
         "shopping_location_id": 4},
        "2026-07-21")
    assert body == {"amount": 1, "best_before_date": "2026-07-21",
                    "location_id": 3, "open": 0,
                    "purchased_date": "2026-07-01", "note": "Cabot",
                    "shopping_location_id": 4}


def test_an_edit_omits_provenance_the_entry_never_had():
    # Grocy rejects a blank date outright, so an empty value is left out
    # rather than sent along as "".
    body = stock_entry_edit_body(
        {"id": 14, "amount": 1, "location_id": 3, "open": 0,
         "purchased_date": "", "note": None, "shopping_location_id": None},
        "2026-07-21")
    assert body == {"amount": 1, "best_before_date": "2026-07-21",
                    "location_id": 3, "open": 0}


def test_an_entry_with_only_a_hash_id_is_skipped_not_sent_to_a_dead_url(monkeypatch):
    # /stock/entry wants the numeric row id; Grocy's ``stock_id`` hash 404s
    # there, so such an entry is skipped rather than counted as updated.
    fresh = (date.today() + timedelta(days=2)).isoformat()
    c, calls = _client_with_entries(monkeypatch, [
        {"stock_id": "6a28c889c1193", "best_before_date": fresh, "amount": 1},
    ])
    result = asyncio.run(c.extend_best_by(7, 3))
    assert calls == []
    assert result["updated"] == 0 and result["new_best_by"] is None


# --- The endpoint ---------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    # An install with no inventory backend is "not set up", and the setup
    # redirect middleware answers every request with the wizard page, so the
    # endpoint under test never runs. Grocy itself is faked per test.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "test-key", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


@pytest.mark.parametrize("delta", [1, 3, 5])
def test_extend_endpoint_all_three_deltas(client, delta):
    seen = {}

    async def fake_extend(self, product_id, days, **scope):
        seen["args"] = (product_id, days)
        return {"product_id": product_id, "updated": 1, "new_best_by": "2026-08-01"}

    with patch.object(GrocyClient, "extend_best_by", fake_extend):
        r = client.post("/expiring/extend/42", json={"days": delta})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "product_id": 42, "updated": 1,
                        "new_best_by": "2026-08-01"}
    assert seen["args"] == (42, delta)


def test_extend_endpoint_passes_the_rows_place_and_date_on(client):
    seen = {}

    async def fake_extend(self, product_id, days, **scope):
        seen["scope"] = scope
        return {"product_id": product_id, "updated": 1, "new_best_by": "2026-08-01"}

    with patch.object(GrocyClient, "extend_best_by", fake_extend):
        r = client.post("/expiring/extend/42", json={
            "days": 1, "location_id": 2, "best_before_date": "2026-07-29"})
    assert r.status_code == 200
    assert seen["scope"] == {"location_id": 2, "best_before_date": "2026-07-29"}


@pytest.mark.parametrize("body", [
    {"days": 1, "location_id": 2},                              # place without a date
    {"days": 1, "location_id": 2, "best_before_date": "none"},  # nothing to extend
    {"days": 1, "best_before_date": "2026-13-40"},
])
def test_extend_endpoint_refuses_half_a_row(client, body):
    called = []

    async def fake_extend(self, product_id, days, **scope):
        called.append(scope)
        return {}

    with patch.object(GrocyClient, "extend_best_by", fake_extend):
        r = client.post("/expiring/extend/42", json=body)
    assert r.status_code == 422
    assert called == []


def test_extend_endpoint_answers_404_when_the_rows_stock_is_gone(client):
    async def fake_extend(self, product_id, days, **scope):
        raise GrocyError("This row is no longer in stock.", kind="gone")

    with patch.object(GrocyClient, "extend_best_by", fake_extend):
        r = client.post("/expiring/extend/42", json={
            "days": 1, "location_id": 2, "best_before_date": "2026-07-29"})
    assert r.status_code == 404


def test_extend_endpoint_rejects_out_of_range_days(client):
    r = client.post("/expiring/extend/42", json={"days": 0})
    assert r.status_code == 422
    r = client.post("/expiring/extend/42", json={"days": 99})
    assert r.status_code == 422


def test_extend_endpoint_surfaces_grocy_outage_as_502(client):
    async def fake_extend(self, product_id, days, **scope):
        raise GrocyError("Your inventory is not reachable right now. It will return when it is.")

    with patch.object(GrocyClient, "extend_best_by", fake_extend):
        r = client.post("/expiring/extend/42", json={"days": 3})
    assert r.status_code == 502
    assert "not reachable" in r.json()["detail"]


def test_extend_refreshes_the_count_cache(client):
    # The /expiring/count 30s cache must not keep showing the old number
    # right after the user acted on the list.
    from app.routers import expiring as expiring_router
    expiring_router._count_items_cache.set([{"days_remaining": 0}])

    async def fake_extend(self, product_id, days, **scope):
        return {"product_id": product_id, "updated": 1, "new_best_by": "2026-08-01"}

    with patch.object(GrocyClient, "extend_best_by", fake_extend):
        client.post("/expiring/extend/42", json={"days": 1})
    assert expiring_router._count_items_cache.get() is None


# --- The page -------------------------------------------------------------------

def test_expiring_page_offers_the_sniff_test_chips(client):
    async def fake_expiring(self, days=7):
        return [{
            "product_id": 5,
            "product": {"name": "Broccoli"},
            "amount": 1,
            "best_before_date": date.today().isoformat(),
            "days_remaining": 0,
        }]

    with patch.object(GrocyClient, "get_expiring_by_date", fake_expiring), \
         patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/expiring")
    assert r.status_code == 200
    assert "Sniff test" in r.text
    for delta in (1, 3, 5):
        assert f'sniffTest(5, &#34;Broccoli&#34;, {delta})' in r.text


def test_inline_handlers_survive_a_real_product_name(client, monkeypatch):
    """tojson does not escape double quotes, so inside a double-quoted
    onclick attribute the name terminated the attribute early and the
    browser's handler became 'sniffTest(65, ' : Unexpected end of input.
    Every sniff/toss/print click on every install died this way. Render a
    row and prove the handler attribute holds no raw quote."""
    import re
    from app.services.grocy import GrocyClient

    async def fake_exp(self, days=7):
        return [{"product_id": 65, "amount": 1, "days_remaining": -1,
                 "best_before_date": "2026-07-21", "due_type": 1,
                 "product": {"id": 65, "name": "Sriracha Sauce", "location": None}}]
    monkeypatch.setattr(GrocyClient, "get_expiring_by_date", fake_exp)
    html = client.get("/ui/expiring?days=7").text
    for m in re.finditer(r'data-on-click="([^"]*)"', html):
        body = m.group(1)
        if "sniffTest" in body or "tossIt" in body or "printLabel" in body:
            # The handler must be a complete call, not an amputated prefix.
            assert body.rstrip().endswith(")"), body
    assert 'sniffTest(65, "' not in html          # raw quote = broken attribute
    assert "sniffTest(65, &#34;Sriracha Sauce&#34;, 1)" in html


def test_consume_confirm_survives_a_quoted_product_name(client, monkeypatch):
    """The "mark all consumed" confirm() is built from the product name, and
    Open Food Facts names arrive unsanitized. Without forceescape a name with a
    double quote closed the onsubmit attribute: the leftover text landed in the
    tag as live attributes, and for an ordinary name the handler was an
    unterminated string, so confirm() never ran and the form submitted silently.
    """
    import re
    from app.services.grocy import GrocyClient

    name = 'Cheez "It" Crackers'

    async def fake_exp(self, days=7):
        return [{"product_id": 65, "amount": 2, "days_remaining": -1,
                 "best_before_date": "2026-07-21", "due_type": 1,
                 "product": {"id": 65, "name": name, "location": None}}]
    monkeypatch.setattr(GrocyClient, "get_expiring_by_date", fake_exp)
    html = client.get("/ui/expiring?days=7").text

    submits = [m.group(1) for m in re.finditer(r'data-on-submit="([^"]*)"', html)]
    confirms = [s for s in submits if "Mark all" in s]
    assert confirms, "the consume form lost its confirm handler"
    for body in confirms:
        # A complete call, not a prefix amputated at the name's first quote.
        assert body.rstrip().endswith(")"), body
        assert "&#34;" in body          # the name's quotes are escaped entities
    # Nothing of the name escaped the attribute into the tag itself.
    assert 'onmouseover' not in html.lower()
    assert '"It" Crackers as consumed' not in html


# --- Location and amount columns (FoodAssistant-8ycjd) --------------------------

def test_expiring_page_shows_location_unit_and_a_half_amount(client):
    async def fake_expiring(self, days=7):
        return [{
            "product_id": 5,
            "product": {"name": "Broccoli", "location_id": 3},
            "amount": 0.5,
            "best_before_date": date.today().isoformat(),
            "days_remaining": 0,
            "location_name": "Refrigerator",
            "unit_name": "Head",
        }]

    with patch.object(GrocyClient, "get_expiring_by_date", fake_expiring), \
         patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/expiring")
    assert r.status_code == 200
    assert "<td>0.5 Head</td>" in r.text
    assert ">Refrigerator</span>" in r.text


def test_get_expiring_names_the_location_and_unit(monkeypatch):
    soon = (date.today() + timedelta(days=1)).isoformat()
    rows = {
        "/stock": [{"product_id": 7, "amount": 2, "best_before_date": soon,
                    "product": {"id": 7, "name": "Tahini", "location_id": 3,
                                "qu_id_stock": 10}}],
        "/objects/locations": [{"id": 3, "name": "Pantry"}],
        "/objects/quantity_units": [{"id": 10, "name": "Jar"}],
    }

    async def fake_get(self, path):
        return rows[path]

    monkeypatch.setattr(GrocyClient, "_get", fake_get)
    items = asyncio.run(GrocyClient().get_expiring(7))
    assert items[0]["location_name"] == "Pantry"
    assert items[0]["unit_name"] == "Jar"


# --- One row per best-by date -----------------------------------------------------
# Greek Yogurt: 3 best by two days ago and 2 still good for nine days. /stock
# reports one row of 5 with the earliest date, which read as "5 expired", and
# tossing that row binned the good two as well.

def _yogurt():
    stock = [{"product_id": 7, "amount": 5, "amount_aggregated": 5, "value": 9.5,
              "best_before_date": (TODAY - timedelta(days=2)).isoformat(),
              "product": {"id": 7, "name": "Greek Yogurt", "location_id": 2}}]
    rows = [
        {"id": 1, "product_id": 7, "stock_id": "s1", "amount": 3, "location_id": 2,
         "best_before_date": (TODAY - timedelta(days=2)).isoformat(), "open": 0,
         "row_created_timestamp": "2026-07-01 10:00:00"},
        {"id": 2, "product_id": 7, "stock_id": "s2", "amount": 2, "location_id": 2,
         "best_before_date": (TODAY + timedelta(days=9)).isoformat(), "open": 0,
         "row_created_timestamp": "2026-07-10 10:00:00"},
    ]
    return stock, rows


def test_expiring_rows_show_only_the_stock_that_is_expiring():
    stock, rows = _yogurt()
    items = expiring_by_date(stock, rows, 7, TODAY)
    assert len(items) == 1
    row = items[0]
    assert row["amount"] == 3
    assert row["best_before_date"] == (TODAY - timedelta(days=2)).isoformat()
    assert row["days_remaining"] == -2
    assert row["location_id"] == 2 and row["by_date"] is True
    assert row["stock_entry_id"] == "s1"
    assert row["product"]["name"] == "Greek Yogurt"
    # The product's totals would contradict the row's own amount.
    assert "amount_aggregated" not in row and "value" not in row


def test_a_wider_window_gives_each_date_its_own_row_soonest_first():
    stock, rows = _yogurt()
    items = expiring_by_date(stock, rows, 14, TODAY)
    assert [(i["amount"], i["days_remaining"]) for i in items] == [(3, -2), (2, 9)]


def test_each_place_gets_its_own_row_and_far_off_stock_none():
    stock = [{"product_id": 8, "amount": 2, "product": {"id": 8, "name": "Chicken"}}]
    rows = [
        {"id": 1, "product_id": 8, "amount": 1, "location_id": 2,
         "best_before_date": (TODAY + timedelta(days=2)).isoformat()},
        {"id": 2, "product_id": 8, "amount": 1, "location_id": 3,
         "best_before_date": (TODAY + timedelta(days=365)).isoformat()},
        {"id": 3, "product_id": 8, "amount": 1, "location_id": 4, "best_before_date": None},
    ]
    items = expiring_by_date(stock, rows, 30, TODAY)
    assert [(i["location_id"], i["amount"]) for i in items] == [(2, 1)]


def test_get_expiring_by_date_names_the_location_and_unit(monkeypatch):
    stock, rows = _yogurt()
    stock[0]["product"]["qu_id_stock"] = 10
    for row in rows:
        row["best_before_date"] = (date.today() + timedelta(days=1)).isoformat()
    replies = {
        "/stock": stock,
        "/objects/stock": rows,
        "/objects/locations": [{"id": 2, "name": "Fridge"}],
        "/objects/quantity_units": [{"id": 10, "name": "Cup"}],
    }

    async def fake_get(self, path):
        return replies[path]

    monkeypatch.setattr(GrocyClient, "_get", fake_get)
    items = asyncio.run(GrocyClient().get_expiring_by_date(7))
    assert [(i["amount"], i["location_name"], i["unit_name"]) for i in items] == [
        (5, "Fridge", "Cup")]


def _row_page(client, items):
    async def fake_expiring(self, days=7):
        return items

    with patch.object(GrocyClient, "get_expiring_by_date", fake_expiring), \
         patch.object(type(settings), "is_configured", lambda self: True):
        return client.get("/ui/expiring")


def test_a_rows_buttons_send_its_place_and_date(client):
    day = date.today() - timedelta(days=2)
    r = _row_page(client, [{
        "product_id": 7, "product": {"name": "Greek Yogurt"}, "amount": 3.0,
        "best_before_date": day.isoformat(), "days_remaining": -2,
        "location_id": 2, "by_date": True,
    }])
    assert r.status_code == 200
    iso = day.isoformat()
    words = f"3 Greek Yogurt (best by {day:%b} {day.day})"
    assert (f"tossIt(7, &#34;Greek Yogurt&#34;, 3.0, {{&#34;best_before_date&#34;: "
            f"&#34;{iso}&#34;, &#34;location_id&#34;: 2, &#34;place&#34;: &#34;&#34;, "
            f"&#34;what&#34;: &#34;3 Greek Yogurt&#34;, &#34;words&#34;: "
            f"&#34;{words}&#34;}})") in r.text
    assert f"sniffTest(7, &#34;Greek Yogurt&#34;, 1, 2, &#34;{iso}&#34;)" in r.text
    # The check mark asks about this stock and posts its place and date.
    assert f"Mark {words} as consumed?" in r.text
    assert '<input type="hidden" name="location_id" value="2">' in r.text
    assert f'<input type="hidden" name="best_before_date" value="{iso}">' in r.text
    assert "Mark all Greek Yogurt" not in r.text


def test_the_consume_form_takes_only_the_rows_stock(client):
    seen = {}

    async def fake_group(self, product_id, amount, location_id, best_before_date,
                         spoiled=False, expected_amount=None):
        seen["args"] = (product_id, amount, location_id, best_before_date, spoiled)
        return {"product_id": product_id, "consumed": amount, "requested": amount,
                "complete": True}

    async def no_plain_consume(self, *a, **kw):
        raise AssertionError("a row must never consume the whole product")

    with patch.object(GrocyClient, "consume_stock_group", fake_group), \
         patch.object(GrocyClient, "consume_stock", no_plain_consume):
        r = client.post("/ui/consume/7", data={
            "amount": "3", "name": "Greek Yogurt", "location_id": "2",
            "best_before_date": "2026-07-13"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert seen["args"] == (7, 3.0, 2, "2026-07-13", False)
    assert "Greek%20Yogurt%20marked%20as%20consumed" in r.headers["location"]


# --- Review findings: what the page showed, tare weight, place-wide toss -------

def test_the_consume_form_sends_what_the_row_showed_and_says_when_it_changed(client):
    seen = {}

    async def fake_group(self, product_id, amount, location_id, best_before_date,
                         spoiled=False, expected_amount=None):
        seen["expected"] = expected_amount
        raise GrocyError("This row changed since the page loaded: it now holds 2, "
                         "not 3. Nothing was changed.", kind="changed")

    with patch.object(GrocyClient, "consume_stock_group", fake_group):
        r = client.post("/ui/consume/7", data={
            "amount": "3", "name": "Greek Yogurt", "location_id": "2",
            "best_before_date": "2026-07-13", "expected_amount": "3"},
            follow_redirects=False)
    assert seen["expected"] == 3.0
    location = r.headers["location"]
    assert "msg_type=warning" in location
    assert "it%20now%20holds%202" in location and "Error" not in location


def test_the_row_markup_sends_the_amount_it_showed(client):
    day = date.today() - timedelta(days=2)
    r = _row_page(client, [{
        "product_id": 7, "product": {"name": "Flour"}, "amount": 500.0,
        "unit_name": "g", "unit_name_plural": "g", "location_name": "Pantry",
        "best_before_date": day.isoformat(), "days_remaining": -2,
        "location_id": 2, "by_date": True,
    }])
    assert '<input type="hidden" name="expected_amount" value="500.0">' in r.text
    # The question names the unit: "Toss 500 g Flour (best by ...)".
    assert f"500 g Flour (best by {day:%b} {day.day})" in r.text
    assert "&#34;what&#34;: &#34;500 g Flour&#34;" in r.text
    assert "&#34;place&#34;: &#34;Pantry&#34;" in r.text


def test_one_unit_is_named_in_the_singular(client):
    day = date.today() + timedelta(days=1)
    r = _row_page(client, [{
        "product_id": 7, "product": {"name": "Milk"}, "amount": 1.0,
        "unit_name": "Bottle", "unit_name_plural": "Bottles",
        "best_before_date": day.isoformat(), "days_remaining": 1,
        "location_id": 2, "by_date": True,
    }])
    assert f"Mark 1 Bottle Milk (best by {day:%b} {day.day}) as consumed?" in r.text


def test_a_row_without_a_place_never_says_all(client):
    day = date.today() + timedelta(days=1)
    r = _row_page(client, [{
        "product_id": 7, "product": {"name": "Rice"}, "amount": 2.0,
        "best_before_date": day.isoformat(), "days_remaining": 1,
        "location_id": None, "by_date": True,
    }])
    words = f"2 Rice (best by {day:%b} {day.day})"
    assert "all Rice" not in r.text
    assert f"Mark {words} as consumed?" in r.text
    assert f"tossIt(7, &#34;Rice&#34;, 2.0, {{&#34;words&#34;: &#34;{words}&#34;}})" in r.text
    # Without a place the form takes the row's amount, product-wide.
    assert '<input type="hidden" name="amount" value="2.0">' in r.text
    assert 'name="expected_amount"' not in r.text


def test_a_product_weighed_with_its_container_points_to_grocy(client):
    day = date.today() - timedelta(days=1)
    r = _row_page(client, [{
        "product_id": 7, "product": {"name": "Flour"}, "amount": 900.0,
        "best_before_date": day.isoformat(), "days_remaining": -1,
        "location_id": 2, "by_date": True, "tare_weight": True,
    }])
    assert "tossIt(" not in r.text
    assert 'action="ui/consume/7"' not in r.text
    assert "Weighed with its container: use or toss it in Grocy" in r.text
    # The sniff test only moves dates, so it stays.
    assert "sniffTest(7" in r.text


def test_expiring_rows_flag_a_product_weighed_with_its_container():
    stock, rows = _yogurt()
    assert expiring_by_date(stock, rows, 7, TODAY)[0]["tare_weight"] is False
    stock[0]["product"]["enable_tare_weight_handling"] = "1"
    assert expiring_by_date(stock, rows, 7, TODAY)[0]["tare_weight"] is True


def test_the_summary_counts_products_on_their_soonest_row(client):
    today = date.today()

    def row(pid, name, days, loc):
        return {"product_id": pid, "product": {"name": name}, "amount": 1.0,
                "best_before_date": (today + timedelta(days=days)).isoformat(),
                "days_remaining": days, "location_id": loc, "by_date": True}

    from app.routers.expiring import summary_counts
    items = [row(7, "Yogurt", -3, 2), row(7, "Yogurt", -1, 3), row(7, "Yogurt", 2, 2),
             row(8, "Milk", 0, 2), row(9, "Ham", 5, 2), row(9, "Ham", 6, 3)]
    assert summary_counts(items) == {"expired": 1, "today": 1, "within_3_days": 0,
                                     "later": 1}
    r = _row_page(client, items)
    assert '<div class="fs-3 fw-bold urgency-expired">1</div>' in r.text


def test_the_display_prints_the_expired_amount_not_the_product_total(client):
    today = date.today()
    rows = [
        {"product_id": 7, "product": {"name": "Greek Yogurt"}, "amount": 3.0,
         "days_remaining": -2, "best_before_date": (today - timedelta(days=2)).isoformat()},
        {"product_id": 7, "product": {"name": "Greek Yogurt"}, "amount": 1.0,
         "days_remaining": 2, "best_before_date": (today + timedelta(days=2)).isoformat()},
        {"product_id": 8, "product": {"name": "Milk"}, "amount": 0.5,
         "days_remaining": 1, "best_before_date": (today + timedelta(days=1)).isoformat()},
    ]

    async def fake_rows(self, days=7):
        return rows

    with patch.object(GrocyClient, "get_expiring_by_date", fake_rows):
        r = client.get("/expiring/display")
    assert r.text == "EXP: Greek Yogurt x3\n1d: Milk x0.5"


def test_extend_needs_a_place_with_its_date(client):
    called = []

    async def fake_extend(self, product_id, days, **scope):
        called.append(scope)
        return {}

    with patch.object(GrocyClient, "extend_best_by", fake_extend):
        r = client.post("/expiring/extend/42", json={"days": 1, "best_before_date": "2026-07-29"})
    assert r.status_code == 400
    assert called == []


def test_a_raced_row_sniff_test_answers_gone(monkeypatch):
    # The row's only entry moved between the list read and its turn.
    day = (date.today() + timedelta(days=1)).isoformat()
    later = (date.today() + timedelta(days=4)).isoformat()
    reads = iter([[{"id": 11, "amount": 1, "location_id": 2, "best_before_date": day}],
                  [{"id": 11, "amount": 1, "location_id": 2, "best_before_date": later}]])
    calls = []

    async def fake_get(self, path):
        return next(reads)

    async def fake_request(self, method, path, body=None):
        calls.append(path)
        return {}

    monkeypatch.setattr(GrocyClient, "_get", fake_get)
    monkeypatch.setattr(GrocyClient, "_request", fake_request)
    with pytest.raises(GrocyError) as e:
        asyncio.run(GrocyClient().extend_best_by(7, 1, location_id=2, best_before_date=day))
    assert e.value.kind == "gone"
    assert "changed since the page loaded" in str(e.value)
    assert calls == []


def test_a_toss_sends_what_the_row_showed_on(client):
    seen = {}

    async def fake_group(self, product_id, amount, location_id, best_before_date,
                         spoiled=False, expected_amount=None):
        seen["expected"] = expected_amount
        raise GrocyError("This row changed since the page loaded: it now holds 2, "
                         "not 3. Nothing was changed.", kind="changed")

    with patch.object(GrocyClient, "consume_stock_group", fake_group):
        r = client.post("/expiring/toss/42", json={
            "amount": 3.0, "location_id": 2, "best_before_date": "2026-10-25",
            "expected_amount": 3.0})
    assert r.status_code == 409
    assert seen["expected"] == 3.0
    assert "it now holds 2" in r.json()["detail"]


def test_a_toss_blocked_by_an_opened_one_offers_the_place_wide_toss(client):
    async def fake_group(self, *a, **kw):
        err = GrocyError("Grocy would use an opened one first, so nothing was used.",
                         kind="group")
        err.opened_first = True
        raise err

    with patch.object(GrocyClient, "consume_stock_group", fake_group):
        r = client.post("/expiring/toss/42", json={
            "amount": 3.0, "location_id": 2, "best_before_date": "2026-10-25"})
    assert r.status_code == 409
    assert r.json() == {"detail": "Grocy would use an opened one first, so nothing was used.",
                        "place_toss": True}


def test_the_confirmed_place_wide_toss_goes_through_the_place(client):
    seen = {}

    async def fake_place(self, product_id, amount, location_id, best_before_date,
                         spoiled=False, expected_amount=None):
        seen["args"] = (product_id, amount, location_id, best_before_date, spoiled,
                        expected_amount)
        return {"product_id": product_id, "consumed": amount, "requested": amount,
                "complete": True, "place_wide": True}

    async def no_group(self, *a, **kw):
        raise AssertionError("the confirmed step must not try the row alone again")

    with patch.object(GrocyClient, "consume_stock_at_place", fake_place), \
         patch.object(GrocyClient, "consume_stock_group", no_group):
        r = client.post("/expiring/toss/42", json={
            "amount": 3.0, "location_id": 2, "best_before_date": "2026-10-25",
            "expected_amount": 3.0, "place_wide": True})
    assert r.status_code == 200
    assert seen["args"] == (42, 3.0, 2, "2026-10-25", True, 3.0)
    assert r.json()["place_wide"] is True


@pytest.mark.parametrize("body", [
    {"amount": 3.0, "place_wide": True},                                   # no row
    {"location_id": 2, "best_before_date": "2026-10-25", "place_wide": True},  # no amount
])
def test_the_place_wide_toss_needs_the_whole_row(client, body):
    r = client.post("/expiring/toss/42", json=body)
    assert r.status_code == 422
