"""Receipt price capture (FoodAssistant-5osx).

Photograph a grocery receipt and turn it into real purchase prices on Grocy
stock entries. This module is the pure half of the flow: the prompt the vision
provider runs, the tolerant parser for the model's reply, and the matcher that
pairs extracted line items with Grocy products. routers/receipt.py does the
I/O (photo upload, Grocy writes) on top of these.

Everything here is deliberately import-light and side-effect free so the whole
flow is unit-testable with canned model replies and canned stock lists.

The store name read off the receipt rides along as informational text only.
Grocy can scope price history per shopping_location, but v1 does not create or
link shopping_locations: the price lands on the stock entry and the store is
just shown to the user during review.
"""
from __future__ import annotations

import difflib
import re

from ..providers.base import parse_json_response

# A line item pairs with a product only when the similarity score reaches this
# bar. Pinned by tests: below it a proposal is worse than no proposal, since a
# wrong pairing writes a real price onto the wrong product's history.
MATCH_THRESHOLD = 0.6

# Two tokens count as the same word at this SequenceMatcher ratio, which lets
# a receipt's clipped spellings ("mlk", "chkn") land on the real word without
# letting "soup" drift into "breast".
_TOKEN_RATIO = 0.8

# Filler words that say nothing about what the product is. Kept short on
# purpose; the same idea as the recipe matcher's stop words in
# services/mealie.py, trimmed to what shows up on receipts and product names.
_STOP_WORDS = {
    "the", "and", "with", "for", "fresh", "large", "small", "medium",
    "pack", "count", "each", "size", "family",
}

_PRICES_PROMPT = """
Read this grocery receipt image. Extract the store name and every purchased
product line with the price that was actually paid.
Return a JSON object with these exact fields:
{
  "store": "store name printed on the receipt, or null if not visible",
  "items": [
    {
      "name": "plain product name, e.g. Whole Milk, Chicken Breast",
      "price": 3.49,
      "quantity": 1
    }
  ]
}
Rules:
- Expand receipt abbreviations into plain product names ("GV WHL MLK GAL"
  becomes "Whole Milk"). Do not include the store's own brand prefix.
- "price" is the final price paid for ONE unit, as a number, after any coupon
  or discount on that line. When the line shows a total for several units,
  divide by the quantity. Use null when no price is legible for the line.
- "quantity" is how many units that line covers; use 1 when not stated.
- Include every purchased product line. Skip totals, subtotals, tax, deposits,
  fees, and coupon-only lines.
Return ONLY valid JSON. No markdown, no explanation.
""".strip()


def build_prices_prompt() -> str:
    """The receipt price-capture prompt every provider runs. Pure."""
    return _PRICES_PROMPT


def _norm(name) -> str:
    """Lowercased, squeezed text key for comparing names."""
    return re.sub(r"\s+", " ", str(name or "").strip().lower())


def _tokens(text) -> set[str]:
    """Meaningful word tokens of a name, singularized, stop words dropped."""
    words = re.findall(r"[a-z]+", _norm(text))
    return {w.rstrip("s") for w in words if len(w) >= 3 and w not in _STOP_WORDS}


def _token_hits(token: str, others: set[str]) -> bool:
    return any(
        token == other
        or difflib.SequenceMatcher(None, token, other).ratio() >= _TOKEN_RATIO
        for other in others
    )


def similarity(a, b) -> float:
    """How alike two product names are, 0.0 to 1.0.

    Token-level fuzzy Dice: each side's tokens are checked for a close match on
    the other side, so "Whole Mlk" still lands on "Whole Milk" while "Chicken
    Soup" stays away from "Chicken Breast". Names too short to tokenize fall
    back to a plain character ratio.
    """
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return difflib.SequenceMatcher(None, na, nb).ratio()
    matched_a = sum(1 for t in ta if _token_hits(t, tb))
    matched_b = sum(1 for t in tb if _token_hits(t, ta))
    return (matched_a + matched_b) / (len(ta) + len(tb))


