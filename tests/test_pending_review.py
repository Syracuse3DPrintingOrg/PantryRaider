"""The Review screen follows a storage change and offers the sniff test
(FoodAssistant-8kvp).

Changing an item's storage area on the Pending review screen recomputes its
best-by date from the new area's shelf-life rule, mirroring the stock-move
transfer hook (frozen to refrigerated pulls the date in, refrigerated to
frozen pushes it out), and a date the user typed by hand is never shortened.
A review row whose product already sits in stock and is expiring gets the
Expiring page's sniff test chips; a fresh scan does not.

Covers the pure proposal, the pure sniff-eligibility scan of a Grocy stock
list, the PATCH /pending recompute path, the GET /pending payload gating, and
the page wiring.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services.defaults import propose_review_best_by  # noqa: E402
from app.services.grocy import GrocyClient, stock_sniff_candidates  # noqa: E402

TODAY = date(2026, 7, 15)


def _in(days: int) -> date:
    return TODAY + timedelta(days=days)


# --- The pure proposal ---------------------------------------------------------

@pytest.mark.parametrize("old,hand_set,from_s,to_s,dest_days,expected", [
    # A suggested date is only the old area's estimate: recompute it outright
    # from the new area's rule, in both directions.
    (_in(10), False, "refrigerated", "frozen", 180, _in(180)),
    (_in(300), False, "frozen", "refrigerated", 10, _in(10)),
    # Ambient shelves carry different rules (dry pasta vs counter bread), so a
    # suggested date still recomputes across them.
    (_in(7), False, "room_temp", "dry", 730, _in(730)),
    # Restating the same storage is not a change.
    (_in(10), False, "refrigerated", "refrigerated", 14, None),
    # Recomputing onto the date already there proposes nothing.
    (_in(10), False, "frozen", "refrigerated", 10, None),
    # A dateless row adopts the new area's rule (what commit would do anyway).
    (None, False, "refrigerated", "frozen", 180, _in(180)),
    # A hand-typed date follows the transfer-hook semantics: freezing extends...
    (_in(2), True, "refrigerated", "frozen", 180, _in(180)),
    # ... but never shortens, in any direction.
    (_in(400), True, "refrigerated", "frozen", 180, None),
    (_in(300), True, "frozen", "refrigerated", 10, None),
    # Two ambient shelves are the same temperature: hand-typed dates hold.
    (_in(30), True, "dry", "room_temp", 7, None),
    # Unknown destination storage or a missing rule proposes nothing.
    (_in(2), False, "refrigerated", "wine_cellar", 30, None),
    (_in(2), False, "refrigerated", "frozen", None, None),
])
def test_review_proposal_matrix(old, hand_set, from_s, to_s, dest_days, expected):
    assert propose_review_best_by(old, hand_set, from_s, to_s,
                                  dest_days, TODAY) == expected


# --- Sniff eligibility from a stock list (pure) --------------------------------

def test_dated_stock_inside_the_window_is_eligible():
    stock = [{"product_id": 7, "amount": 2,
              "product": {"name": "Whole Milk"},
              "best_before_date": _in(2).isoformat()}]
    assert stock_sniff_candidates(stock, today=TODAY) == {
        "whole milk": {"product_id": 7,
                       "best_before_date": _in(2).isoformat(),
                       "days_remaining": 2},
    }


def test_expired_stock_is_still_eligible_that_is_the_point():
    stock = [{"product_id": 7, "amount": 1, "name": "Yogurt",
              "best_before_date": _in(-3).isoformat()}]
    got = stock_sniff_candidates(stock, today=TODAY)
    assert got["yogurt"]["days_remaining"] == -3


def test_fresh_undated_or_absent_stock_is_not_eligible():
    stock = [
        # Well outside the Expiring page's default window.
        {"product_id": 1, "amount": 1, "name": "Ketchup",
         "best_before_date": _in(60).isoformat()},
        # Undated: nothing to extend.
        {"product_id": 2, "amount": 1, "name": "Salt"},
        # No stock left.
        {"product_id": 3, "amount": 0, "name": "Eggs",
         "best_before_date": _in(1).isoformat()},
        # No product id: /expiring/extend has nothing to call.
        {"amount": 1, "name": "Mystery",
         "best_before_date": _in(1).isoformat()},
    ]
    assert stock_sniff_candidates(stock, today=TODAY) == {}
    assert stock_sniff_candidates([], today=TODAY) == {}


def test_earliest_date_wins_for_a_repeated_name():
    stock = [
        {"product_id": 7, "amount": 1, "name": "Milk",
         "best_before_date": _in(5).isoformat()},
        {"product_id": 7, "amount": 1, "name": "Milk",
         "best_before_date": _in(1).isoformat()},
    ]
    got = stock_sniff_candidates(stock, today=TODAY)
    assert got["milk"]["best_before_date"] == _in(1).isoformat()


def test_the_window_matches_the_expiring_pages_cut():
    # get_expiring keeps delta <= days; the boundary day is in, one past is out.
    on_edge = [{"product_id": 7, "amount": 1, "name": "Milk",
                "best_before_date": _in(7).isoformat()}]
    past_edge = [{"product_id": 7, "amount": 1, "name": "Milk",
                  "best_before_date": _in(8).isoformat()}]
    assert "milk" in stock_sniff_candidates(on_edge, today=TODAY)
    assert stock_sniff_candidates(past_edge, today=TODAY) == {}


# --- The endpoints --------------------------------------------------------------

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(SERVICE)
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
def _empty_grocy_stock(monkeypatch):
    """Keep Grocy off the network; tests that need stock override this."""
    async def _empty(self):
        return []
    monkeypatch.setattr(GrocyClient, "get_stock", _empty)


def _clear_pending(client):
    for row in client.get("pending/").json()["items"]:
        client.delete(f"pending/{row['id']}")


def _queue_milk(client) -> dict:
    """Queue one receipt-style row; the "milk" seed rule dates it."""
    saved = client.post(
        "pending/items",
        json={"items": [{"name": "Whole Milk", "quantity": 1}],
              "source": "receipt"},
    )
    assert saved.status_code == 200
    return saved.json()["items"][0]


def test_area_change_recomputes_a_suggested_date_from_the_rule(client):
    _clear_pending(client)
    item = _queue_milk(client)
    # The seed rule ("milk", refrigerated, 10 days) dated the row.
    assert item["storage_type"] == "refrigerated"
    assert item["best_by_source"] == "default"
    assert item["best_by_date"] == (date.today() + timedelta(days=10)).isoformat()

    # Freezing it pushes the date out to the frozen shelf life.
    frozen = client.patch(f"pending/{item['id']}",
                          json={"storage_type": "frozen"}).json()
    assert frozen["best_by_date"] == (date.today() + timedelta(days=180)).isoformat()
    assert frozen["best_by_source"] == "default"

    # Back to the fridge pulls it in again: a suggested date recomputes
    # outright, shortening included.
    fridge = client.patch(f"pending/{item['id']}",
                          json={"storage_type": "refrigerated"}).json()
    assert fridge["best_by_date"] == (date.today() + timedelta(days=10)).isoformat()
    assert fridge["best_by_source"] == "default"


def test_a_hand_typed_date_extends_on_freezing_but_never_shortens(client):
    _clear_pending(client)
    item = _queue_milk(client)
    typed = (date.today() + timedelta(days=2)).isoformat()
    patched = client.patch(f"pending/{item['id']}",
                           json={"best_by_date": typed}).json()
    assert patched["best_by_source"] is None  # a manual date now

    # Freezing may extend it (the freezer honestly buys more time)...
    frozen = client.patch(f"pending/{item['id']}",
                          json={"storage_type": "frozen"}).json()
    assert frozen["best_by_date"] == (date.today() + timedelta(days=180)).isoformat()

    # ... but a hand-typed date is never shortened by an area change.
    _clear_pending(client)
    item = _queue_milk(client)
    far = (date.today() + timedelta(days=400)).isoformat()
    client.patch(f"pending/{item['id']}", json={"best_by_date": far})
    frozen = client.patch(f"pending/{item['id']}",
                          json={"storage_type": "frozen"}).json()
    assert frozen["best_by_date"] == far
    assert frozen["best_by_source"] is None

    room = client.patch(f"pending/{item['id']}",
                        json={"storage_type": "room_temp"}).json()
    assert room["best_by_date"] == far
    assert room["best_by_source"] is None


def test_a_date_sent_alongside_the_area_change_wins(client):
    # The user's explicit date in the same request is never second-guessed.
    _clear_pending(client)
    item = _queue_milk(client)
    typed = (date.today() + timedelta(days=3)).isoformat()
    patched = client.patch(
        f"pending/{item['id']}",
        json={"storage_type": "frozen", "best_by_date": typed}).json()
    assert patched["best_by_date"] == typed
    assert patched["best_by_source"] is None


def test_a_cleared_date_is_refilled_from_the_new_areas_rule(client):
    # Commit refills a missing date via apply_defaults anyway; the review
    # screen shows that truth as soon as the area is picked.
    _clear_pending(client)
    item = _queue_milk(client)
    client.patch(f"pending/{item['id']}", json={"best_by_date": ""})
    frozen = client.patch(f"pending/{item['id']}",
                          json={"storage_type": "frozen"}).json()
    assert frozen["best_by_date"] == (date.today() + timedelta(days=180)).isoformat()
    assert frozen["best_by_source"] == "default"


def test_sniff_hook_only_on_rows_with_expiring_stock_behind_them(client, monkeypatch):
    _clear_pending(client)
    soon = (date.today() + timedelta(days=2)).isoformat()
    later = (date.today() + timedelta(days=60)).isoformat()

    async def _stock(self):
        return [
            {"product_id": 7, "amount": 2, "product": {"name": "Whole Milk"},
             "best_before_date": soon},
            {"product_id": 8, "amount": 1, "product": {"name": "Ketchup"},
             "best_before_date": later},
        ]

    monkeypatch.setattr(GrocyClient, "get_stock", _stock)
    for name in ("Whole Milk", "Ketchup", "Dragonfruit"):
        client.post("pending/items",
                    json={"items": [{"name": name, "quantity": 1}]})
    rows = {r["name"]: r for r in client.get("pending/").json()["items"]}

    # In stock AND expiring: the chips have everything they need.
    assert rows["Whole Milk"]["duplicate"] is True
    assert rows["Whole Milk"]["sniff"] == {
        "product_id": 7, "best_before_date": soon, "days_remaining": 2}
    # In stock but nowhere near expiring: duplicate badge only, no chips.
    assert rows["Ketchup"]["duplicate"] is True
    assert rows["Ketchup"]["sniff"] is None
    # A fresh scan with no stock behind it: neither.
    assert rows["Dragonfruit"]["duplicate"] is False
    assert rows["Dragonfruit"]["sniff"] is None


def test_grocy_outage_drops_the_hints_but_the_list_loads(client, monkeypatch):
    _clear_pending(client)
    _queue_milk(client)

    async def _down(self):
        raise RuntimeError("grocy is down")

    monkeypatch.setattr(GrocyClient, "get_stock", _down)
    r = client.get("pending/")
    assert r.status_code == 200
    row = r.json()["items"][0]
    assert row["duplicate"] is False and row["sniff"] is None


# --- The page -------------------------------------------------------------------

def test_pending_page_wires_the_sniff_chips(client):
    from app.config import settings
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/pending")
    assert r.status_code == 200
    assert "sniffTest(" in r.text
    assert "expiring/extend/" in r.text
    # Chips render only for rows the server marked eligible.
    assert "item.sniff" in r.text
