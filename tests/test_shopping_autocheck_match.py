"""Stocking an item ticks only the list rows it covers (FoodAssistant-ec0c9)."""
from __future__ import annotations

import pytest

from app.integrations.shopping import stocked_item_covers


@pytest.mark.parametrize("stocked,listed", [
    ("Great Value Whole Milk", "Whole wheat bread"),
    ("Chicken Noodle Soup", "Chicken breast"),
    ("Ground Coffee", "Ground beef"),
    ("Organic Bananas", "Organic eggs"),
])
def test_unrelated_rows_that_share_a_word_stay_unticked(stocked, listed):
    assert not stocked_item_covers(stocked, listed)


@pytest.mark.parametrize("stocked,listed", [
    ("Great Value Whole Milk", "Milk"),
    ("Great Value Whole Milk", "whole milk"),
    ("Organic Bananas", "banana"),
    ("Ground Beef 80/20", "2 lb ground beef"),
])
def test_rows_the_item_covers_are_ticked(stocked, listed):
    assert stocked_item_covers(stocked, listed)


def test_a_row_with_no_significant_words_is_never_ticked():
    assert not stocked_item_covers("Milk", "")
    assert not stocked_item_covers("Milk", "2 x")
