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
from app.services.grocy import GrocyClient, GrocyError, sniff_test_new_date  # noqa: E402

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
    assert dates["/objects/stock/11"] == (date.today() + timedelta(days=5)).isoformat()
    assert dates["/objects/stock/12"] == (date.today() + timedelta(days=3)).isoformat()
    assert "/objects/stock/13" not in dates
    # The earliest resulting date drives the expiring row.
    assert result["new_best_by"] == (date.today() + timedelta(days=3)).isoformat()


def test_extend_best_by_with_no_dated_entries_reports_none(monkeypatch):
    c, calls = _client_with_entries(monkeypatch, [{"id": 9, "amount": 1}])
    result = asyncio.run(c.extend_best_by(7, 5))
    assert result == {"product_id": 7, "updated": 0, "new_best_by": None}
    assert calls == []


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

    async def fake_extend(self, product_id, days):
        seen["args"] = (product_id, days)
        return {"product_id": product_id, "updated": 1, "new_best_by": "2026-08-01"}

    with patch.object(GrocyClient, "extend_best_by", fake_extend):
        r = client.post("/expiring/extend/42", json={"days": delta})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "product_id": 42, "updated": 1,
                        "new_best_by": "2026-08-01"}
    assert seen["args"] == (42, delta)


def test_extend_endpoint_rejects_out_of_range_days(client):
    r = client.post("/expiring/extend/42", json={"days": 0})
    assert r.status_code == 422
    r = client.post("/expiring/extend/42", json={"days": 99})
    assert r.status_code == 422


def test_extend_endpoint_surfaces_grocy_outage_as_502(client):
    async def fake_extend(self, product_id, days):
        raise GrocyError("Grocy is not reachable. Inventory will return when it is.")

    with patch.object(GrocyClient, "extend_best_by", fake_extend):
        r = client.post("/expiring/extend/42", json={"days": 3})
    assert r.status_code == 502
    assert "not reachable" in r.json()["detail"]


def test_extend_refreshes_the_count_cache(client):
    # The /expiring/count 30s cache must not keep showing the old number
    # right after the user acted on the list.
    from app.routers import expiring as expiring_router
    expiring_router._count_items_cache.set([{"days_remaining": 0}])

    async def fake_extend(self, product_id, days):
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

    with patch.object(GrocyClient, "get_expiring", fake_expiring), \
         patch.object(type(settings), "is_configured", lambda self: True):
        r = client.get("/ui/expiring")
    assert r.status_code == 200
    assert "Sniff test" in r.text
    for delta in (1, 3, 5):
        assert f'sniffTest(5, "Broccoli", {delta})' in r.text
