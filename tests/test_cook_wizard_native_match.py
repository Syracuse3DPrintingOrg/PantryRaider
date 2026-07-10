"""Cook wizard guided finder: native/local recipes must actually match the
cuisine/diet question, and must not be forced above better matches.

FoodAssistant-nomr: the Cook wizard's guided finder ("Help me find a recipe")
sent /mealie/suggest?cuisine=...&dietary=... but the native/local library was
handed to classify_recipes unfiltered, so every recipe in the user's own
library showed up regardless of the answers given, and (being tiered before
the external results) sat above web recipes that did honor the filters. These
pure tests exercise the fix directly on the filtering function used by the
/mealie/suggest route.
"""
from __future__ import annotations

from app.services.recipes_external import (
    filter_native_by_cuisine,
    filter_native_recipes,
)

ITALIAN = {
    "name": "Weeknight Lasagna", "description": "A classic baked pasta.",
    "tags": ["Italian", "comfort food"], "categories": ["Pasta"],
    "recipeIngredient": [{"note": "lasagna noodles"}, {"note": "ground beef"}],
}
THAI = {
    "name": "Green Curry", "description": "Coconut curry with vegetables.",
    "tags": ["Thai"], "categories": ["Curry"],
    "recipeIngredient": [{"note": "coconut milk"}, {"note": "chicken thigh"}],
}
UNTAGGED = {
    "name": "Grandma's Casserole", "description": "",
    "tags": [], "categories": [],
    "recipeIngredient": [{"note": "chicken breast"}, {"note": "rice"}],
}


def test_no_cuisine_filter_keeps_everything():
    # No question asked yet: nothing to narrow by, so nothing is dropped.
    out = filter_native_by_cuisine([ITALIAN, THAI, UNTAGGED], "")
    assert out == [ITALIAN, THAI, UNTAGGED]


def test_cuisine_filter_excludes_non_matching_local_recipes():
    # This is the bug: before the fix, every native recipe passed through
    # /mealie/suggest untouched by the cuisine query param. Asking for
    # "Thai" must drop the Italian recipe.
    out = filter_native_by_cuisine([ITALIAN, THAI], "Thai")
    assert out == [THAI]


def test_cuisine_filter_drops_untagged_recipes_instead_of_keeping_them():
    # Unlike the external-source filter (which keeps untagged candidates
    # because absence isn't evidence of a mismatch there), a native recipe
    # with no cuisine hint at all must be dropped -- otherwise every recipe
    # in a typical library (almost none of which are cuisine-tagged) would
    # keep showing up for every cuisine question, reproducing the bug.
    out = filter_native_by_cuisine([UNTAGGED], "Thai")
    assert out == []


def test_cuisine_filter_expands_broad_regions():
    # "Asian" is a region pill; Thai and Japanese recipes should both match it
    # (mirrors _CUISINE_AREAS, the same mapping the external search uses).
    japanese = {**THAI, "name": "Teriyaki Salmon", "tags": ["Japanese"]}
    out = filter_native_by_cuisine([ITALIAN, THAI, japanese], "Asian")
    assert out == [THAI, japanese]


def test_dietary_filter_excludes_recipes_with_banned_ingredients():
    vegan_ok = {
        "name": "Veggie Stir Fry", "tags": [], "categories": [],
        "recipeIngredient": [{"note": "broccoli"}, {"note": "tofu"}],
    }
    out = filter_native_recipes([ITALIAN, vegan_ok], dietary="Vegan")
    assert out == [vegan_ok]


def test_filter_native_recipes_combines_cuisine_and_diet():
    thai_meat = THAI
    thai_veg = {
        "name": "Thai Veggie Curry", "tags": ["Thai"], "categories": [],
        "recipeIngredient": [{"note": "coconut milk"}, {"note": "tofu"}],
    }
    out = filter_native_recipes(
        [ITALIAN, thai_meat, thai_veg], cuisine="Thai", dietary="Vegetarian")
    assert out == [thai_veg]


def test_no_filters_is_a_no_op():
    out = filter_native_recipes([ITALIAN, THAI, UNTAGGED])
    assert out == [ITALIAN, THAI, UNTAGGED]
