"""Stock lands in the storage the user chose (FoodAssistant-51o2x).

Grocy files a /stock/products/{id}/add without a location_id under the
product's default location, so an existing product added as frozen used to
sit in the fridge while its best-by followed the freezer.
"""
from __future__ import annotations

import asyncio
import json

import httpx

from app.models.food import FoodItem, StorageType
from app.services import grocy as grocy_mod
from app.services.grocy import GrocyClient


def _mock_grocy(monkeypatch, products, locations):
    sent = []

    def handler(request: httpx.Request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        sent.append((request.method, path, body))
        if path.endswith("/objects/locations"):
            if request.method == "GET":
                return httpx.Response(200, json=locations)
            locations.append({"id": 90 + len(locations), "name": body["name"]})
            return httpx.Response(200, json={"created_object_id": locations[-1]["id"]})
        if path.endswith("/objects/product_groups"):
            if request.method == "GET":
                return httpx.Response(200, json=[{"id": 1, "name": "Dairy"}])
            return httpx.Response(200, json={"created_object_id": 2})
        if path.endswith("/objects/quantity_units"):
            if request.method == "GET":
                return httpx.Response(200, json=[{"id": 1, "name": "Piece"}])
            return httpx.Response(200, json={"created_object_id": 2})
        if path.endswith("/objects/products"):
            if request.method == "GET":
                return httpx.Response(200, json=products)
            return httpx.Response(200, json={"created_object_id": 777})
        if "/stock/products/" in path and path.endswith("/add"):
            return httpx.Response(200, json=[{"id": 1}])
        if "product_barcodes" in path:
            if request.method == "GET":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"created_object_id": 5})
        return httpx.Response(404, json={"error_message": "unexpected " + path})

    monkeypatch.setattr(grocy_mod, "_client",
                        httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(grocy_mod.settings, "grocy_base_url", "http://grocy.test",
                        raising=False)
    return sent


def _add_body(sent, product_id):
    return next(b for m, p, b in sent if p.endswith(f"/stock/products/{product_id}/add"))


def test_adding_an_existing_product_as_frozen_lands_it_in_the_freezer(monkeypatch):
    locations = [{"id": 3, "name": "Refrigerator"}, {"id": 4, "name": "Freezer"}]
    products = [{"id": 321, "name": "Milk", "location_id": 3}]
    sent = _mock_grocy(monkeypatch, products, locations)
    item = FoodItem(name="Milk", storage_type=StorageType.frozen)
    asyncio.run(GrocyClient().import_item(item))
    assert _add_body(sent, 321).get("location_id") == 4


def test_a_new_product_gets_its_stock_in_the_chosen_location(monkeypatch):
    locations = [{"id": 3, "name": "Refrigerator"}, {"id": 4, "name": "Freezer"}]
    sent = _mock_grocy(monkeypatch, [], locations)
    item = FoodItem(name="Peas", storage_type=StorageType.frozen)
    asyncio.run(GrocyClient().import_item(item))
    assert _add_body(sent, 777).get("location_id") == 4


def test_add_stock_without_a_location_leaves_grocys_default(monkeypatch):
    sent = _mock_grocy(monkeypatch, [], [])
    asyncio.run(GrocyClient().add_stock(5, FoodItem(name="Rice")))
    assert "location_id" not in _add_body(sent, 5)


def _location_writes(sent):
    return [(m, b) for m, p, b in sent if p.endswith("/objects/locations") and m != "GET"]


def test_a_fresh_grocys_fridge_is_used_instead_of_a_second_one(monkeypatch):
    # A fresh Grocy 4.7.1 has one location, "Fridge" (id 2).
    locations = [{"id": 2, "name": "Fridge", "is_freezer": 0, "active": 1}]
    sent = _mock_grocy(monkeypatch, [], locations)
    item = FoodItem(name="Milk", storage_type=StorageType.refrigerated)
    asyncio.run(GrocyClient().import_item(item))
    assert _add_body(sent, 777).get("location_id") == 2
    assert _location_writes(sent) == []


def test_an_existing_refrigerator_still_wins_over_the_fridge(monkeypatch):
    locations = [{"id": 2, "name": "Fridge"}, {"id": 3, "name": "Refrigerator"}]
    sent = _mock_grocy(monkeypatch, [], locations)
    item = FoodItem(name="Milk", storage_type=StorageType.refrigerated)
    asyncio.run(GrocyClient().import_item(item))
    assert _add_body(sent, 777).get("location_id") == 3
    assert _location_writes(sent) == []


def test_an_inactive_fridge_is_not_reused(monkeypatch):
    locations = [{"id": 2, "name": "Fridge", "active": 0}]
    sent = _mock_grocy(monkeypatch, [], locations)
    item = FoodItem(name="Milk", storage_type=StorageType.refrigerated)
    asyncio.run(GrocyClient().import_item(item))
    assert _location_writes(sent) == [("POST", {"name": "Refrigerator"})]


def test_a_fridge_marked_as_a_freezer_is_not_reused(monkeypatch):
    locations = [{"id": 2, "name": "Fridge", "is_freezer": 1, "active": 1}]
    sent = _mock_grocy(monkeypatch, [], locations)
    item = FoodItem(name="Milk", storage_type=StorageType.refrigerated)
    asyncio.run(GrocyClient().import_item(item))
    assert _location_writes(sent) == [("POST", {"name": "Refrigerator"})]


def test_a_fridge_a_custom_category_owns_is_not_reused(monkeypatch):
    # The user files drinks in Grocy's Fridge through a category of their own,
    # so everyday fridge food still gets a place of its own.
    monkeypatch.setattr(grocy_mod.settings, "custom_storage_categories",
                        [{"label": "Drinks", "location": "Fridge"}], raising=False)
    locations = [{"id": 2, "name": "Fridge", "is_freezer": 0, "active": 1}]
    sent = _mock_grocy(monkeypatch, [], locations)
    item = FoodItem(name="Milk", storage_type=StorageType.refrigerated)
    asyncio.run(GrocyClient().import_item(item))
    assert _location_writes(sent) == [("POST", {"name": "Refrigerator"})]


def test_a_freezer_created_here_is_a_freezer_in_grocy(monkeypatch):
    sent = _mock_grocy(monkeypatch, [], [{"id": 2, "name": "Fridge"}])
    item = FoodItem(name="Peas", storage_type=StorageType.frozen)
    asyncio.run(GrocyClient().import_item(item))
    assert _location_writes(sent) == [("POST", {"name": "Freezer", "is_freezer": 1})]


def test_an_existing_freezer_is_left_as_it_is(monkeypatch):
    # An install whose Freezer predates the flag keeps it unchanged.
    locations = [{"id": 4, "name": "Freezer", "is_freezer": 0}]
    sent = _mock_grocy(monkeypatch, [], locations)
    item = FoodItem(name="Peas", storage_type=StorageType.frozen)
    asyncio.run(GrocyClient().import_item(item))
    assert _add_body(sent, 777).get("location_id") == 4
    assert _location_writes(sent) == []


def test_other_places_are_created_without_the_freezer_flag(monkeypatch):
    sent = _mock_grocy(monkeypatch, [], [])
    for storage in (StorageType.refrigerated, StorageType.room_temp, StorageType.dry):
        asyncio.run(GrocyClient().import_item(FoodItem(name="Rice", storage_type=storage)))
    assert [b for _, b in _location_writes(sent)] == [
        {"name": "Refrigerator"}, {"name": "Counter / Room Temp"},
        {"name": "Pantry / Dry Storage"}]