def _to_price(value) -> float | None:
    """Coerce a model's price into a positive float, or None.

    Tolerates numbers and price-looking strings ("$3.49", "3,49"). Anything
    that is not a real positive amount becomes None: a zero or negative price
    is never kept, so no code path downstream can stamp Grocy's price history
    with the 0.00 that corrupts inventory value.
    """
    if isinstance(value, str):
        value = value.strip().replace("$", "").replace(",", ".")
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return round(price, 2) if price > 0 else None


def _to_quantity(value) -> float:
    try:
        qty = float(value)
    except (TypeError, ValueError):
        return 1.0
    return qty if qty > 0 else 1.0


def parse_receipt_reply(raw: str) -> dict:
    """Parse a model's receipt-prices reply into {"store", "items"}.

    Tolerates markdown code fences (parse_json_response), the object form the
    prompt asks for, and a bare item list from a model that drops the wrapper.
    Rows without a usable name are skipped; a row with no legible price is
    kept with price None so the user still sees the line during review. Raises
    ValueError when the reply is not JSON at all, so the caller can answer
    with an honest error instead of an empty result.
    """
    data = parse_json_response(raw)
    store = None
    if isinstance(data, dict):
        raw_store = data.get("store")
        store = str(raw_store).strip() if raw_store else None
        rows = data.get("items")
        if not isinstance(rows, list):
            # json_object mode sometimes renames the array; take the first
            # list value rather than losing the whole receipt.
            rows = next((v for v in data.values() if isinstance(v, list)), [])
    elif isinstance(data, list):
        rows = data
    else:
        rows = []

    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        price = _to_price(row.get("price", row.get("unit_price")))
        items.append({
            "name": name,
            "price": price,
            "quantity": _to_quantity(row.get("quantity")),
        })
    return {"store": store, "items": items}


def _product_candidates(stock: list[dict]) -> list[tuple[int, str]]:
    """(product_id, name) pairs from a Grocy stock or product list.

    Accepts raw /stock rows ({"product_id", "product": {"name"}}) and plain
    product rows ({"id", "name"}), de-duplicated by product id.
    """
    seen: dict[int, str] = {}
    for row in stock or []:
        if not isinstance(row, dict):
            continue
        product = row.get("product") if isinstance(row.get("product"), dict) else {}
        name = str(product.get("name") or row.get("name") or "").strip()
        try:
            pid = int(row.get("product_id") or row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if pid and name and pid not in seen:
            seen[pid] = name
    return list(seen.items())


def match_line_items(items: list[dict], stock: list[dict]) -> list[dict]:
    """Pair extracted line items with Grocy products by name similarity.

    Every input item comes back (the review list shows unmatched lines too);
    only a similarity at or above MATCH_THRESHOLD proposes a product, and the
    best-scoring product wins. product_id and product_name are None for a line
    with no confident match. The actual price write happens later, against the
    matched product's newest stock entry, after the user confirms.
    """
    candidates = _product_candidates(stock)
    results = []
    for item in items or []:
        best_pid, best_name, best_score = None, None, 0.0
        for pid, name in candidates:
            score = similarity(item.get("name"), name)
            if score > best_score:
                best_pid, best_name, best_score = pid, name, score
        matched = best_score >= MATCH_THRESHOLD
        results.append({
            "name": item.get("name"),
            "price": item.get("price"),
            "quantity": item.get("quantity", 1),
            "product_id": best_pid if matched else None,
            "product_name": best_name if matched else None,
            "score": round(best_score, 3),
        })
    return results


def newest_entry(entries) -> dict | None:
    """The newest stock entry that can carry a price, or None.

    The shopping trip that produced the receipt is the most recent add, so its
    entry is the one created last: highest row_created_timestamp, ties broken
    by the higher numeric row id (the same rule the label printer uses to code
    a just-added item). Entries with no remaining stock or no numeric id are
    skipped; GrocyClient.set_entry_price needs the id to write.
    """
    best_key, best = None, None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        try:
            row_id = int(entry.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if not row_id:
            continue
        try:
            amount = float(entry.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if amount <= 0:
            continue
        key = (str(entry.get("row_created_timestamp") or ""), row_id)
        if best_key is None or key > best_key:
            best_key, best = key, entry
    return best
