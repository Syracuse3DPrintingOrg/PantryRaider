"""Waste insights (FoodAssistant-64eg): what got tossed instead of eaten.

The Expiring page's Toss it action records a consume with Grocy's spoiled
flag, and Grocy keeps that flag in its stock log. This module turns that log
into a small per-product waste picture: how often a product was tossed, how
much, and what share of everything consumed of that product went in the bin.

The aggregation is pure so it can be pinned byte-exact in tests; the loader
wraps the one raw Grocy fetch the page needs and degrades to an empty list on
any trouble, because the Expiring page must render even when the log does not.
"""
from __future__ import annotations


def _truthy(value) -> bool:
    """Grocy booleans arrive as 0/1 ints, "0"/"1" strings, or real bools."""
    if isinstance(value, str):
        return value.strip() not in ("", "0", "false", "False")
    return bool(value)


def waste_summary(log_rows: list[dict], products: dict[str, str]) -> list[dict]:
    """Aggregate spoiled consume transactions per product name. Pure.

    ``log_rows`` is a raw Grocy /objects/stock_log list (consume rows log
    negative amounts, so absolute values are summed) and ``products`` maps
    str(product id) -> name. Only real consume transactions count, and rows
    Grocy marked undone are skipped: an undone toss never happened. For each
    product with at least one spoiled consume, the result carries how many
    times it was tossed, how much, how much was consumed in total (eaten and
    tossed together), and the tossed share of that total, rounded to four
    places. Sorted most-wasted first (by amount, then name) so callers can
    slice a top-N straight off.
    """
    spoiled_count: dict[str, int] = {}
    spoiled_amount: dict[str, float] = {}
    total_amount: dict[str, float] = {}
    for row in log_rows or []:
        if (row.get("transaction_type") or "") != "consume":
            continue
        if _truthy(row.get("undone")):
            continue
        pid = str(row.get("product_id") or "")
        if not pid:
            continue
        try:
            amount = abs(float(row.get("amount") or 0))
        except (TypeError, ValueError):
            continue
        total_amount[pid] = total_amount.get(pid, 0.0) + amount
        if _truthy(row.get("spoiled")):
            spoiled_count[pid] = spoiled_count.get(pid, 0) + 1
            spoiled_amount[pid] = spoiled_amount.get(pid, 0.0) + amount

    out = []
    for pid, count in spoiled_count.items():
        tossed = spoiled_amount[pid]
        total = total_amount.get(pid, 0.0)
        out.append({
            "name": products.get(pid, f"Product {pid}"),
            "times_tossed": count,
            "amount_tossed": round(tossed, 2),
            "amount_consumed_total": round(total, 2),
            "share": round(tossed / total, 4) if total else 1.0,
        })
    out.sort(key=lambda w: (-w["amount_tossed"], w["name"].lower()))
    return out


async def load_waste(grocy, limit: int = 500, top: int = 5) -> list[dict]:
    """The top wasted products for the Expiring page, or [] when unknowable.

    Reads the raw stock log rather than GrocyClient.get_stock_log() because
    the enriched journal rows drop the ``spoiled`` flag this whole feature
    hangs on. Any failure (Grocy down, odd payload) returns an empty list so
    the page still renders; the template's empty state stays honest because a
    healthy pantry with no recorded spoilage looks exactly the same.
    """
    try:
        rows = await grocy._get(
            f"/objects/stock_log?order=row_created_timestamp%3Adesc&limit={limit}"
        )
        products = {str(p["id"]): p["name"] for p in await grocy.get_products()}
    except Exception:  # noqa: BLE001
        return []
    return waste_summary(rows, products)[:top]
