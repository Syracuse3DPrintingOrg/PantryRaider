"""Stock rows carry their unit on Grocy 4.7 (FoodAssistant-bt5q6).

Grocy 4.7's /stock rows name the stock unit only by the product's
qu_id_stock, so the unit came back empty, the inventory showed no units and
unit-aware recipe coverage never ran.
"""
from __future__ import annotations

import asyncio

import httpx

from app.services import grocy as grocy_mod
from app.services.grocy import GrocyClient, _stock_unit_name


def _install(monkeypatch, stock, units_status=200):
    def handler(request):
        p = request.url.path
        if p.endswith("/stock"):
            return httpx.Response(200, json=stock)
        if p.endswith("/objects/locations"):
            return httpx.Response(200, json=[{"id": 3, "name": "Pantry"}])
        if p.endswith("/objects/product_groups"):
            return httpx.Response(200, json=[{"id": 1, "name": "Condiments"}])
        if p.endswith("/objects/stock"):
            return httpx.Response(200, json=[])
        if p.endswith("/objects/quantity_units"):
            if units_status != 200:
                return httpx.Response(units_status, json={"error_message": "nope"})
            return httpx.Response(200, json=[{"id": 10, "name": "Gram"}])
        return httpx.Response(404, json={})

    monkeypatch.setattr(grocy_mod, "_client",
                        httpx.AsyncClient(transport=httpx.MockTransport(handler)))


_STOCK = [{"product_id": 7, "amount": 6, "best_before_date": "2030-01-01",
           "product": {"id": 7, "name": "Tahini", "location_id": 3,
                       "product_group_id": 1, "qu_id_stock": 10}}]


def test_full_stock_reports_the_stock_unit(monkeypatch):
    _install(monkeypatch, _STOCK)
    rows = asyncio.run(GrocyClient().get_full_stock())
    assert rows[0]["unit"] == "Gram"


def test_a_failed_units_read_leaves_the_unit_blank(monkeypatch):
    _install(monkeypatch, _STOCK, units_status=500)
    rows = asyncio.run(GrocyClient().get_full_stock())
    assert rows[0]["name"] == "Tahini"
    assert rows[0]["unit"] is None


def test_unit_name_prefers_a_nested_unit_and_handles_unknown_ids():
    assert _stock_unit_name({}, {"qu_unit_stock": {"name": "Jar"}}, {}) == "Jar"
    assert _stock_unit_name({}, {"qu_id_stock": 99}, {"10": "Gram"}) is None
    assert _stock_unit_name({}, {}, {"10": "Gram"}) is None
