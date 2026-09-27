"""Grocy quantity-unit conversion resolution (FoodAssistant-imod).

Truth table for services.units.conversion_factor (product-specific beats
global, one-hop chains, missing chains, rejected factors, case-insensitive
names) plus the matcher wiring: an ingredient amount in a convertible unit
counts stock correctly, and without a resolvable conversion everything
behaves exactly as before.
"""
import pytest

from app.config import settings
from app.services.mealie import (classify_recipes, partition_recipe_ingredients,
                                 reset_staple_cache)
from app.services.units import conversion_factor

# Unit table: id 1 Gram/Grams, id 2 Bottle/Bottles, id 3 Milliliter/Milliliters,
# id 4 Case/Cases. A duplicate-named row proves first-claim determinism.
UNITS = [
    {"id": 1, "name": "Gram", "name_plural": "Grams"},
    {"id": 2, "name": "Bottle", "name_plural": "Bottles"},
    {"id": 3, "name": "Milliliter", "name_plural": "Milliliters"},
    {"id": 4, "name": "Case", "name_plural": "Cases"},
]


# ── conversion_factor truth table ────────────────────────────────────────────

def test_global_direct_row():
    rows = [{"id": 1, "product_id": None, "from_qu_id": 1, "to_qu_id": 2,
             "factor": 0.002}]
    assert conversion_factor(7, "Gram", "Bottle", UNITS, rows) == 0.002


def test_product_specific_beats_global():
    rows = [
        {"id": 1, "product_id": None, "from_qu_id": 1, "to_qu_id": 2, "factor": 0.002},
        {"id": 2, "product_id": 7, "from_qu_id": 1, "to_qu_id": 2, "factor": 0.004},
    ]
    assert conversion_factor(7, "Gram", "Bottle", UNITS, rows) == 0.004


def test_other_products_rows_are_ignored():
    rows = [
        {"id": 1, "product_id": None, "from_qu_id": 1, "to_qu_id": 2, "factor": 0.002},
        {"id": 2, "product_id": 99, "from_qu_id": 1, "to_qu_id": 2, "factor": 0.004},
    ]
    assert conversion_factor(7, "Gram", "Bottle", UNITS, rows) == 0.002


def test_one_hop_chain_multiplies():
    # Gram -> Milliliter -> Bottle: 1 g = 1 ml, 1 ml = 0.002 bottles.
    rows = [
        {"id": 1, "product_id": None, "from_qu_id": 1, "to_qu_id": 3, "factor": 1.0},
        {"id": 2, "product_id": None, "from_qu_id": 3, "to_qu_id": 2, "factor": 0.002},
    ]
    assert conversion_factor(7, "Gram", "Bottle", UNITS, rows) == pytest.approx(0.002)


def test_product_row_wins_inside_a_chain():
    rows = [
        {"id": 1, "product_id": None, "from_qu_id": 1, "to_qu_id": 3, "factor": 1.0},
        {"id": 2, "product_id": 7, "from_qu_id": 1, "to_qu_id": 3, "factor": 2.0},
        {"id": 3, "product_id": None, "from_qu_id": 3, "to_qu_id": 2, "factor": 0.002},
    ]
    assert conversion_factor(7, "Gram", "Bottle", UNITS, rows) == pytest.approx(0.004)


def test_two_hops_are_never_followed():
    # Gram -> Milliliter -> Bottle -> Case exists, but Gram -> Case needs two
    # hops, so it must NOT resolve.
    rows = [
        {"id": 1, "product_id": None, "from_qu_id": 1, "to_qu_id": 3, "factor": 1.0},
        {"id": 2, "product_id": None, "from_qu_id": 3, "to_qu_id": 2, "factor": 0.002},
        {"id": 3, "product_id": None, "from_qu_id": 2, "to_qu_id": 4, "factor": 0.1},
    ]
    assert conversion_factor(7, "Gram", "Case", UNITS, rows) is None


def test_missing_chain_returns_none():
    assert conversion_factor(7, "Gram", "Bottle", UNITS, []) is None


