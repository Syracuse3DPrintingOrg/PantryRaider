"""Normalization of branded/sized Grocy stock names into the canonical
ingredient terms external recipe catalogs (TheMealDB filter.php) expect.

TheMealDB's filter.php only matches its single-ingredient taxonomy, so
"Baby Spinach" must reduce to "spinach" before querying.
"""
import pytest

from app.services.recipes_external import _core_ingredient


@pytest.mark.parametrize("raw, expected", [
    ("Baby Spinach", "spinach"),
    ("Organic Whole Milk", "milk"),
    ("Chicken Breast 1lb", "chicken breast"),
    ("Boneless Skinless Chicken Thighs", "chicken thighs"),
    # already-clean names pass through unchanged
    ("spinach", "spinach"),
    ("chicken breast", "chicken breast"),
])
def test_core_ingredient(raw, expected):
    assert _core_ingredient(raw) == expected


def test_strips_embedded_quantities_and_units():
    assert _core_ingredient("500g Ground Beef") == "beef"
    assert _core_ingredient("2 x 400ml Coconut Cream") == "coconut cream"


def test_empty_and_pure_noise():
    assert _core_ingredient("") == ""
    assert _core_ingredient("Large Organic") == ""
