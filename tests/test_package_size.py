"""What one package holds: the 64 fl oz in a bottle of milk.

An item said how many there were ("1 Piece" of milk) but never how big each
one was, so the Inventory could not tell a 64 oz jug from a pint. Items now
carry a package size, read from the AI reply's own field, from the size Open
Food Facts reports for a barcode, or from a size written into the name ("Whole
milk 64 oz"). The review screen shows and edits it, Grocy keeps it as the
product's own unit conversion (1 Piece = 64 Ounces), and the Inventory shows
"1 x 64 oz".

Covers the pure parser, the AI reply parser, the barcode lookup, the import
into Grocy (against a fake Grocy), the Inventory rows and the pending
endpoints.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.models.food import FoodItem
from app.providers.base import _parse_item, _parse_receipt
from app.services import barcode
from app.services import grocy as grocy_mod
from app.services.grocy import GrocyClient, package_size_of, size_label
from app.services.package_size import (apply_package_size, normalize_size,
                                       parse_size, split_name)

_SERVICE_DIR = Path(__file__).resolve().parents[1] / "service"

PIECE = {"id": 2, "name": "Piece", "name_plural": "Pieces"}
OUNCE = {"id": 6, "name": "Ounce", "name_plural": "Ounces"}
GRAM = {"id": 7, "name": "Gram", "name_plural": "Grams"}
FLOZ = {"id": 8, "name": "Fluid ounce", "name_plural": "Fluid ounces"}


# --- The pure parser ------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("64 oz", (64.0, "Ounce")),
    ("64oz", (64.0, "Ounce")),
    ("40 OZ", (40.0, "Ounce")),
    ("10oz.", (10.0, "Ounce")),
    ("12 fl oz", (12.0, "Fluid ounce")),
    ("16.9 fl. oz (500 ml)", (16.9, "Fluid ounce")),
    ("1.89 l", (1.89, "Liter")),
    ("1,5 L", (1.5, "Liter")),
    ("500g", (500.0, "Gram")),
    ("2 lb", (2.0, "Pound")),
    ("1 1/2 lb", (1.5, "Pound")),
    ("1/2 gal", (0.5, "Gallon")),
    ("1 gallon", (1.0, "Gallon")),
    # A multipack scanned once is one item: the size is the whole pack.
    ("6 x 330 ml", (1980.0, "Milliliter")),
    # US labels give anything past a pound in pounds and ounces.
    ("1 lb 5 oz", (21.0, "Ounce")),
    ("2 lbs. 4.5 oz", (36.5, "Ounce")),
    # Said in words, as a size is typed in the review screen's box.
    ("half gallon", (0.5, "Gallon")),
    ("Half-Gallon", (0.5, "Gallon")),
    ("half a gallon", (0.5, "Gallon")),
    ("a quart", (1.0, "Quart")),
    ("a quarter pound", (0.25, "Pound")),
    ("two pounds", (2.0, "Pound")),
    # A unit on its own is one of it.
    ("gallon", (1.0, "Gallon")),
    ("oz", (1.0, "Ounce")),
])
def test_parse_size_reads_sizes_as_printed(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", None, 64, "3 pcs", "2 bags", "7 grain", "0 g",
    # Words that only look like sizes.
    "Grade A Large eggs", "half and half", "a bag",
    # Past anything on a pantry shelf.
    "5000 kg",
    # One letter that is a cup, a teaspoon or a tablespoon in a recipe is never
    # read as a package size.
    "2 c", "1 t", "1 T",
])
def test_parse_size_finds_no_size(text):
    assert parse_size(text) is None


@pytest.mark.parametrize("name, expected", [
    ("Whole milk, 64 oz", ("Whole milk", (64.0, "Ounce"))),
    ("Rao's marinara sauce (24 oz)", ("Rao's marinara sauce", (24.0, "Ounce"))),
    ("64 oz whole milk", ("whole milk", (64.0, "Ounce"))),
    ("Hoisin sauce 8.5 fl oz", ("Hoisin sauce", (8.5, "Fluid ounce"))),
    ("Kikkoman ponzu - 10 oz", ("Kikkoman ponzu", (10.0, "Ounce"))),
    ("7 grain bread 24 oz", ("7 grain bread", (24.0, "Ounce"))),
    ("2% milk 1 gal", ("2% milk", (1.0, "Gallon"))),
    ("Whole milk half gallon", ("Whole milk", (0.5, "Gallon"))),
    # Half-and-half is a cream, not half of something.
    ("Half and half quart", ("Half and half quart", None)),
    ("Half-and-half half gallon", ("Half-and-half", (0.5, "Gallon"))),
    ("Lundberg brown rice syrup 1 lb 5 oz",
     ("Lundberg brown rice syrup", (21.0, "Ounce"))),
    ("Organic whole milk", ("Organic whole milk", None)),
    # Nothing would be left of the name.
    ("12 oz", ("12 oz", None)),
])
def test_split_name_takes_the_size_out_of_the_name(name, expected):
    assert split_name(name) == expected


@pytest.mark.parametrize("amount, unit, expected", [
    (64, "Fluid ounce", "64 fl oz"),
    (1.5, "Liter", "1.5 L"),
    (500, "g", "500 g"),
    (1, "Cup", "1 Cup"),
    (2, "Cup", "2 Cups"),
    (None, "Ounce", ""),
    (64, None, ""),
    (0, "Ounce", ""),
])
def test_size_label(amount, unit, expected):
    assert size_label(amount, unit) == expected


def test_normalize_size_takes_a_number_and_unit_or_text():
    assert normalize_size(64, "oz") == (64.0, "Ounce")
    assert normalize_size("64", "fl oz") == (64.0, "Fluid ounce")
    assert normalize_size("1.5 L", None) == (1.5, "Liter")
    assert normalize_size(2, "bags") is None
    assert normalize_size(True, "oz") is None
    assert normalize_size(64, None) is None


def test_apply_package_size_from_the_name():
    item = apply_package_size(FoodItem(name="Whole milk 64 oz", quantity=2))
    assert (item.name, item.quantity, item.unit) == ("Whole milk", 2.0, "item")
    assert (item.package_size, item.package_unit) == (64.0, "Fluid ounce")


def test_apply_package_size_from_the_notes():
    item = apply_package_size(FoodItem(name="Peanut butter",
                                       notes="Size: 40 oz (1 opened)"))
    assert (item.package_size, item.package_unit) == (40.0, "Ounce")
    assert item.notes == "Size: 40 oz (1 opened)"


def test_a_size_given_as_the_amount_is_one_package():
    item = apply_package_size(FoodItem(name="Ponzu", quantity=10, unit="oz",
                                       package_size=10, package_unit="oz"))
    assert (item.quantity, item.unit) == (1.0, "item")
    assert (item.package_size, item.package_unit) == (10.0, "Ounce")


def test_ounces_of_a_fluid_ounce_bottle_are_one_bottle():
    item = apply_package_size(FoodItem(name="Ponzu", quantity=10, unit="oz",
                                       package_size="10 fl oz"))
    assert (item.quantity, item.unit) == (1.0, "item")
    assert (item.package_size, item.package_unit) == (10.0, "Fluid ounce")


def test_food_sold_by_weight_keeps_its_amount():
    item = apply_package_size(FoodItem(name="Apples", quantity=1.5, unit="lbs"))
    assert (item.quantity, item.unit, item.package_size) == (1.5, "lbs", None)


# What gemma4:e4b answered, every time, for a typed pantry list: the size in
# its own field and its unit copied into the unit field too.
@pytest.mark.parametrize("quantity, unit, size, expected", [
    (1, "oz", "3.25 oz", (1.0, "item")),
    (1, "g", "250 g", (1.0, "item")),
    (1, "lb", "0.44 lb", (1.0, "item")),
    (1, "lb", "1 lb 5 oz", (1.0, "item")),
    (2, "oz", "8 oz", (2.0, "item")),
    # An amount that is a whole number of sizes is that many packages.
    (48, "oz", "24 oz", (2.0, "item")),
    (2, "lb", "16 oz", (2.0, "item")),
    # Already a count: left alone.
    (2, "item", "24 oz", (2.0, "item")),
    (3, "jar", "24 oz", (3.0, "jar")),
    # A weighed amount that is no whole number of packages stays weighed.
    (1.5, "lb", "1 lb", (1.5, "lb")),
])
def test_a_measured_amount_with_a_size_counts_packages(quantity, unit, size, expected):
    item = apply_package_size(FoodItem(name="Sugar", quantity=quantity,
                                       unit=unit, package_size=size))
    assert (item.quantity, item.unit) == expected
    assert item.package_size is not None


def test_the_receipt_reply_that_lost_the_sizes():
    receipt = _parse_receipt({"items": [
        {"name": "Wilton pink sanding sugar", "quantity": 1, "unit": "oz",
         "package_size": "3.25 oz"},
        {"name": "Bird's original custard powder", "quantity": 1, "unit": "g",
         "package_size": "250 g"},
        {"name": "Marcona almonds", "quantity": 1, "unit": "lb",
         "package_size": "0.44 lb"},
        {"name": "Meyer lemon blossoms", "quantity": 2, "unit": None,
         "package_size": None},
    ]}, 0.85, "{}")
    got = [(i.quantity, i.unit, size_label(i.package_size, i.package_unit))
           for i in receipt.items]
    assert got == [(1.0, "item", "3.25 oz"), (1.0, "item", "250 g"),
                   (1.0, "item", "0.44 lb"), (2.0, "item", "")]


def test_a_different_size_in_the_name_stays_in_the_name():
    item = apply_package_size(FoodItem(name="Milk 64 oz", package_size=32,
                                       package_unit="fl oz"))
    assert item.name == "Milk 64 oz"
    assert (item.package_size, item.package_unit) == (32.0, "Fluid ounce")


def test_an_unreadable_size_is_dropped():
    item = apply_package_size(FoodItem(name="Jam", package_size=3,
                                       package_unit="jars"))
    assert (item.package_size, item.package_unit) == (None, None)


def test_a_typed_size_is_read_into_the_item():
    assert FoodItem(name="Milk", package_size="64 oz").package_unit == "Ounce"
    assert FoodItem(name="Milk", package_size="lots").package_size is None


# --- The AI reply ---------------------------------------------------------------

def test_the_reply_parser_reads_the_package_size():
    item = _parse_item({"name": "Whole milk", "quantity": 1, "unit": "item",
                        "package_size": "64 fl oz"}, 0.9)
    assert (item.package_size, item.package_unit) == (64.0, "Fluid ounce")


def test_the_reply_parser_finds_a_size_left_in_the_name():
    receipt = _parse_receipt({"items": [
        {"name": "Rao's marinara 24 oz", "quantity": 2, "unit": "item",
         "package_size": None},
        {"name": "Bananas", "quantity": 2.1, "unit": "lbs"},
    ]}, 0.85, "{}")
    rao, bananas = receipt.items
    assert (rao.name, rao.quantity, rao.package_size) == ("Rao's marinara", 2.0, 24.0)
    assert bananas.package_size is None


@pytest.mark.parametrize("bad", [{"x": 1}, [1, 2], "a jar", -64, float("nan")])
def test_an_odd_package_size_costs_only_the_size(bad):
    item = _parse_item({"name": "Jam", "quantity": 1, "package_size": bad}, 0.9)
    assert item.name == "Jam"
    assert item.package_size is None


# --- The barcode lookup ---------------------------------------------------------

def _off_reply(quantity):
    return {"code": "1", "errors": [], "warnings": [], "status": "success",
            "result": {"id": "product_found", "lc_name": "Product found",
                       "name": "Product found"},
            "product": {"product_name": "Whole Milk", "brands": "Wegmans",
                        "categories_tags": ["en:milks"], "quantity": quantity}}


@pytest.fixture
def no_llm(monkeypatch):
    monkeypatch.setattr(barcode.settings, "barcode_enrichment", "none")
    monkeypatch.setattr(barcode.settings, "barcode_llm_fallback", False)
    monkeypatch.setattr(barcode, "apply_defaults", lambda item, db, **kw: item)


def _patch_off(monkeypatch, body):
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, json=body))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(barcode.httpx, "AsyncClient", factory)


@pytest.mark.anyio
async def test_a_scan_keeps_the_size_open_food_facts_reports(monkeypatch, no_llm):
    _patch_off(monkeypatch, _off_reply("1.89 l"))
    item = await barcode.lookup_barcode("0041190000016", db=None)
    assert item.name.endswith("Whole Milk")
    assert (item.package_size, item.package_unit) == (1.89, "Liter")


@pytest.mark.anyio
async def test_a_scan_with_no_size_has_none(monkeypatch, no_llm):
    _patch_off(monkeypatch, _off_reply(None))
    item = await barcode.lookup_barcode("0041190000016", db=None)
    assert item.package_size is None


# --- Grocy ----------------------------------------------------------------------

def _fake_grocy(monkeypatch, products, units, conversions=None, inverse=True):
    """A fake Grocy that keeps the unit conversions written to it. With
    ``inverse`` it adds each one's reverse, as Grocy 4 does in a trigger."""
    sent = []
    conversions = conversions if conversions is not None else []
    units = list(units)

    def handler(request: httpx.Request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        sent.append((request.method, path, body))
        if path.endswith("/objects/locations") or path.endswith("/objects/product_groups"):
            if request.method == "GET":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"created_object_id": 3})
        if path.endswith("/objects/quantity_units"):
            if request.method == "GET":
                return httpx.Response(200, json=units)
            new = {"id": 50 + len(units), **body}
            units.append(new)
            return httpx.Response(200, json={"created_object_id": new["id"]})
        if path.endswith("/objects/products"):
            if request.method == "GET":
                return httpx.Response(200, json=products)
            products.append({"id": 777, **body})
            return httpx.Response(200, json={"created_object_id": 777})
        if "/objects/products/" in path:
            wanted = path.rsplit("/", 1)[1]
            return httpx.Response(200, json=next(
                (p for p in products if str(p["id"]) == wanted), {}))
        if path.endswith("/objects/quantity_unit_conversions"):
            if request.method == "POST":
                conversions.append(dict(body))
                if inverse:
                    conversions.append({
                        "from_qu_id": body["to_qu_id"], "to_qu_id": body["from_qu_id"],
                        "factor": 1 / body["factor"], "product_id": body["product_id"]})
                return httpx.Response(200, json={"created_object_id": 90})
            wanted = request.url.params.get("query[]", "")
            rows = [c for c in conversions
                    if not wanted or wanted == f"product_id={c.get('product_id')}"]
            return httpx.Response(200, json=rows)
        if "/stock/products/" in path and path.endswith("/add"):
            return httpx.Response(200, json=[{"id": 1}])
        return httpx.Response(404, json={"error_message": "unexpected " + path})

    monkeypatch.setattr(grocy_mod, "_client",
                        httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(grocy_mod.settings, "grocy_base_url", "http://grocy.test",
                        raising=False)
    return sent, conversions


def _conversion_posts(sent):
    return [b for m, p, b in sent
            if m == "POST" and p.endswith("/objects/quantity_unit_conversions")]


def test_a_new_product_keeps_its_size_as_a_conversion(monkeypatch):
    sent, conversions = _fake_grocy(monkeypatch, [], [PIECE, OUNCE, FLOZ])
    result = asyncio.run(GrocyClient().import_item(
        FoodItem(name="Whole milk 64 oz", quantity=2)))
    assert result["name"] == "Whole milk"
    assert "unit_note" not in result
    # Milk is poured, so its "oz" is fluid ounces.
    assert _conversion_posts(sent) == [
        {"from_qu_id": 2, "to_qu_id": 8, "factor": 64.0, "product_id": 777}]
    # Both directions exist, so the recipe matcher (which never inverts a row)
    # can turn fluid ounces into Pieces of this product.
    assert {(c["from_qu_id"], c["to_qu_id"]) for c in conversions} == {(2, 8), (8, 2)}


def test_an_older_grocy_gets_the_reverse_conversion_too(monkeypatch):
    sent, conversions = _fake_grocy(monkeypatch, [], [PIECE, GRAM], inverse=False)
    asyncio.run(GrocyClient().import_item(
        FoodItem(name="Nutella", package_size=400, package_unit="g")))
    assert _conversion_posts(sent) == [
        {"from_qu_id": 2, "to_qu_id": 7, "factor": 400.0, "product_id": 777},
        {"from_qu_id": 7, "to_qu_id": 2, "factor": 1 / 400, "product_id": 777}]


def test_the_size_unit_is_made_when_grocy_has_none(monkeypatch):
    sent, _ = _fake_grocy(monkeypatch, [], [PIECE])
    asyncio.run(GrocyClient().import_item(
        FoodItem(name="Hoisin sauce", package_size=8.5, package_unit="fl oz")))
    made = [b for m, p, b in sent if m == "POST" and p.endswith("/objects/quantity_units")]
    assert made == [{"name": "Fluid ounce", "name_plural": "Fluid ounces"}]
    assert _conversion_posts(sent)[0]["factor"] == 8.5


def test_the_same_size_again_writes_nothing(monkeypatch):
    milk = {"id": 1, "name": "Whole milk", "qu_id_stock": 2}
    held = [{"from_qu_id": 2, "to_qu_id": 6, "factor": 64, "product_id": 1}]
    sent, _ = _fake_grocy(monkeypatch, [milk], [PIECE, OUNCE], held)
    result = asyncio.run(GrocyClient().import_item(
        FoodItem(name="Whole milk", package_size=64, package_unit="oz")))
    assert _conversion_posts(sent) == []
    assert "unit_note" not in result


def test_the_same_size_in_another_unit_writes_nothing(monkeypatch):
    nutella = {"id": 1, "name": "Nutella", "qu_id_stock": 2}
    held = [{"from_qu_id": 2, "to_qu_id": 7, "factor": 400, "product_id": 1}]
    sent, _ = _fake_grocy(monkeypatch, [nutella], [PIECE, GRAM, OUNCE], held)
    result = asyncio.run(GrocyClient().import_item(
        FoodItem(name="Nutella", package_size=14.11, package_unit="oz")))
    assert _conversion_posts(sent) == []
    assert "unit_note" not in result


def test_another_size_goes_in_as_its_own_product(monkeypatch):
    milk = {"id": 1, "name": "Whole milk", "qu_id_stock": 2}
    held = [{"from_qu_id": 2, "to_qu_id": 6, "factor": 64, "product_id": 1}]
    sent, conversions = _fake_grocy(monkeypatch, [milk], [PIECE, OUNCE, FLOZ], held)
    result = asyncio.run(GrocyClient().import_item(
        FoodItem(name="Whole milk, 32 oz")))
    assert result["name"] == "Whole milk 32 fl oz"
    assert result["product_id"] == 777
    assert "keeps Whole milk as 64 oz each" in result["unit_note"]
    assert _conversion_posts(sent) == [
        {"from_qu_id": 2, "to_qu_id": 8, "factor": 32.0, "product_id": 777}]


def test_that_size_finds_its_own_product_the_next_time(monkeypatch):
    products = [{"id": 1, "name": "Whole milk", "qu_id_stock": 2},
                {"id": 5, "name": "Whole milk 32 oz", "qu_id_stock": 2}]
    held = [{"from_qu_id": 2, "to_qu_id": 6, "factor": 64, "product_id": 1},
            {"from_qu_id": 2, "to_qu_id": 6, "factor": 32, "product_id": 5}]
    # Made when milk was still read in ounces: found under that name.
    sent, _ = _fake_grocy(monkeypatch, products, [PIECE, OUNCE, FLOZ], held)
    result = asyncio.run(GrocyClient().import_item(
        FoodItem(name="Whole milk 32 oz")))
    assert result["product_id"] == 5
    assert result["name"] == "Whole milk 32 oz"
    assert "unit_note" not in result
    assert _conversion_posts(sent) == []
    assert any(p.endswith("/stock/products/5/add") for _, p, _ in sent)


def test_a_scan_onto_a_product_of_another_size_is_noted(monkeypatch):
    milk = {"id": 1, "name": "Whole milk", "qu_id_stock": 2}
    held = [{"from_qu_id": 2, "to_qu_id": 6, "factor": 64, "product_id": 1}]
    sent, _ = _fake_grocy(monkeypatch, [milk], [PIECE, OUNCE], held)
    result = asyncio.run(GrocyClient().add_to_product(
        1, FoodItem(name="Whole milk", package_size=32, package_unit="oz")))
    assert _conversion_posts(sent) == []
    assert "64 oz each" in result["unit_note"]


def test_a_product_stocked_by_weight_gets_no_conversion(monkeypatch):
    beef = {"id": 1, "name": "Ground beef", "qu_id_stock": 6}
    sent, _ = _fake_grocy(monkeypatch, [beef], [PIECE, OUNCE])
    asyncio.run(GrocyClient().import_item(
        FoodItem(name="Ground beef", quantity=16, unit="oz",
                 package_size=16, package_unit="oz")))
    assert _conversion_posts(sent) == []


def test_a_failed_size_never_costs_the_stock(monkeypatch):
    sent, _ = _fake_grocy(monkeypatch, [], [PIECE, OUNCE])

    async def broken(self, product_id, item):
        raise grocy_mod.GrocyError("boom")

    monkeypatch.setattr(GrocyClient, "ensure_package_size", broken)
    result = asyncio.run(GrocyClient().import_item(
        FoodItem(name="Whole milk 64 oz")))
    assert result["product_id"] == 777
    assert any(p.endswith("/stock/products/777/add") for _, p, _ in sent)


def test_package_size_of_reads_the_products_own_conversion():
    units = {"2": "Piece", "6": "Ounce", "9": "Pack"}
    rows = [{"from_qu_id": 2, "to_qu_id": 6, "factor": "64", "product_id": 1},
            {"from_qu_id": 6, "to_qu_id": 2, "factor": 1 / 64, "product_id": 1},
            {"from_qu_id": 2, "to_qu_id": 6, "factor": 10, "product_id": None}]
    assert package_size_of(rows, 1, 2, units) == (64.0, "Ounce")
    # Another product's, or a global one, is not this product's size.
    assert package_size_of(rows, 3, 2, units) is None
    # Stocked in Ounces already: the amount is the size.
    assert package_size_of(rows, 1, 6, units) is None
    # A Pack of 6 Pieces is a count, not a size.
    assert package_size_of(
        [{"from_qu_id": 9, "to_qu_id": 2, "factor": 6, "product_id": 1}],
        1, 9, units) is None


def test_inventory_rows_carry_the_package_size(monkeypatch):
    stock = [{"product_id": 1, "amount": 2, "best_before_date": "2099-01-01",
              "product": {"id": 1, "name": "Whole milk", "qu_id_stock": 2,
                          "location_id": 1}},
             {"product_id": 2, "amount": 1, "best_before_date": "2099-01-01",
              "product": {"id": 2, "name": "Basil", "qu_id_stock": 2,
                          "location_id": 1}}]
    reads = {
        "/stock": stock,
        "/objects/locations": [{"id": 1, "name": "Fridge"}],
        "/objects/product_groups": [],
        "/objects/stock": [],
        "/objects/quantity_units": [PIECE, FLOZ],
        "/objects/quantity_unit_conversions": [
            {"from_qu_id": 2, "to_qu_id": 8, "factor": 64, "product_id": 1}],
    }

    async def fake_get(self, path):
        return reads[path]

    async def fake_cached(self, path):
        return reads[path]

    monkeypatch.setattr(GrocyClient, "_get", fake_get)
    monkeypatch.setattr(GrocyClient, "_cached_list", fake_cached)
    rows = {r["name"]: r for r in asyncio.run(GrocyClient().get_full_stock())}
    milk = rows["Whole milk"]
    assert (milk["package_size"], milk["package_unit"]) == (64.0, "Fluid ounce")
    assert milk["package_label"] == "64 fl oz"
    assert "package_label" not in rows["Basil"]


# --- The review screen ----------------------------------------------------------

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
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


@pytest.fixture
def pending(client, monkeypatch):
    async def _empty_stock(self):
        return []

    imported = []

    async def _import_ok(self, item):
        imported.append(item)
        return {"product_id": 1, "name": item.name}

    monkeypatch.setattr(GrocyClient, "get_stock", _empty_stock)
    monkeypatch.setattr(GrocyClient, "import_item", _import_ok)
    for row in client.get("pending/").json()["items"]:
        client.delete(f"pending/{row['id']}")
    return imported


def _add(client, **item):
    r = client.post("pending/items", json={"items": [item], "source": "receipt"})
    assert r.status_code == 200, r.text
    return r.json()["items"][0]


def test_a_receipt_line_lands_with_its_size(client, pending):
    row = _add(client, name="Whole milk 64 oz", quantity=1, unit="item")
    assert row["name"] == "Whole milk"
    assert (row["package_size"], row["package_unit"], row["size"]) == (
        64.0, "Fluid ounce", "64 fl oz")


def test_the_size_is_edited_on_review(client, pending):
    row = _add(client, name="Ponzu", quantity=1, unit="item")
    assert row["size"] == ""
    r = client.patch(f"pending/{row['id']}", json={"size": "10 fl oz"})
    assert r.status_code == 200
    assert r.json()["size"] == "10 fl oz"
    r = client.patch(f"pending/{row['id']}", json={"size": ""})
    assert r.json()["package_size"] is None


def test_a_size_that_is_no_size_is_refused(client, pending):
    row = _add(client, name="Ponzu", quantity=1, unit="item")
    r = client.patch(f"pending/{row['id']}", json={"size": "a bottle"})
    assert r.status_code == 422
    assert "is not a size" in r.json()["detail"]


def test_a_size_typed_into_the_name_moves_to_the_size(client, pending):
    row = _add(client, name="Hoisin", quantity=2, unit="item")
    r = client.patch(f"pending/{row['id']}", json={"name": "Hoisin sauce 8.5 fl oz"})
    assert (r.json()["name"], r.json()["size"]) == ("Hoisin sauce", "8.5 fl oz")


def test_the_size_reaches_the_import(client, pending):
    row = _add(client, name="Rao's marinara 24 oz", quantity=2, unit="item")
    r = client.post("pending/commit", json={"ids": [row["id"]]})
    assert r.status_code == 200, r.text
    (item,) = pending
    assert (item.name, item.quantity, item.package_size, item.package_unit) == (
        "Rao's marinara", 2.0, 24.0, "Ounce")


def test_the_pages_carry_the_size_fields():
    js = (_SERVICE_DIR / "app/static/js/pages/pending.js").read_text(encoding="utf-8")
    assert "patchItem(${item.id}, 'size', this.value, this)" in js
    inv = (_SERVICE_DIR / "app/static/js/pages/inventory.js").read_text(encoding="utf-8")
    assert "item.package_label" in inv
    add = (_SERVICE_DIR / "app/templates/add.html").read_text(encoding="utf-8")
    assert 'id="manual-size"' in add
    queue = (_SERVICE_DIR / "app/static/js/manage-pantry.js").read_text(encoding="utf-8")
    assert "setQueueSize(${i}, this)" in queue
    # The manual form checks its size before queueing the item.
    assert "await readSize(sizeBox.value)" in queue


def test_a_typed_size_is_read_as_it_is_typed():
    from app.main import app
    client = TestClient(app)
    ok = client.get("/inventory/size", params={"text": "half gallon"})
    assert ok.json() == {"package_size": 0.5, "package_unit": "Gallon", "size": "0.5 gal"}
    assert client.get("/inventory/size", params={"text": ""}).json()["size"] == ""
    bad = client.get("/inventory/size", params={"text": "1 jar"})
    assert bad.status_code == 422
    assert "is not a size" in bad.json()["detail"]


# --- Liquids by volume, and recipes across units ------------------------------

@pytest.mark.parametrize("name, poured", [
    ("O Organics Whole Milk Vitamin D", True), ("California Olive Ranch oil", True),
    ("Chicken broth", True), ("Half and half", True), ("Coconut milk", True),
    ("Apple cider vinegar", True),
    # Sold by weight, though a liquid is in the name.
    ("Milk chocolate bar", False), ("Cream cheese", False), ("Sour cream", False),
    ("Ice cream", False), ("Canola oil nonstick cooking spray", False),
    ("Baking soda", False), ("Cream of mushroom soup", False),
    ("Tuna in olive oil", False), ("Sweetened condensed milk", False),
    ("Cream of tartar", False), ("Rao's marinara", False), ("Honey", False),
])
def test_which_foods_are_poured(name, poured):
    from app.services.package_size import is_poured
    assert is_poured(name) is poured


def test_ounces_of_a_poured_liquid_are_fluid_ounces():
    milk = apply_package_size(FoodItem(name="Whole milk", package_size="64 oz"))
    assert (milk.package_size, milk.package_unit) == (64.0, "Fluid ounce")
    sauce = apply_package_size(FoodItem(name="Rao's marinara", package_size="24 oz"))
    assert (sauce.package_size, sauce.package_unit) == (24.0, "Ounce")


def test_a_size_first_kept_in_ounces_is_the_same_size_in_fluid_ounces():
    from app.services.grocy import _same_size
    assert _same_size((64.0, "Ounce"), 64, "Fluid ounce")
    assert not _same_size((32.0, "Ounce"), 64, "Fluid ounce")


def test_a_carton_now_read_in_fluid_ounces_stays_one_product(monkeypatch):
    milk = {"id": 1, "name": "Whole milk", "qu_id_stock": 2}
    held = [{"from_qu_id": 2, "to_qu_id": 6, "factor": 64.0, "product_id": 1},
            {"from_qu_id": 6, "to_qu_id": 2, "factor": 1 / 64, "product_id": 1}]
    sent, _ = _fake_grocy(monkeypatch, [milk], [PIECE, OUNCE, FLOZ], conversions=held)
    result = asyncio.run(GrocyClient().import_item(
        FoodItem(name="Whole milk", package_size="64 oz")))
    assert result["name"] == "Whole milk"
    assert "unit_note" not in result
    assert _conversion_posts(sent) == []


_UNITS = [{"id": 2, "name": "Piece", "name_plural": "Pieces"},
          {"id": 6, "name": "Gallon", "name_plural": "Gallons"},
          {"id": 7, "name": "Ounce", "name_plural": "Ounces"}]
_HALF_GALLON = [
    {"product_id": 5, "from_qu_id": 2, "to_qu_id": 6, "factor": 0.5},
    {"product_id": 5, "from_qu_id": 6, "to_qu_id": 2, "factor": 2.0}]


def test_a_recipe_in_cups_is_measured_against_a_carton():
    from app.services.units import conversion_factor
    # Grocy has no cup unit or global rows: 2 cups is a quarter of the carton.
    assert 2 * conversion_factor(5, "cups", "Piece", _UNITS, _HALF_GALLON) == pytest.approx(0.25)
    assert conversion_factor(5, "cup", "Gallon", _UNITS, _HALF_GALLON) == pytest.approx(1 / 16)


@pytest.mark.parametrize("product, unit", [
    # Weight never converts to volume.
    (5, "Ounce"),
    # Another product's carton size says nothing about this one.
    (9, "Piece"),
])
def test_fixed_ratios_never_guess(product, unit):
    from app.services.units import conversion_factor
    assert conversion_factor(product, "cup", unit, _UNITS, _HALF_GALLON) is None


def test_grocys_own_rows_still_win():
    from app.services.units import conversion_factor
    rows = _HALF_GALLON + [{"product_id": None, "from_qu_id": 6, "to_qu_id": 7, "factor": 130}]
    assert conversion_factor(5, "Gallon", "Ounce", _UNITS, rows) == 130


def test_the_recipe_check_says_whether_a_carton_covers_it():
    from app.services.recipe_match import _stock_covers_amount
    stock = {"product_id": 5, "amount": 1, "unit": "Piece"}
    assert _stock_covers_amount({"quantity": 2, "unit": "cups"}, stock, _UNITS, _HALF_GALLON) is True
    assert _stock_covers_amount({"quantity": 10, "unit": "cups"}, stock, _UNITS, _HALF_GALLON) is False


# --- Counts per package -------------------------------------------------------

@pytest.mark.parametrize("text, count", [
    ("12 CT", 12), ("12PK 12 FL OZ", 12), ("18 count", 18), ("dozen", 12),
    ("2 dozen", 24), ("half dozen", 6), ("24 pk 16.9 fl oz", 24),
    # Not counts.
    ("6 x 330 ml", None), ("7 grain bread", None), ("Pack of gum", None),
    ("2 lb", None), ("1 ct", None),
])
def test_how_many_a_package_holds(text, count):
    from app.services.package_size import parse_count
    assert parse_count(text) == count


@pytest.mark.parametrize("fields, expected", [
    # Two cartons of a dozen are 24 eggs, as "2 dozen" already was.
    (dict(name="LARGE EGGS", quantity=2, unit="item", package_size="12 CT"),
     (24.0, "Piece", "LARGE EGGS", None, None)),
    (dict(name="Dozen large eggs"), (12.0, "Piece", "large eggs", None, None)),
    # A pack of drinks: the size is each can's.
    (dict(name="COCA COLA", package_size="12PK 12 FL OZ"),
     (12.0, "Piece", "COCA COLA", 12.0, "Fluid ounce")),
    (dict(name="COCA COLA 12PK", package_size="12 FL OZ"),
     (12.0, "Piece", "COCA COLA", 12.0, "Fluid ounce")),
    # A count of pieces: the size is the whole package's, shared out.
    (dict(name="Mission tortillas 10 ct", package_size="17.5 oz"),
     (10.0, "Piece", "Mission tortillas", 1.75, "Ounce")),
    # The model already counted them out.
    (dict(name="Eggs", quantity=12, package_size="12 ct"),
     (12.0, "Piece", "Eggs", None, None)),
    # Sold by weight: no count to take.
    (dict(name="Apples", quantity=1.5, unit="lbs"), (1.5, "lbs", "Apples", None, None)),
])
def test_a_count_per_package_is_counted_out(fields, expected):
    item = apply_package_size(FoodItem(**fields))
    assert (item.quantity, item.unit, item.name, item.package_size,
            item.package_unit) == expected
    assert "package_count" not in item.model_dump()


def test_the_receipt_reply_counts_out_eggs():
    receipt = _parse_receipt({"items": [
        {"name": "LARGE EGGS", "quantity": 1, "unit": "item", "package_size": "12 CT"}]},
        0.85, "{}")
    (eggs,) = receipt.items
    assert (eggs.quantity, eggs.unit) == (12.0, "Piece")
