"""Opened tracking on the Inventory page (FoodAssistant-oyef).

Covers the pure opened-amount merge map, the /inventory/open endpoint, the
dashboard rows carrying amount_opened, and the page markup (the one-tap
Opened action and the Opened badge logic living in the page script).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services.grocy import GrocyClient, GrocyError, opened_amounts  # noqa: E402


# --- Pure merge map -----------------------------------------------------------

def test_opened_amounts_maps_only_positive_opened_stock():
    stock = [
        {"product_id": 1, "amount": 3.0, "amount_opened": 1.0},
        {"product_id": 2, "amount": 2.0, "amount_opened": 0},
        {"product_id": 3, "amount": 1.0},                       # field absent
        {"product_id": 4, "amount": 2.0, "amount_opened": "2"},  # string shape
        {"product_id": None, "amount_opened": 5.0},              # no id: dropped
        {"product_id": 5, "amount_opened": "nonsense"},          # unreadable: dropped
    ]
    assert opened_amounts(stock) == {1: 1.0, 4: 2.0}


def test_opened_amounts_handles_empty_input():
    assert opened_amounts([]) == {}
    assert opened_amounts(None) == {}


# --- Endpoints ----------------------------------------------------------------

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


def test_open_endpoint_marks_one_unit_opened(client):
    seen = {}

    async def fake_open(self, product_id, amount=1.0):
        seen["args"] = (product_id, amount)
        return {"transaction_id": "abc"}

    with patch.object(GrocyClient, "open_stock", fake_open):
        r = client.post("/inventory/open/42")
    assert r.status_code == 200
    assert r.json() == {"transaction_id": "abc"}
    assert seen["args"] == (42, 1.0)


def test_open_endpoint_surfaces_grocy_errors(client):
    async def fake_open(self, product_id, amount=1.0):
        raise GrocyError("Grocy 400 on /stock/products/42/open: no stock")

    with patch.object(GrocyClient, "open_stock", fake_open):
        r = client.post("/inventory/open/42")
    assert r.status_code == 500
    assert "no stock" in r.json()["detail"]


def test_dashboard_rows_carry_amount_opened(client):
    async def fake_full_stock(self):
        return [
            {"product_id": 1, "name": "Milk", "amount": 3.0, "days_remaining": 2,
             "storage_bucket": "other"},
            {"product_id": 2, "name": "Eggs", "amount": 6.0, "days_remaining": 5,
             "storage_bucket": "other"},
        ]

    async def fake_stock(self):
        return [{"product_id": 1, "amount": 3.0, "amount_opened": 1.0}]

    with patch.object(GrocyClient, "get_full_stock", fake_full_stock), \
         patch.object(GrocyClient, "get_stock", fake_stock):
        r = client.get("/inventory/dashboard")
    assert r.status_code == 200
    rows = {i["name"]: i for i in r.json()["other"]}
    assert rows["Milk"]["amount_opened"] == 1.0
    assert rows["Eggs"]["amount_opened"] == 0.0


def test_dashboard_survives_a_failed_opened_lookup(client):
    # The merge is a garnish: losing it must not lose the dashboard.
    async def fake_full_stock(self):
        return [{"product_id": 1, "name": "Milk", "amount": 3.0,
                 "days_remaining": 2, "storage_bucket": "other"}]

    async def broken_stock(self):
        raise GrocyError("Grocy is not reachable. Inventory will return when it is.")

    with patch.object(GrocyClient, "get_full_stock", fake_full_stock), \
         patch.object(GrocyClient, "get_stock", broken_stock):
        r = client.get("/inventory/dashboard")
    assert r.status_code == 200
    assert r.json()["other"][0]["amount_opened"] == 0.0


# --- The page -----------------------------------------------------------------

def test_inventory_page_ships_the_opened_action_and_badge(client):
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/inventory")
    assert r.status_code == 200
    # The rows render client-side, so the page script must carry the one-tap
    # Opened action, the badge, and the zero-stock and nothing-open gates.
    assert "markOpened(" in r.text
    assert "badge-opened" in r.text
    assert "item.amount_opened > 0" in r.text
    assert "item.amount > 0" in r.text