def test_direction_is_not_inverted():
    # Only Bottle -> Gram is defined; Gram -> Bottle stays unresolvable rather
    # than guessing 1/factor.
    rows = [{"id": 1, "product_id": None, "from_qu_id": 2, "to_qu_id": 1,
             "factor": 500}]
    assert conversion_factor(7, "Gram", "Bottle", UNITS, rows) is None


def test_zero_factor_rejected():
    rows = [{"id": 1, "product_id": None, "from_qu_id": 1, "to_qu_id": 2,
             "factor": 0}]
    assert conversion_factor(7, "Gram", "Bottle", UNITS, rows) is None


def test_negative_factor_rejected():
    rows = [{"id": 1, "product_id": None, "from_qu_id": 1, "to_qu_id": 2,
             "factor": -2}]
    assert conversion_factor(7, "Gram", "Bottle", UNITS, rows) is None


def test_garbage_rows_are_skipped_not_fatal():
    rows = [
        "junk",
        {"id": 1, "product_id": "seven", "from_qu_id": 1, "to_qu_id": 2, "factor": 9},
        {"id": 2, "product_id": None, "from_qu_id": None, "to_qu_id": 2, "factor": 9},
        {"id": 3, "product_id": None, "from_qu_id": 1, "to_qu_id": 2, "factor": "x"},
        {"id": 4, "product_id": None, "from_qu_id": 1, "to_qu_id": 2, "factor": 0.5},
    ]
    assert conversion_factor(7, "Gram", "Bottle", UNITS, rows) == 0.5


def test_unit_names_match_case_insensitively_and_plural():
    rows = [{"id": 1, "product_id": None, "from_qu_id": 1, "to_qu_id": 2,
             "factor": 0.002}]
    assert conversion_factor(7, "  gRaMs ", "bottles", UNITS, rows) == 0.002


def test_same_unit_name_is_identity():
    assert conversion_factor(7, "Gram", "gram", [], []) == 1.0
    assert conversion_factor(7, "Grams", "gram", UNITS, []) == 1.0


def test_unknown_unit_name_returns_none():
    assert conversion_factor(7, "Cup", "Gram", UNITS, []) is None
    assert conversion_factor(7, "Gram", "Cup", UNITS, []) is None


def test_blank_unit_names_return_none():
    assert conversion_factor(7, "", "Gram", UNITS, []) is None
    assert conversion_factor(7, "Gram", None, UNITS, []) is None


# ── Matcher wiring ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fixed_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "staple_items", "")
    monkeypatch.setattr(settings, "perishable_days", 14)
    monkeypatch.setattr(settings, "expiring_soon_days", 5)
    reset_staple_cache()
    yield
    reset_staple_cache()


def ing(food, qty=None, unit=None):
    entry = {"food": {"name": food}, "note": food}
    if qty is not None:
        entry["quantity"] = qty
    if unit is not None:
        entry["unit"] = {"name": unit}
    return entry


def recipe(name, ingredients):
    return {"name": name, "slug": name, "id": name,
            "recipeIngredient": ingredients}


def hummus_stock(amount=2.0, unit="Bottle", days=2):
    return {"name": "Tahini", "product_id": 7, "amount": amount, "unit": unit,
            "days_remaining": days, "storage_bucket": "refrigerated"}


G_TO_BOTTLE = [{"id": 1, "product_id": 7, "from_qu_id": 1, "to_qu_id": 2,
                "factor": 0.002}]  # 500 g per bottle


def test_convertible_and_sufficient_stays_ready():
    # Wants 200 g, stock is 2 bottles (1000 g): covered, ready as before.
    tiers = classify_recipes(
        [recipe("Hummus", [ing("tahini", qty=200, unit="Gram")])],
        [hummus_stock()], units=UNITS, conversions=G_TO_BOTTLE)
    assert [r["name"] for r in tiers["ready"]] == ["Hummus"]


def test_convertible_but_short_counts_as_needing_a_shop_run():
    # Wants 1200 g, stock is 2 bottles (1000 g): the name match alone no longer
    # covers it. The perishable tahini still earns the recipe its place in the
    # shopping tier, with the ingredient honestly unmatched.
    tiers = classify_recipes(
        [recipe("Hummus", [ing("tahini", qty=1200, unit="Gram")])],
        [hummus_stock()], units=UNITS, conversions=G_TO_BOTTLE)
    assert not tiers["ready"] and not tiers["staples"]
    assert [r["name"] for r in tiers["shopping"]] == ["Hummus"]
    r = tiers["shopping"][0]
    assert "tahini" in r["unmatched_ingredients"][0]
    assert r["expiring_items_used"] == ["Tahini"]


