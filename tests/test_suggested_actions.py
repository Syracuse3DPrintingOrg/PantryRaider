"""Suggested Custom Buttons: signal ranking, dedupe, and cap (FoodAssistant-1a8h)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import suggested_actions as sa  # noqa: E402


NOW = datetime(2026, 7, 9, tzinfo=timezone.utc)


def _log_row(product_name, transaction_type="purchase", days_ago=1):
    ts = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")
    return {"product_name": product_name, "transaction_type": transaction_type,
            "timestamp": ts}


# -- rank_grocery_purchases --------------------------------------------------

def test_rank_grocery_purchases_requires_the_threshold():
    rows = [_log_row("Milk"), _log_row("Milk"), _log_row("Milk")]
    out = sa.rank_grocery_purchases(rows, now=NOW)
    assert out == [{"product_name": "Milk", "count": 3}]


def test_rank_grocery_purchases_below_threshold_is_excluded():
    rows = [_log_row("Eggs"), _log_row("Eggs")]
    out = sa.rank_grocery_purchases(rows, now=NOW)
    assert out == []


def test_rank_grocery_purchases_ignores_non_purchase_and_stale_rows():
    rows = [
        _log_row("Milk", transaction_type="consume"),
        _log_row("Milk", transaction_type="consume"),
        _log_row("Milk", transaction_type="consume"),
        _log_row("Bread", days_ago=90),  # outside the 30-day window
        _log_row("Bread", days_ago=90),
        _log_row("Bread", days_ago=90),
    ]
    assert sa.rank_grocery_purchases(rows, now=NOW) == []


def test_rank_grocery_purchases_sorts_by_count_then_name():
    rows = (
        [_log_row("Milk")] * 3
        + [_log_row("Eggs")] * 5
        + [_log_row("Bread")] * 3
    )
    out = sa.rank_grocery_purchases(rows, now=NOW)
    assert [r["product_name"] for r in out] == ["Eggs", "Bread", "Milk"]


def test_rank_grocery_purchases_is_case_and_whitespace_insensitive():
    rows = [_log_row("milk"), _log_row(" Milk "), _log_row("MILK")]
    out = sa.rank_grocery_purchases(rows, now=NOW)
    assert len(out) == 1
    assert out[0]["count"] == 3


def test_rank_grocery_purchases_handles_empty_and_malformed_input():
    assert sa.rank_grocery_purchases([], now=NOW) == []
    assert sa.rank_grocery_purchases(None, now=NOW) == []
    assert sa.rank_grocery_purchases([{"nope": True}, "not a dict"], now=NOW) == []


def test_rank_grocery_purchases_custom_window_and_threshold():
    rows = [_log_row("Coffee", days_ago=5)] * 2
    assert sa.rank_grocery_purchases(rows, now=NOW, min_count=2) == \
        [{"product_name": "Coffee", "count": 2}]
    assert sa.rank_grocery_purchases(rows, now=NOW, window_days=2) == []


# -- rank_cook_counts ---------------------------------------------------------

def test_rank_cook_counts_filters_threshold_and_bad_rows():
    rows = [
        {"recipe_key": "mealie:a", "title": "Chicken Soup", "count": 3},
        {"recipe_key": "mealie:b", "title": "Toast", "count": 1},
        {"recipe_key": "", "title": "No key", "count": 5},
        {"recipe_key": "mealie:c", "title": "", "count": 5},
        "not a dict",
    ]
    out = sa.rank_cook_counts(rows, min_count=2)
    assert out == [{"recipe_key": "mealie:a", "title": "Chicken Soup", "count": 3}]


def test_rank_cook_counts_empty_input():
    assert sa.rank_cook_counts([]) == []
    assert sa.rank_cook_counts(None) == []


# -- build_suggestions ---------------------------------------------------------

def _grocery(name, count=3):
    return {"product_name": name, "count": count}


def _cook(title, count=2, key=None):
    return {"recipe_key": key or f"mealie:{title.lower()}", "title": title, "count": count}


def test_build_suggestions_shapes_shopping_and_cook_kinds():
    out = sa.build_suggestions([_grocery("Milk", 4)], [_cook("Chicken Soup", 3)])
    assert len(out) == 2
    shopping = next(s for s in out if s["kind"] == "shopping_add")
    cook = next(s for s in out if s["kind"] == "cook_recipe")
    assert shopping["id"] == "shopping:milk"
    assert shopping["payload"] == {"type": "shopping_add", "item": "Milk", "label": "Milk"}
    assert "4 times" in shopping["reason"]
    assert cook["id"] == "cook:mealie:chicken soup"
    assert cook["payload"] == {"token": "cook"}
    assert cook["label"] == "Cook: Chicken Soup"
    assert "3 times" in cook["reason"]


def test_build_suggestions_only_offers_the_top_cook_signal():
    out = sa.build_suggestions(
        [], [_cook("Chicken Soup", 5), _cook("Tacos", 4)])
    assert len(out) == 1
    assert out[0]["label"] == "Cook: Chicken Soup"


def test_build_suggestions_dedupes_against_existing_shopping_items():
    out = sa.build_suggestions(
        [_grocery("Milk"), _grocery("Eggs")], [],
        existing_shopping_items={"milk"})
    assert [s["label"] for s in out] == ["Eggs"]


def test_build_suggestions_dedupe_is_case_insensitive():
    out = sa.build_suggestions(
        [_grocery("Milk")], [], existing_shopping_items={"MILK"})
    assert out == []


def test_build_suggestions_skips_cook_recipe_when_cook_token_already_placed():
    out = sa.build_suggestions(
        [], [_cook("Chicken Soup")], existing_layout_tokens={"cook"})
    assert out == []


def test_build_suggestions_honors_dismissed_ids():
    out = sa.build_suggestions(
        [_grocery("Milk")], [_cook("Chicken Soup")],
        dismissed_ids={"shopping:milk", "cook:mealie:chicken soup"})
    assert out == []


def test_build_suggestions_caps_at_max_suggestions():
    groceries = [_grocery(f"Item {i}") for i in range(10)]
    out = sa.build_suggestions(groceries, [_cook("Chicken Soup")], max_suggestions=6)
    assert len(out) == 6


def test_build_suggestions_empty_inputs_yield_empty_list():
    assert sa.build_suggestions([], []) == []
    assert sa.build_suggestions(None, None) == []


def test_build_suggestions_preserves_grocery_rank_order():
    out = sa.build_suggestions(
        [_grocery("Eggs", 5), _grocery("Bread", 3)], [])
    assert [s["label"] for s in out] == ["Eggs", "Bread"]
