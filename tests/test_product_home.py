"""Stock bought again goes where the product lives.

Every import used to put an item wherever the AI guessed, even for a product
Grocy already had, so olive oil moved to the counter came back to the pantry
with the next bottle. A product now lives where it was first stocked until the
user moves it (a move already makes the new place the product's default in
Grocy), and stock bought later goes there: the review screen shows that place,
and a place of the user's own (a Lazy Susan) is used on import. Only a place
as cold as the item needs counts, so a chicken once moved to the fridge to
thaw never sends the next frozen chicken there.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.models.food import FoodItem, StorageType
from app.services import grocy as grocy_mod
from app.services.grocy import GrocyClient, location_coldness

PIECE = {"id": 2, "name": "Piece", "name_plural": "Pieces"}

FRIDGE = {"id": 1, "name": "Refrigerator"}
FREEZER = {"id": 2, "name": "Freezer", "is_freezer": 1}
COUNTER = {"id": 3, "name": "Counter / Room Temp"}
PANTRY = {"id": 4, "name": "Pantry / Dry Storage"}
LAZY_SUSAN = {"id": 5, "name": "Lazy Susan"}
GARAGE_FREEZER = {"id": 6, "name": "Garage chest", "is_freezer": 1}
CLOSED = {"id": 7, "name": "Old cupboard", "active": 0}
LOCATIONS = [FRIDGE, FREEZER, COUNTER, PANTRY, LAZY_SUSAN, GARAGE_FREEZER, CLOSED]


def _fake_grocy(monkeypatch, products):
    """A fake Grocy holding ``products`` and LOCATIONS. Returns the location
    each stock add went to, in order."""
    added = []

    def handler(request: httpx.Request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        if path.endswith("/objects/locations"):
            return httpx.Response(200, json=LOCATIONS)
        if path.endswith("/objects/product_groups"):
            if request.method == "GET":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"created_object_id": 9})
        if path.endswith("/objects/quantity_units"):
            return httpx.Response(200, json=[PIECE])
        if path.endswith("/objects/quantity_unit_conversions"):
            return httpx.Response(200, json=[])
        if path.endswith("/objects/products"):
            if request.method == "GET":
                return httpx.Response(200, json=products)
            products.append({"id": 777, **body})
            return httpx.Response(200, json={"created_object_id": 777})
        if "/objects/products/" in path:
            wanted = path.rsplit("/", 1)[1]
            return httpx.Response(200, json=next(
                (p for p in products if str(p["id"]) == wanted), {}))
        if "/stock/products/" in path and path.endswith("/add"):
            added.append(body.get("location_id"))
            return httpx.Response(200, json=[{"id": 1}])
        return httpx.Response(404, json={"error_message": "unexpected " + path})

    monkeypatch.setattr(grocy_mod, "_client",
                        httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(grocy_mod.settings, "grocy_base_url", "http://grocy.test",
                        raising=False)
    return added


def _product(name, location):
    return {"id": 40, "name": name, "location_id": location["id"],
            "qu_id_stock": 2, "qu_id_purchase": 2}


@pytest.mark.parametrize("location, coldness", [
    (FRIDGE, 2), (FREEZER, 3), (COUNTER, 1), (PANTRY, 1), (LAZY_SUSAN, 1),
    (GARAGE_FREEZER, 3), ({"id": 8, "name": "Garage fridge"}, 2),
    ({"id": 9, "name": "Basement chest freezer"}, 3),
])
def test_how_cold_a_place_keeps_food(location, coldness):
    assert location_coldness(location) == coldness


def _remembered(monkeypatch, product, storage):
    _fake_grocy(monkeypatch, [product] if product else [])
    item = FoodItem(name="Yellow onions", storage_type=storage)
    asyncio.run(GrocyClient().remember_places([item]))
    return item.storage_type


def test_review_shows_where_the_product_lives(monkeypatch):
    # The AI says pantry; this kitchen keeps onions on the counter.
    got = _remembered(monkeypatch, _product("Yellow onions", COUNTER), StorageType.dry)
    assert got == StorageType.room_temp


def test_the_name_is_matched_whatever_its_case(monkeypatch):
    got = _remembered(monkeypatch, _product("yellow ONIONS", COUNTER), StorageType.dry)
    assert got == StorageType.room_temp


def test_a_new_product_keeps_the_guess(monkeypatch):
    assert _remembered(monkeypatch, None, StorageType.dry) == StorageType.dry


@pytest.mark.parametrize("home, guess", [
    # Thawed once in the fridge: the next frozen one still goes in the freezer.
    (FRIDGE, StorageType.frozen),
    # Moved to the fridge once opened: a new sealed jar goes to the pantry.
    (FRIDGE, StorageType.dry),
    (FREEZER, StorageType.refrigerated),
    (PANTRY, StorageType.refrigerated),
    # A place Grocy no longer uses is no home.
    (CLOSED, StorageType.dry),
])
def test_a_home_of_another_temperature_is_never_used(monkeypatch, home, guess):
    assert _remembered(monkeypatch, _product("Yellow onions", home), guess) == guess


def test_a_place_of_the_users_own_is_left_for_the_import(monkeypatch):
    got = _remembered(monkeypatch, _product("Yellow onions", LAZY_SUSAN), StorageType.dry)
    assert got == StorageType.dry


def _imported(monkeypatch, product, storage):
    added = _fake_grocy(monkeypatch, [product] if product else [])
    asyncio.run(GrocyClient().import_item(
        FoodItem(name="Smoked paprika", storage_type=storage)))
    return added


def test_stock_goes_back_to_a_place_of_the_users_own(monkeypatch):
    assert _imported(monkeypatch, _product("Smoked paprika", LAZY_SUSAN),
                     StorageType.dry) == [LAZY_SUSAN["id"]]


def test_a_freezer_of_the_users_own_takes_frozen_food(monkeypatch):
    assert _imported(monkeypatch, _product("Smoked paprika", GARAGE_FREEZER),
                     StorageType.frozen) == [GARAGE_FREEZER["id"]]


def test_a_place_of_the_users_own_at_another_temperature_is_skipped(monkeypatch):
    assert _imported(monkeypatch, _product("Smoked paprika", GARAGE_FREEZER),
                     StorageType.dry) == [PANTRY["id"]]


def test_a_built_in_place_follows_the_review_screen(monkeypatch):
    # Moved to the pantry on review, the import keeps that choice even though
    # the product lives on the counter.
    assert _imported(monkeypatch, _product("Smoked paprika", COUNTER),
                     StorageType.dry) == [PANTRY["id"]]


def test_a_new_product_goes_where_the_ai_guessed(monkeypatch):
    assert _imported(monkeypatch, None, StorageType.frozen) == [FREEZER["id"]]


def test_the_scan_survives_an_unreachable_grocy(monkeypatch):
    from app.routers import analyze

    class Down:
        async def remember_places(self, items):
            raise grocy_mod.GrocyError("Grocy is down")

    monkeypatch.setattr(analyze, "GrocyClient", Down)
    item = FoodItem(name="Yellow onions", storage_type=StorageType.dry)
    asyncio.run(analyze._remember_places([item]))
    assert item.storage_type == StorageType.dry
