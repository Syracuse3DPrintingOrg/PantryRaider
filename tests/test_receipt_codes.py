"""A receipt's printed item numbers name the product.

US receipts print an item number before each line, usually the product's UPC
with its check digit and leading zeros left off. A line that reads "ORG BBY
SPINACH" is, by its code, a 5 oz tub of Organic Girl baby spinach. The AI
reads the number off the line, the app completes it into a barcode and looks
it up like a scan, and the line takes the product's full name, brand, size and
barcode. The digits may be misread, so no AI guesses a product from them, and
a product that shares no word with the line is passed over.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models.food import FoodItem
from app.providers.base import _parse_receipt
from app.services import barcode
from app.services import grocy as grocy_mod
from app.services.barcode import (BarcodeNotFound, receipt_code_barcodes,
                                  resolve_receipt_codes, same_product)


@pytest.mark.parametrize("code, expected", [
    # Kings: UPC without its check digit.
    ("89520300101", ["895203001011"]),
    ("89520300115", ["895203001158"]),
    # ... and without its leading zero.
    ("7989340064", ["079893400648"]),
    # Already whole, check digit and all.
    ("079893400648", ["079893400648"]),
    ("4006381333931", ["4006381333931"]),
    # A 12-digit EAN body.
    ("400638133393", ["4006381333931"]),
    # A produce PLU or nothing at all is no barcode.
    ("4011", []), ("", []), (None, []), ("abc", []),
])
def test_a_receipt_code_becomes_barcodes(code, expected):
    assert receipt_code_barcodes(code) == expected


@pytest.mark.parametrize("line, found, same", [
    ("ORG BBY SPINACH", "organicgirl Organic Girl Baby Spinach", True),
    ("ORGANIC GIRL ARUGU", "organicgirl baby arugula", True),
    ("0 ORG MILK WHL UHT", "O Organics whole milk vitamin D", True),
    ("ORG BBY SPINACH", "Tostitos tortilla chips", False),
    # Words every other product has prove nothing.
    ("ORG WHL MILK", "Organic whole wheat bread", False),
])
def test_the_found_product_must_share_a_word_with_the_line(line, found, same):
    assert same_product(line, found) is same


def test_the_reply_parser_keeps_the_code_but_never_sends_it():
    result = _parse_receipt({"items": [
        {"name": "ORG BBY SPINACH", "code": "89520300101", "quantity": 1},
        {"name": "Bananas", "code": "4011", "quantity": 2.1, "unit": "lbs"},
        {"name": "Milk", "code": None},
        {"name": "Eggs", "code": True},
    ]}, 0.85, "{}")
    assert [i.receipt_code for i in result.items] == ["89520300101", None, None, None]
    assert "receipt_code" not in result.items[0].model_dump()


def _found(name, brand=None, size=None, unit=None):
    return FoodItem(name=name, brand=brand, package_size=size, package_unit=unit)


def _resolve(monkeypatch, items, catalog, grocy=None):
    """Resolve ``items`` against Open Food Facts ``catalog`` and the user's
    Grocy, ``grocy`` mapping a barcode to what known_barcode answers."""
    asked = []

    async def known(self, code):
        asked.append(("grocy", code))
        if isinstance(grocy, Exception):
            raise grocy
        return (grocy or {}).get(code)

    monkeypatch.setattr(grocy_mod.GrocyClient, "known_barcode", known)

    async def fake_lookup(code, db, guess=True):
        asked.append((code, guess))
        if code not in catalog:
            raise BarcodeNotFound(code)
        return catalog[code]

    monkeypatch.setattr(barcode, "lookup_barcode", fake_lookup)
    asyncio.run(resolve_receipt_codes(items, db=None))
    return asked


def test_a_line_takes_the_products_name_size_and_barcode(monkeypatch):
    item = FoodItem(name="ORG BBY SPINACH", quantity=2, storage_type="refrigerated",
                    category="Produce", receipt_code="89520300101")
    asked = _resolve(monkeypatch, [item], {
        "895203001011": _found("Organic Girl Baby Spinach", "organicgirl", 5, "Ounce")})
    assert asked == [("grocy", "895203001011"), ("895203001011", False)]
    assert (item.name, item.brand, item.barcode) == (
        "Organic Girl Baby Spinach", "organicgirl", "895203001011")
    assert (item.package_size, item.package_unit) == (5, "Ounce")
    # The receipt's own quantity, place and category stand.
    assert (item.quantity, item.storage_type.value, item.category.value) == (
        2, "refrigerated", "Produce")
    assert item.receipt_code is None


def test_a_size_printed_on_the_line_wins(monkeypatch):
    item = FoodItem(name="Whole milk", package_size=64, package_unit="Fluid ounce",
                    receipt_code="7989340064")
    _resolve(monkeypatch, [item], {
        "079893400648": _found("O Organics Whole Milk", "O Organics", 1, "Gallon")})
    assert (item.package_size, item.package_unit) == (64, "Fluid ounce")


def test_an_unknown_code_leaves_the_line_as_printed(monkeypatch):
    item = FoodItem(name="ORG BBY SPINACH", receipt_code="89520300101")
    _resolve(monkeypatch, [item], {})
    assert (item.name, item.barcode) == ("ORG BBY SPINACH", None)


def test_a_misread_code_that_finds_another_product_is_passed_over(monkeypatch):
    item = FoodItem(name="ORG BBY SPINACH", receipt_code="89520300101")
    _resolve(monkeypatch, [item], {"895203001011": _found("Tostitos tortilla chips")})
    assert (item.name, item.barcode) == ("ORG BBY SPINACH", None)


def test_a_failed_lookup_costs_only_that_line(monkeypatch):
    async def broken(code, db, guess=True):
        raise barcode.BarcodeServiceError("Open Food Facts unreachable")

    async def unknown(self, code):
        return None

    monkeypatch.setattr(barcode, "lookup_barcode", broken)
    monkeypatch.setattr(grocy_mod.GrocyClient, "known_barcode", unknown)
    items = [FoodItem(name="ORG BBY SPINACH", receipt_code="89520300101"),
             FoodItem(name="Bananas")]
    asyncio.run(resolve_receipt_codes(items, db=None))
    assert [i.name for i in items] == ["ORG BBY SPINACH", "Bananas"]


def test_a_store_label_is_never_looked_up(monkeypatch):
    item = FoodItem(name="DELI TURKEY", receipt_code="21234500000")
    asked = _resolve(monkeypatch, [item], {})
    assert asked == []


def test_a_product_already_in_grocy_comes_in_as_the_user_left_it(monkeypatch):
    # The first carton's size was fixed on the review screen; the next
    # receipt's carton gets it without asking Open Food Facts.
    item = FoodItem(name="0 ORG MILK WHL UHT", quantity=2, receipt_code="7989340064")
    asked = _resolve(monkeypatch, [item], {
        "079893400648": _found("O Organics Whole Milk Vitamin D", "O Organics")},
        grocy={"079893400648": {"name": "O Organics shelf-stable whole milk",
                                "size": (64.0, "Fluid ounce")}})
    assert asked == [("grocy", "079893400648")]
    assert (item.name, item.barcode, item.quantity) == (
        "O Organics shelf-stable whole milk", "079893400648", 2)
    assert (item.package_size, item.package_unit) == (64.0, "Fluid ounce")


def test_a_renamed_product_is_still_found_by_its_barcode(monkeypatch):
    item = FoodItem(name="LIFE BAL PROT PWD", receipt_code="89520300101")
    _resolve(monkeypatch, [item], {}, grocy={
        "895203001011": {"name": "Life Balance dried egg whites", "size": None}})
    assert (item.name, item.package_size) == ("Life Balance dried egg whites", None)


def test_open_food_facts_answers_while_grocy_is_down(monkeypatch):
    item = FoodItem(name="ORG BBY SPINACH", receipt_code="89520300101")
    _resolve(monkeypatch, [item], {
        "895203001011": _found("Organic Girl Baby Spinach", "organicgirl", 5, "Ounce")},
        grocy=grocy_mod.GrocyError("Grocy is down"))
    assert (item.name, item.package_size) == ("Organic Girl Baby Spinach", 5)
