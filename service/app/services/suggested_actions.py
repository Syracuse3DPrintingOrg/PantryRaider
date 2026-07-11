"""Suggested Custom Buttons (FoodAssistant-1a8h).

Pure assembly of quick-action button suggestions for the Start Page and Stream
Deck editors, learned from what the user already does: groceries they buy
often (Grocy purchase history) and recipes they cook often (the cook-count
tally in cook_counts.py). No new tracking is introduced here; this module just
reads the existing signals, ranks them, and turns the winners into the same
prefilled-action shape each editor's "add a button" path already understands.

Thresholds: a grocery item needs 3+ purchases in the lookback window; a recipe
needs 2+ cooks. A suggestion already matching a configured button, or one the
user dismissed, is left out. The list is capped so the "Suggested" section
never overwhelms the palette.

Everything here is a pure function over plain dicts/lists so it is unit
testable without Grocy, Mealie, or a database; the router (services/ui.py)
does the I/O and hands the results in.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# Grocy stock_log transaction type that records a purchase (see
# services/grocy.get_stock_log / get_restock_suggestions, which use the same
# transaction-type strings for the consume side of this same log).
_PURCHASE_TYPE = "purchase"

MAX_SUGGESTIONS = 6


def _norm(text) -> str:
    """Lowercase, collapsed-whitespace key for case/spacing-insensitive matches."""
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def rank_grocery_purchases(stock_log_rows, *, window_days: int = 30,
                            min_count: int = 3, now: datetime | None = None) -> list[dict]:
    """Products purchased ``min_count``+ times in the last ``window_days`` days.

    ``stock_log_rows`` is the list shape returned by GrocyClient.get_stock_log:
    dicts with ``product_name``, ``transaction_type``, and a ``timestamp``
    string (``YYYY-MM-DDTHH:MM:SS``, sortable as text). Rows with an
    unparseable or missing timestamp are skipped rather than assumed recent.
    Returns ``[{"product_name", "count"}]`` sorted most-purchased first, ties
    broken alphabetically for a stable order. Pure and total: a malformed or
    empty input yields ``[]``."""
    if not isinstance(stock_log_rows, (list, tuple)):
        return []
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%S")
    display: dict[str, str] = {}
    tally: dict[str, int] = {}
    for row in stock_log_rows:
        if not isinstance(row, dict):
            continue
        if row.get("transaction_type") != _PURCHASE_TYPE:
            continue
        ts = str(row.get("timestamp") or "")
        if not ts or ts < cutoff:
            continue
        name = str(row.get("product_name") or "").strip()
        if not name:
            continue
        key = _norm(name)
        display.setdefault(key, name)
        tally[key] = tally.get(key, 0) + 1
    out = [{"product_name": display[k], "count": c}
           for k, c in tally.items() if c >= min_count]
    out.sort(key=lambda r: (-r["count"], r["product_name"].lower()))
    return out


def rank_cook_counts(cook_rows, *, min_count: int = 2) -> list[dict]:
    """Filter/normalize cook_counts.top_counts() rows to the threshold.

    ``cook_rows`` is the shape returned by cook_counts.top_counts:
    ``[{"recipe_key", "title", "count", "source"}]``. Returns the rows meeting
    ``min_count``, already-sorted input order preserved (top_counts sorts by
    count descending). Pure and total."""
    if not isinstance(cook_rows, (list, tuple)):
        return []
    out = []
    for row in cook_rows:
        if not isinstance(row, dict):
            continue
        try:
            count = int(row.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count < min_count:
            continue
        title = str(row.get("title") or "").strip()
        key = str(row.get("recipe_key") or "").strip()
        if not title or not key:
            continue
        out.append({"recipe_key": key, "title": title, "count": count})
    return out


def _shopping_suggestion(item: dict, window_days: int) -> dict:
    name = item["product_name"]
    count = item["count"]
    return {
        "id": f"shopping:{_norm(name)}",
        "kind": "shopping_add",
        "label": name,
        "reason": f"Bought {count} times in the last {window_days} days.",
        "payload": {"type": "shopping_add", "item": name, "label": name},
    }


def _cook_suggestion(recipe: dict) -> dict:
    title = recipe["title"]
    count = recipe["count"]
    times = "time" if count == 1 else "times"
    return {
        "id": f"cook:{recipe['recipe_key']}",
        "kind": "cook_recipe",
        "label": f"Cook: {title}",
        "reason": f"Cooked {count} {times}.",
        # No per-recipe deck action exists (only the built-in Cook page
        # shortcut), so the prefilled action is that built-in token, same as
        # dragging the "Cook" palette chip onto a key.
        "payload": {"token": "cook"},
    }


def build_suggestions(
    grocery_signals: list,
    cook_signals: list,
    *,
    window_days: int = 30,
    existing_shopping_items=(),
    existing_layout_tokens=(),
    dismissed_ids=(),
    max_suggestions: int = MAX_SUGGESTIONS,
) -> list[dict]:
    """Assemble the ranked, deduped, capped suggestion list.

    ``grocery_signals`` / ``cook_signals`` are the already-thresholded outputs
    of rank_grocery_purchases / rank_cook_counts (ranked, highest first).
    ``existing_shopping_items`` / ``existing_layout_tokens`` describe what the
    user already has configured (see build_dedupe_sets below for how the
    router assembles them from settings); a suggestion matching either is
    skipped. ``dismissed_ids`` are suggestion ids the user has X'd out. Pure,
    total, and order-stable: shopping suggestions first (most-purchased
    first), then at most one cook suggestion (the most-cooked recipe; any
    additional cooked-often recipes would just be the same "open Cook" button,
    so only the top one is offered), capped overall at max_suggestions."""
    existing_items = {_norm(i) for i in existing_shopping_items if i}
    existing_tokens = {str(t) for t in existing_layout_tokens if t}
    dismissed = {str(d) for d in dismissed_ids if d}

    out: list[dict] = []
    for item in grocery_signals or []:
        if not isinstance(item, dict) or not item.get("product_name"):
            continue
        sug = _shopping_suggestion(item, window_days)
        if sug["id"] in dismissed:
            continue
        if _norm(item["product_name"]) in existing_items:
            continue
        out.append(sug)
        if len(out) >= max_suggestions:
            return out

    # Only the single most-cooked recipe: every cook_recipe suggestion resolves
    # to the same built-in "cook" action, so offering more than one would just
    # be duplicate buttons with different labels.
    if "cook" not in existing_tokens:
        for recipe in cook_signals or []:
            if not isinstance(recipe, dict) or not recipe.get("title"):
                continue
            sug = _cook_suggestion(recipe)
            if sug["id"] in dismissed:
                continue
            out.append(sug)
            break

    return out[:max_suggestions]
