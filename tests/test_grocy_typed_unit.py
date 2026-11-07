"""The unit typed for an item reaches Grocy.

Adding "2 kg Basmati Rice", "1 gallon Milk" or "1 bag Peas" from the Manual
tab (or a receipt, photo or import) created every product in Pieces, so the
Inventory showed "2 Piece" rice. A new product now takes the typed unit; a
product Grocy already has keeps its own unit and converts when Grocy can (or
by the built-in kitchen measures when both units have a fixed size). A unit
that measures something else is refused rather than booking kilograms as
pieces, and anything else unconverted goes in with a note saying so.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.models.food import FoodItem
from app.services import grocy as grocy_mod
from app.services.grocy import (GrocyClient, counted_out, find_unit, unit_label,
                                unit_names)

PIECE = {"id": 2, "name": "Piece", "name_plural": "Pieces"}
KILOGRAM = {"id": 4, "name": "Kilogram", "name_plural": "Kilograms"}
GRAM = {"id": 7, "name": "Gram", "name_plural": "Grams"}


@pytest.mark.parametrize("typed, expected", [
    ("kg", ("Kilogram", "Kilograms")),
    ("Kg", ("Kilogram", "Kilograms")),
    ("kilograms", ("Kilogram", "Kilograms")),
    ("lbs", ("Pound", "Pounds")),
    ("gallon", ("Gallon", "Gallons")),
    ("fl  oz", ("Fluid ounce", "Fluid ounces")),
    ("item", ("Piece", "Pieces")),
    ("pcs", ("Piece", "Pieces")),
    ("", ("Piece", "Pieces")),
    ("bag", ("Bag", "Bags")),
    ("bags", ("Bag", "Bags")),
    ("boxes", ("Box", "Boxes")),
    ("loaves", ("Loaf", "Loaves")),
    ("glass", ("Glass", "Glasses")),
    ("500g", ("Piece", "Pieces")),
    ("Fl. Oz.", ("Fluid ounce", "Fluid ounces")),
    ("can(s)", ("Piece", "Pieces")),
    ("bag/box", ("Piece", "Pieces")),
])
def test_unit_names_reads_what_people_type(typed, expected):
    assert unit_names(typed)[:2] == expected


def test_find_unit_prefers_the_unit_named_as_typed():
    units = [KILOGRAM, {"id": 9, "name": "kg", "name_plural": "kg"}]
    assert find_unit(units, "kg")["id"] == 9
    assert find_unit(units, "kilos")["id"] == 4
    assert find_unit([PIECE], "item")["id"] == 2
    assert find_unit([PIECE], "bag") is None


def test_unit_label_is_plural_unless_exactly_one():
    assert unit_label(PIECE, 1) == "Piece"
    assert unit_label(PIECE, 12) == "Pieces"
    assert unit_label(PIECE, 0.5) == "Pieces"
    assert unit_label({"name": "Dash"}, 2) == "Dash"


def _mock_grocy(monkeypatch, products, units, conversions=(), units_later=None):
    """A fake Grocy. With ``units_later``, making a quantity unit fails, as
    when another add made it first, and the units read after that are
    ``units_later``."""
    sent = []
    unit_reads = []

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
                unit_reads.append(1)
                if units_later is not None and len(unit_reads) > 1:
                    return httpx.Response(200, json=units_later)
                return httpx.Response(200, json=units)
            if units_later is not None:
                return httpx.Response(400, json={
                    "error_message": "UNIQUE constraint failed: quantity_units.name"})
            return httpx.Response(200, json={"created_object_id": 50})
        if path.endswith("/objects/products"):
            if request.method == "GET":
                return httpx.Response(200, json=products)
            return httpx.Response(200, json={"created_object_id": 777})
        if "/objects/products/" in path:
            wanted = path.rsplit("/", 1)[1]
            return httpx.Response(200, json=next(
                (p for p in products if str(p["id"]) == wanted), {}))
        if "quantity_unit_conversions" in path:
            return httpx.Response(200, json=list(conversions))
        if "/stock/products/" in path and path.endswith("/add"):
            return httpx.Response(200, json=[{"id": 1}])
        return httpx.Response(404, json={"error_message": "unexpected " + path})

    monkeypatch.setattr(grocy_mod, "_client",
                        httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(grocy_mod.settings, "grocy_base_url", "http://grocy.test",
                        raising=False)
    return sent


def _posts(sent, suffix):
    return [b for m, p, b in sent if m == "POST" and p.endswith(suffix)]


def _added(sent, product_id):
    return next(b["amount"] for m, p, b in sent
                if p.endswith(f"/stock/products/{product_id}/add"))


def _rice(quantity=2.0, unit="kg"):
    return FoodItem(name="Basmati Rice", quantity=quantity, unit=unit)


def test_a_new_product_is_stocked_in_the_unit_typed(monkeypatch):
    sent = _mock_grocy(monkeypatch, [], [PIECE])
    result = asyncio.run(GrocyClient().import_item(_rice()))
    assert _posts(sent, "/objects/quantity_units") == [
        {"name": "Kilogram", "name_plural": "Kilograms"}]
    product = _posts(sent, "/objects/products")[0]
    assert product["qu_id_stock"] == product["qu_id_purchase"] == 50
    assert _added(sent, 777) == 2.0
    assert "unit_note" not in result


def test_a_unit_grocy_already_has_is_reused_under_any_spelling(monkeypatch):
    sent = _mock_grocy(monkeypatch, [], [PIECE, KILOGRAM])
    asyncio.run(GrocyClient().import_item(_rice(unit="kilograms")))
    assert _posts(sent, "/objects/quantity_units") == []
    assert _posts(sent, "/objects/products")[0]["qu_id_stock"] == 4


def test_no_unit_typed_still_means_piece(monkeypatch):
    sent = _mock_grocy(monkeypatch, [], [PIECE])
    asyncio.run(GrocyClient().import_item(_rice(quantity=12, unit="item")))
    assert _posts(sent, "/objects/quantity_units") == []
    assert _posts(sent, "/objects/products")[0]["qu_id_stock"] == 2


def test_an_existing_product_converts_when_grocy_can(monkeypatch):
    rice = {"id": 321, "name": "Basmati Rice", "qu_id_stock": 4}
    conversions = [{"product_id": None, "from_qu_id": 7, "to_qu_id": 4,
                    "factor": 0.001}]
    sent = _mock_grocy(monkeypatch, [rice], [PIECE, KILOGRAM, GRAM], conversions)
    result = asyncio.run(GrocyClient().import_item(_rice(quantity=500, unit="g")))
    assert _added(sent, 321) == pytest.approx(0.5)
    assert "unit_note" not in result
    assert _posts(sent, "/objects/products") == []


BAG = {"id": 11, "name": "Bag", "name_plural": "Bags"}


def test_an_existing_product_in_a_unit_of_no_fixed_size_says_so(monkeypatch):
    rice = {"id": 321, "name": "Basmati Rice", "qu_id_stock": 11}
    sent = _mock_grocy(monkeypatch, [rice], [PIECE, BAG])
    result = asyncio.run(GrocyClient().import_item(_rice()))
    assert _added(sent, 321) == 2.0
    assert _posts(sent, "/objects/quantity_units") == []
    assert _posts(sent, "/objects/products") == []
    assert result["unit_note"] == (
        "Basmati Rice is kept in Bags in Grocy, which has no conversion "
        "from kg, so it was added as 2 Bags.")


def test_a_weight_into_a_product_kept_in_pieces_is_refused(monkeypatch):
    # "2 kg" is never booked as 2 Pieces: the add fails, so the item stays
    # in the queue with the reason, and nothing is made or stocked in Grocy.
    rice = {"id": 321, "name": "Basmati Rice", "qu_id_stock": 2}
    sent = _mock_grocy(monkeypatch, [rice], [PIECE])
    with pytest.raises(grocy_mod.GrocyError) as err:
        asyncio.run(GrocyClient().import_item(_rice()))
    assert str(err.value) == (
        "Basmati Rice is kept in Pieces in Grocy, and kg cannot be turned into "
        "Pieces without a unit conversion. Change the unit to Pieces, or add a "
        "conversion for it in Grocy, then add it again.")
    assert [m for m, *_ in sent if m == "POST"] == []


def test_an_existing_product_with_no_unit_typed_adds_quietly(monkeypatch):
    rice = {"id": 321, "name": "Basmati Rice", "qu_id_stock": 4}
    sent = _mock_grocy(monkeypatch, [rice], [PIECE, KILOGRAM])
    result = asyncio.run(GrocyClient().import_item(_rice(quantity=1, unit="item")))
    assert _added(sent, 321) == 1.0
    assert "unit_note" not in result


def test_no_unit_typed_is_never_scaled_by_a_piece_conversion(monkeypatch):
    # A kitchen that keeps flour in Grams and says a Piece (a bag) is 1000 g
    # still gets the amount it entered, as before units were read.
    flour = {"id": 321, "name": "Flour", "qu_id_stock": 7}
    conversions = [{"product_id": 321, "from_qu_id": 2, "to_qu_id": 7,
                    "factor": 1000}]
    sent = _mock_grocy(monkeypatch, [flour], [PIECE, GRAM], conversions)
    item = FoodItem(name="Flour", quantity=500, unit="item")
    result = asyncio.run(GrocyClient().import_item(item))
    assert _added(sent, 321) == 500
    assert "unit_note" not in result


def test_a_product_made_earlier_in_the_same_batch_keeps_its_unit(monkeypatch):
    sent = _mock_grocy(monkeypatch, [], [PIECE])
    client = GrocyClient()
    asyncio.run(client.import_item(_rice()))
    result = asyncio.run(client.import_item(_rice(quantity=1, unit="Kg")))
    assert len(_posts(sent, "/objects/products")) == 1
    assert len(_posts(sent, "/objects/quantity_units")) == 1
    assert "unit_note" not in result


@pytest.mark.parametrize("quantity, typed, expected", [
    (1, "dozen", (12, "Piece")),
    (2, "Dozens", (24, "Piece")),
    (1, "doz.", (12, "Piece")),
    (1, "half a dozen", (6, "Piece")),
    (1, "half dozen", (6, "Piece")),
    (3, "pairs", (6, "Piece")),
    (2, "kg", None),
])
def test_fixed_counts_are_pieces(quantity, typed, expected):
    assert counted_out(quantity, typed) == expected


def _eggs(quantity=1.0, unit="dozen"):
    return FoodItem(name="Eggs", quantity=quantity, unit=unit)


def test_a_dozen_makes_a_new_product_in_pieces(monkeypatch):
    sent = _mock_grocy(monkeypatch, [], [PIECE])
    result = asyncio.run(GrocyClient().import_item(_eggs()))
    assert _posts(sent, "/objects/quantity_units") == []
    assert _posts(sent, "/objects/products")[0]["qu_id_stock"] == 2
    assert _added(sent, 777) == 12
    assert "unit_note" not in result


def test_a_dozen_never_creates_a_dozen_unit(monkeypatch):
    sent = _mock_grocy(monkeypatch, [], [])
    asyncio.run(GrocyClient().import_item(_eggs()))
    assert _posts(sent, "/objects/quantity_units") == [
        {"name": "Piece", "name_plural": "Pieces"}]


def test_half_a_dozen_adds_six_to_a_product_in_pieces(monkeypatch):
    eggs = {"id": 321, "name": "Eggs", "qu_id_stock": 2}
    sent = _mock_grocy(monkeypatch, [eggs], [PIECE])
    result = asyncio.run(GrocyClient().import_item(_eggs(unit="half a dozen")))
    assert _added(sent, 321) == 6
    assert "unit_note" not in result


def test_a_dozen_on_a_product_kept_by_weight_is_refused(monkeypatch):
    eggs = {"id": 321, "name": "Eggs", "qu_id_stock": 4}
    sent = _mock_grocy(monkeypatch, [eggs], [PIECE, KILOGRAM])
    with pytest.raises(grocy_mod.GrocyError, match="and 1 dozen cannot be"):
        asyncio.run(GrocyClient().import_item(_eggs()))
    assert [m for m, *_ in sent if m == "POST"] == []


def test_a_dozen_on_a_product_in_a_unit_of_no_fixed_size_says_so(monkeypatch):
    eggs = {"id": 321, "name": "Eggs", "qu_id_stock": 11}
    sent = _mock_grocy(monkeypatch, [eggs], [PIECE, BAG])
    result = asyncio.run(GrocyClient().import_item(_eggs()))
    assert _added(sent, 321) == 12
    assert result["unit_note"] == (
        "Eggs is kept in Bags in Grocy, which has no conversion from "
        "Pieces (1 dozen), so it was added as 12 Bags.")


DOZEN = {"id": 8, "name": "Dozen", "name_plural": "Dozens"}


def test_a_product_kept_in_its_own_dozen_unit_adds_one_dozen(monkeypatch):
    eggs = {"id": 321, "name": "Eggs", "qu_id_stock": 8}
    sent = _mock_grocy(monkeypatch, [eggs], [PIECE, DOZEN])
    result = asyncio.run(GrocyClient().import_item(_eggs()))
    assert _added(sent, 321) == 1
    assert "unit_note" not in result


def test_a_dozen_unit_the_product_converts_from_is_used_as_it_is(monkeypatch):
    eggs = {"id": 321, "name": "Eggs", "qu_id_stock": 2}
    conversions = [{"product_id": 321, "from_qu_id": 8, "to_qu_id": 2, "factor": 12}]
    sent = _mock_grocy(monkeypatch, [eggs], [PIECE, DOZEN], conversions)
    asyncio.run(GrocyClient().import_item(_eggs(quantity=2)))
    assert _added(sent, 321) == 24


def test_a_known_barcode_in_a_dozen_unit_adds_one_dozen(monkeypatch):
    eggs = {"id": 321, "name": "Eggs", "qu_id_stock": 8}
    sent = _mock_grocy(monkeypatch, [eggs], [PIECE, DOZEN])
    result = asyncio.run(GrocyClient().add_to_product(321, _eggs(unit="Dozen"), 3))
    assert _added(sent, 321) == 1
    assert result["amount"] == 1


def test_a_known_barcode_counts_a_dozen_out_when_grocy_has_no_such_unit(monkeypatch):
    eggs = {"id": 321, "name": "Eggs", "qu_id_stock": 2}
    sent = _mock_grocy(monkeypatch, [eggs], [PIECE])
    asyncio.run(GrocyClient().add_to_product(321, _eggs(), 3))
    assert _added(sent, 321) == 12


# A product made in the unit typed for it has no Grocy conversion to anything,
# so the units with a fixed size convert by their own sizes.

@pytest.mark.parametrize("src, dst, factor", [
    ("g", "Kilogram", 0.001),
    ("kg", "g", 1000),
    ("lb", "oz", 16),
    ("mg", "g", 0.001),
    ("gallon", "quarts", 4),
    ("quart", "pint", 2),
    ("pint", "cups", 2),
    ("cup", "fl oz", 8),
    ("tbsp", "tsp", 3),
    ("T", "t", 3),
    ("fl oz", "tbsp", 2),
    ("l", "ml", 1000),
])
def test_builtin_factor_uses_us_kitchen_measures(src, dst, factor):
    assert grocy_mod.builtin_factor(src, dst) == pytest.approx(factor)


@pytest.mark.parametrize("src, dst", [
    ("kg", "l"), ("cup", "lb"), ("pcs", "kg"), ("kg", "bag"), ("item", "kg"),
])
def test_builtin_factor_needs_two_sizes_of_one_kind(src, dst):
    assert grocy_mod.builtin_factor(src, dst) is None


def test_grams_into_a_product_made_in_kilograms_convert(monkeypatch):
    rice = {"id": 321, "name": "Basmati Rice", "qu_id_stock": 4}
    sent = _mock_grocy(monkeypatch, [rice], [PIECE, KILOGRAM])
    result = asyncio.run(GrocyClient().import_item(_rice(quantity=500, unit="g")))
    assert _added(sent, 321) == pytest.approx(0.5)
    assert "unit_note" not in result


def test_grocys_own_conversion_beats_the_builtin_one(monkeypatch):
    # A kitchen that says its Cup of rice weighs 0.2 kg gets that.
    rice = {"id": 321, "name": "Basmati Rice", "qu_id_stock": 4}
    cup = {"id": 12, "name": "Cup", "name_plural": "Cups"}
    conversions = [{"product_id": 321, "from_qu_id": 12, "to_qu_id": 4,
                    "factor": 0.2}]
    sent = _mock_grocy(monkeypatch, [rice], [PIECE, KILOGRAM, cup], conversions)
    asyncio.run(GrocyClient().import_item(_rice(quantity=2, unit="cups")))
    assert _added(sent, 321) == pytest.approx(0.4)


def test_a_volume_into_a_product_kept_by_weight_is_refused(monkeypatch):
    rice = {"id": 321, "name": "Basmati Rice", "qu_id_stock": 4}
    sent = _mock_grocy(monkeypatch, [rice], [PIECE, KILOGRAM])
    with pytest.raises(grocy_mod.GrocyError) as err:
        asyncio.run(GrocyClient().import_item(_rice(quantity=2, unit="cups")))
    assert err.value.kind == "unit"
    assert "Basmati Rice is kept in Kilograms" in str(err.value)
    assert not any(p.endswith("/add") for _, p, _ in sent)


def test_a_refused_unit_on_a_known_barcode_adds_nothing(monkeypatch):
    rice = {"id": 321, "name": "Basmati Rice", "qu_id_stock": 4}
    sent = _mock_grocy(monkeypatch, [rice], [PIECE, KILOGRAM])
    with pytest.raises(grocy_mod.GrocyError):
        asyncio.run(GrocyClient().add_to_product(321, _rice(unit="cup"), 3))
    assert not any(p.endswith("/add") for _, p, _ in sent)


# A known barcode's add carries the same unit note as any other add.

def test_add_to_product_returns_the_unit_note(monkeypatch):
    rice = {"id": 321, "name": "Basmati Rice", "qu_id_stock": 11}
    sent = _mock_grocy(monkeypatch, [rice], [PIECE, BAG])
    result = asyncio.run(GrocyClient().add_to_product(321, _rice(), 3))
    assert _added(sent, 321) == 2.0
    assert result["unit_note"] == (
        "Basmati Rice is kept in Bags in Grocy, which has no conversion "
        "from kg, so it was added as 2 Bags.")


def test_add_to_product_notes_a_dozen_counted_out(monkeypatch):
    eggs = {"id": 321, "name": "Eggs", "qu_id_stock": 11}
    sent = _mock_grocy(monkeypatch, [eggs], [PIECE, BAG])
    result = asyncio.run(GrocyClient().add_to_product(321, _eggs(), 3))
    assert _added(sent, 321) == 12
    assert "Pieces (1 dozen)" in result["unit_note"]


def test_add_to_product_converts_without_a_note(monkeypatch):
    rice = {"id": 321, "name": "Basmati Rice", "qu_id_stock": 4}
    sent = _mock_grocy(monkeypatch, [rice], [PIECE, KILOGRAM])
    result = asyncio.run(GrocyClient().add_to_product(321, _rice(500, "g"), 3))
    assert _added(sent, 321) == pytest.approx(0.5)
    assert "unit_note" not in result


def test_review_shows_the_unit_note_of_every_add():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1]
          / "service/app/static/js/pages/pending.js").read_text()
    assert "x.status === 'ok' && x.unit_note" in js


# Piece spellings are a real unit typed; only no unit at all is quiet.

@pytest.mark.parametrize("typed", ["pcs", "each", "ea", "count", "ct", "units",
                                   "pieces"])
def test_piece_spellings_into_another_unit_say_so(monkeypatch, typed):
    flour = {"id": 321, "name": "Flour", "qu_id_stock": 11}
    sent = _mock_grocy(monkeypatch, [flour], [PIECE, BAG])
    item = FoodItem(name="Flour", quantity=2, unit=typed)
    result = asyncio.run(GrocyClient().import_item(item))
    assert _added(sent, 321) == 2
    assert result["unit_note"].startswith("Flour is kept in Bags in Grocy")


def test_piece_spellings_into_a_product_kept_by_weight_say_so(monkeypatch):
    # "2 each" off a receipt for a product kept in Pounds is vague rather
    # than certainly wrong, so it goes in with a note instead of a refusal.
    pound = {"id": 13, "name": "Pound", "name_plural": "Pounds"}
    flour = {"id": 321, "name": "Flour", "qu_id_stock": 13}
    sent = _mock_grocy(monkeypatch, [flour], [PIECE, pound])
    item = FoodItem(name="Flour", quantity=2, unit="each")
    result = asyncio.run(GrocyClient().import_item(item))
    assert _added(sent, 321) == 2
    assert "which has no conversion from each" in result["unit_note"]


def test_piece_spellings_into_a_product_in_pieces_are_quiet(monkeypatch):
    eggs = {"id": 321, "name": "Eggs", "qu_id_stock": 2}
    sent = _mock_grocy(monkeypatch, [eggs], [PIECE])
    result = asyncio.run(GrocyClient().import_item(_eggs(quantity=3, unit="pcs")))
    assert _added(sent, 321) == 3
    assert "unit_note" not in result


# One letter: "t" is a teaspoon and "T" a tablespoon; any other is a Piece.

@pytest.mark.parametrize("typed, expected", [
    ("t", "Teaspoon"), ("tsp", "Teaspoon"), ("t.", "Teaspoon"),
    ("T", "Tablespoon"), ("tbsp", "Tablespoon"), ("tbs", "Tablespoon"),
    ("tbl", "Tablespoon"), ("T.", "Tablespoon"),
    ("mg", "Milligram"), ("c", "Cup"), ("C", "Cup"), ("L", "Liter"),
    ("x", "Piece"), ("q", "Piece"),
])
def test_short_units(typed, expected):
    assert unit_names(typed)[0] == expected


def test_t_and_capital_t_never_share_a_grocy_unit():
    tsp = {"id": 20, "name": "t", "name_plural": "t"}
    tbsp = {"id": 21, "name": "T", "name_plural": "T"}
    assert find_unit([tsp, tbsp], "T")["id"] == 21
    assert find_unit([tsp, tbsp], "t")["id"] == 20
    assert find_unit([tsp], "T") is None
    assert find_unit([{"id": 22, "name": "Tablespoon"}], "T")["id"] == 22


def test_t_makes_a_teaspoon(monkeypatch):
    sent = _mock_grocy(monkeypatch, [], [PIECE])
    asyncio.run(GrocyClient().import_item(FoodItem(name="Vanilla", quantity=1, unit="t")))
    assert _posts(sent, "/objects/quantity_units") == [
        {"name": "Teaspoon", "name_plural": "Teaspoons"}]


def test_an_unknown_letter_never_makes_a_unit(monkeypatch):
    sent = _mock_grocy(monkeypatch, [], [PIECE])
    asyncio.run(GrocyClient().import_item(FoodItem(name="Kale", quantity=1, unit="x")))
    assert _posts(sent, "/objects/quantity_units") == []
    assert _posts(sent, "/objects/products")[0]["qu_id_stock"] == 2


# "1 doz." into a product kept in a Grocy unit named Dozen is 1 Dozen.

@pytest.mark.parametrize("typed", ["doz.", "doz", "Dozens", "dozen"])
def test_any_spelling_of_dozen_finds_the_kitchens_dozen_unit(monkeypatch, typed):
    eggs = {"id": 321, "name": "Eggs", "qu_id_stock": 8}
    sent = _mock_grocy(monkeypatch, [eggs], [PIECE, DOZEN])
    result = asyncio.run(GrocyClient().import_item(_eggs(unit=typed)))
    assert _added(sent, 321) == 1
    assert "unit_note" not in result


def test_half_a_dozen_into_pieces_is_still_six(monkeypatch):
    eggs = {"id": 321, "name": "Eggs", "qu_id_stock": 2}
    sent = _mock_grocy(monkeypatch, [eggs], [PIECE, DOZEN])
    asyncio.run(GrocyClient().add_to_product(321, _eggs(unit="half a dozen"), 3))
    assert _added(sent, 321) == 6


# Two adds making the same new unit at once.

def test_a_unit_made_by_another_add_meanwhile_is_used(monkeypatch):
    sent = _mock_grocy(monkeypatch, [], [PIECE],
                       units_later=[PIECE, {"id": 60, "name": "Kilogram",
                                            "name_plural": "Kilograms"}])
    asyncio.run(GrocyClient().import_item(_rice()))
    assert _posts(sent, "/objects/products")[0]["qu_id_stock"] == 60
    assert _added(sent, 777) == 2.0


def test_a_unit_that_still_cannot_be_made_fails_the_add(monkeypatch):
    sent = _mock_grocy(monkeypatch, [], [PIECE], units_later=[PIECE])
    with pytest.raises(grocy_mod.GrocyError):
        asyncio.run(GrocyClient().import_item(_rice()))
    assert _posts(sent, "/objects/products") == []


# Made-up units: plurals the s rule gets wrong, and no long phrases.

@pytest.mark.parametrize("typed, expected", [
    ("potatoes", ("Potato", "Potatoes")),
    ("leaves", ("Leaf", "Leaves")),
    ("leaf", ("Leaf", "Leaves")),
    ("halves", ("Half", "Halves")),
    ("bus", ("Bus", "Buses")),
    ("citrus", ("Citrus", "Citruses")),
    ("slices", ("Slice", "Slices")),
    ("jars", ("Jar", "Jars")),
    ("big bags", ("Big bag", "Big bags")),
    ("bag of frozen peas", ("Piece", "Pieces")),
])
def test_made_up_units_read_sensibly(typed, expected):
    assert unit_names(typed)[:2] == expected
