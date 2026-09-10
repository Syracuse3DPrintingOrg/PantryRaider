"""Toss it and the waste picture (FoodAssistant-64eg).

Covers the pure spoiled-consume aggregation (byte-exact fixtures), the
degrade-quietly loader, the /expiring/toss endpoint (amount passthrough,
stock lookup when no amount is sent, missing stock, outage, count-cache
refresh), and the Expiring page markup (toss buttons, the Waste table, the
honest empty state, and no waste card at all during an outage).
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services.grocy import GrocyClient, GrocyError  # noqa: E402
from app.services.waste import load_waste, waste_summary  # noqa: E402

PRODUCTS = {"1": "Milk", "2": "Spinach", "3": "Yogurt"}

# A realistic raw stock_log: Grocy logs consumes with negative amounts and
# keeps the spoiled flag on the row. Milk: 3 consumed, 1 of it spoiled.
# Spinach: 2 consumed, both spoiled. Yogurt: eaten, never spoiled.
LOG = [
    {"product_id": 1, "transaction_type": "consume", "amount": -1.0, "spoiled": 0, "undone": 0},
    {"product_id": 1, "transaction_type": "consume", "amount": -1.0, "spoiled": 1, "undone": 0},
    {"product_id": 1, "transaction_type": "consume", "amount": -1.0, "spoiled": 0, "undone": 0},
    {"product_id": 2, "transaction_type": "consume", "amount": -2.0, "spoiled": 1, "undone": 0},
    {"product_id": 2, "transaction_type": "consume", "amount": -1.0, "spoiled": "1", "undone": 0},
    {"product_id": 3, "transaction_type": "consume", "amount": -4.0, "spoiled": 0, "undone": 0},
    # Noise the aggregation must ignore: purchases, an undone toss, and a row
    # with no product id.
    {"product_id": 1, "transaction_type": "purchase", "amount": 6.0, "spoiled": 0, "undone": 0},
    {"product_id": 3, "transaction_type": "consume", "amount": -1.0, "spoiled": 1, "undone": 1},
    {"product_id": None, "transaction_type": "consume", "amount": -1.0, "spoiled": 1, "undone": 0},
]


# --- Pure aggregation (byte-exact) -------------------------------------------

def test_waste_summary_exact_output():
    assert waste_summary(LOG, PRODUCTS) == [
        {"name": "Spinach", "times_tossed": 2, "amount_tossed": 3.0,
         "amount_consumed_total": 3.0, "share": 1.0},
        {"name": "Milk", "times_tossed": 1, "amount_tossed": 1.0,
         "amount_consumed_total": 3.0, "share": 0.3333},
    ]


def test_waste_summary_no_spoilage_is_empty():
    eaten_only = [r for r in LOG if not r.get("spoiled") or r.get("undone")]
    assert waste_summary(eaten_only, PRODUCTS) == []
    assert waste_summary([], PRODUCTS) == []
    assert waste_summary(None, PRODUCTS) == []


def test_waste_summary_unknown_product_gets_a_placeholder_name():
    rows = [{"product_id": 99, "transaction_type": "consume",
             "amount": -1.0, "spoiled": 1, "undone": 0}]
    assert waste_summary(rows, PRODUCTS) == [
        {"name": "Product 99", "times_tossed": 1, "amount_tossed": 1.0,
         "amount_consumed_total": 1.0, "share": 1.0},
    ]


def test_waste_summary_sorts_most_wasted_first_then_by_name():
    rows = [
        {"product_id": 1, "transaction_type": "consume", "amount": -1.0, "spoiled": 1, "undone": 0},
        {"product_id": 3, "transaction_type": "consume", "amount": -1.0, "spoiled": 1, "undone": 0},
        {"product_id": 2, "transaction_type": "consume", "amount": -5.0, "spoiled": 1, "undone": 0},
    ]
    names = [w["name"] for w in waste_summary(rows, PRODUCTS)]
    assert names == ["Spinach", "Milk", "Yogurt"]


# --- The loader ---------------------------------------------------------------

def test_load_waste_reads_the_raw_log_and_slices_the_top(monkeypatch):
    async def fake_get(self, path):
        assert path.startswith("/objects/stock_log?")
        return LOG

    async def fake_products(self):
        return [{"id": int(pid), "name": name} for pid, name in PRODUCTS.items()]

    monkeypatch.setattr(GrocyClient, "_get", fake_get)
    monkeypatch.setattr(GrocyClient, "get_products", fake_products)
    top = asyncio.run(load_waste(GrocyClient(), top=1))
    assert [w["name"] for w in top] == ["Spinach"]


def test_load_waste_degrades_to_empty_on_any_trouble(monkeypatch):
    async def broken_get(self, path):
        raise GrocyError("Grocy is not reachable. Inventory will return when it is.")

    monkeypatch.setattr(GrocyClient, "_get", broken_get)
    assert asyncio.run(load_waste(GrocyClient())) == []


# --- The endpoint -------------------------------------------------------------

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


def test_toss_endpoint_consumes_the_sent_amount_as_spoiled(client):
    seen = {}

    async def fake_consume(self, product_id, amount=1.0, spoiled=False):
        seen["args"] = (product_id, amount, spoiled)
        return {}

    with patch.object(GrocyClient, "consume_stock", fake_consume):
        r = client.post("/expiring/toss/42", json={"amount": 2.0})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "product_id": 42, "amount": 2.0}
    assert seen["args"] == (42, 2.0, True)


def test_toss_without_an_amount_tosses_what_is_in_stock(client):
    # The Review screen's toss link does not know the stock amount, so the
    # server looks it up and tosses all of it.
    seen = {}

    async def fake_stock(self):
        return [{"product_id": 7, "amount": 3.0}, {"product_id": 42, "amount": 2.5}]

    async def fake_consume(self, product_id, amount=1.0, spoiled=False):
        seen["args"] = (product_id, amount, spoiled)
        return {}

    with patch.object(GrocyClient, "get_stock", fake_stock), \
         patch.object(GrocyClient, "consume_stock", fake_consume):
        r = client.post("/expiring/toss/42")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "product_id": 42, "amount": 2.5}
    assert seen["args"] == (42, 2.5, True)


def test_toss_with_nothing_in_stock_is_a_404(client):
    async def fake_stock(self):
        return [{"product_id": 7, "amount": 3.0}]

    with patch.object(GrocyClient, "get_stock", fake_stock):
        r = client.post("/expiring/toss/42")
    assert r.status_code == 404
    assert "no stock" in r.json()["detail"].lower()


def test_toss_rejects_a_non_positive_amount(client):
    r = client.post("/expiring/toss/42", json={"amount": 0})
    assert r.status_code == 422
    r = client.post("/expiring/toss/42", json={"amount": -1})
    assert r.status_code == 422


def test_toss_surfaces_grocy_outage_as_502(client):
    async def fake_consume(self, product_id, amount=1.0, spoiled=False):
        raise GrocyError("Grocy is not reachable. Inventory will return when it is.")

    with patch.object(GrocyClient, "consume_stock", fake_consume):
        r = client.post("/expiring/toss/42", json={"amount": 1.0})
    assert r.status_code == 502
    assert "not reachable" in r.json()["detail"]


def test_toss_refreshes_the_count_cache(client):
    # The /expiring/count 30s cache must not keep showing the old number
    # right after the user acted on the list.
    from app.routers import expiring as expiring_router
    expiring_router._count_items_cache.set([{"days_remaining": 0}])

    async def fake_consume(self, product_id, amount=1.0, spoiled=False):
        return {}

    with patch.object(GrocyClient, "consume_stock", fake_consume):
        client.post("/expiring/toss/42", json={"amount": 1.0})
    assert expiring_router._count_items_cache.get() is None


# --- The page -----------------------------------------------------------------

def _page(client, expiring_items, log_rows):
    async def fake_expiring(self, days=7):
        return expiring_items

    async def fake_get(self, path):
        if path.startswith("/objects/stock_log?"):
            return log_rows
        return []

    async def fake_products(self):
        return [{"id": int(pid), "name": name} for pid, name in PRODUCTS.items()]

    with patch.object(GrocyClient, "get_expiring", fake_expiring), \
         patch.object(GrocyClient, "_get", fake_get), \
         patch.object(GrocyClient, "get_products", fake_products), \
         patch.object(type(settings), "is_configured", lambda self: True):
        return client.get("/ui/expiring")


def test_expiring_page_offers_toss_beside_consume(client):
    r = _page(client, [{
        "product_id": 5,
        "product": {"name": "Broccoli"},
        "amount": 2,
        "best_before_date": date.today().isoformat(),
        "days_remaining": 0,
    }], [])
    assert r.status_code == 200
    assert 'tossIt(5, &#34;Broccoli&#34;, 2)' in r.text
    assert 'action="ui/consume/5"' in r.text  # consume still there beside it


def test_expiring_page_renders_the_waste_table(client):
    r = _page(client, [], LOG)
    assert r.status_code == 200
    assert "Waste" in r.text
    assert "Spinach" in r.text and "Milk" in r.text
    assert "100%" in r.text and "33%" in r.text
    assert "Yogurt" not in r.text  # eaten, never tossed


def test_expiring_page_waste_empty_state_is_honest(client):
    r = _page(client, [], [])
    assert r.status_code == 200
    assert "No spoilage recorded yet" in r.text


def test_review_page_ships_the_toss_link_on_sniff_rows(client):
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/pending")
    assert r.status_code == 200
    # Review rows render client-side; the sniff-eligible block must carry the
    # toss link wired to the same /expiring/toss endpoint the list page uses.
    assert "tossStock(" in r.text
    assert "expiring/toss/" in r.text


def test_expiring_page_hides_waste_during_an_outage(client):
    async def broken(self, days=7):
        raise GrocyError("Grocy is not reachable. Inventory will return when it is.")

    with patch.object(GrocyClient, "get_expiring", broken), \
         patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/expiring")
    assert r.status_code == 200
    # No waste card at all: "no spoilage recorded" would be a claim the page
    # cannot back while Grocy is unreachable.
    assert "No spoilage recorded yet" not in r.text
    assert 'id="waste-summary"' not in r.text