def test_no_conversion_chain_keeps_todays_name_only_match():
    # Same short amount, but no conversion rows at all: behavior is exactly as
    # today, the name match counts and the recipe is ready.
    tiers = classify_recipes(
        [recipe("Hummus", [ing("tahini", qty=1200, unit="Gram")])],
        [hummus_stock()], units=UNITS, conversions=[])
    assert [r["name"] for r in tiers["ready"]] == ["Hummus"]


def test_no_tables_passed_keeps_todays_name_only_match():
    tiers = classify_recipes(
        [recipe("Hummus", [ing("tahini", qty=1200, unit="Gram")])],
        [hummus_stock()])
    assert [r["name"] for r in tiers["ready"]] == ["Hummus"]


def test_amount_free_ingredient_never_penalized():
    # "tahini, to taste" carries no quantity, so conversions cannot apply.
    tiers = classify_recipes(
        [recipe("Hummus", [ing("tahini")])],
        [hummus_stock()], units=UNITS, conversions=G_TO_BOTTLE)
    assert [r["name"] for r in tiers["ready"]] == ["Hummus"]


def test_same_unit_amounts_compare_without_conversion_rows():
    # Ingredient and stock both in grams: identity conversion, so 800 g of
    # stock cannot cover 1200 g even with no rows defined.
    tiers = classify_recipes(
        [recipe("Hummus", [ing("tahini", qty=1200, unit="Gram")])],
        [hummus_stock(amount=800, unit="Grams")],
        units=UNITS, conversions=[])
    assert [r["name"] for r in tiers["shopping"]] == ["Hummus"]


def test_second_stock_row_can_still_cover():
    # The first token match is short but another stock row covers the amount;
    # the ingredient still counts as matched.
    short = hummus_stock(amount=0.1)
    full = {"name": "Tahini Paste", "product_id": 8, "amount": 3.0,
            "unit": "Bottle", "days_remaining": 30, "storage_bucket": "pantry"}
    rows = G_TO_BOTTLE + [{"id": 2, "product_id": 8, "from_qu_id": 1,
                           "to_qu_id": 2, "factor": 0.002}]
    tiers = classify_recipes(
        [recipe("Hummus", [ing("tahini", qty=1200, unit="Gram")])],
        [short, full], units=UNITS, conversions=rows)
    assert [r["name"] for r in tiers["ready"]] == ["Hummus"]


def test_partition_short_ingredient_lands_on_needed():
    out = partition_recipe_ingredients(
        [ing("tahini", qty=1200, unit="Gram"), ing("wasabi")],
        [hummus_stock()], units=UNITS, conversions=G_TO_BOTTLE)
    assert out["owned"] == []
    assert len(out["needed"]) == 2


def test_partition_sufficient_ingredient_stays_owned():
    out = partition_recipe_ingredients(
        [ing("tahini", qty=200, unit="Gram")],
        [hummus_stock()], units=UNITS, conversions=G_TO_BOTTLE)
    assert len(out["owned"]) == 1
    assert out["needed"] == []


def test_partition_without_tables_is_unchanged():
    out = partition_recipe_ingredients(
        [ing("tahini", qty=1200, unit="Gram")], [hummus_stock()])
    assert len(out["owned"]) == 1
    assert out["needed"] == []


def test_partition_short_staple_stays_owned():
    # A staple is assumed on hand even when the tracked amount runs short.
    flour_stock = {"name": "Flour", "product_id": 9, "amount": 100,
                   "unit": "Gram", "days_remaining": 200,
                   "storage_bucket": "pantry"}
    out = partition_recipe_ingredients(
        [ing("flour", qty=500, unit="Gram")], [flour_stock],
        units=UNITS, conversions=[])
    assert len(out["owned"]) == 1
    assert out["needed"] == []
