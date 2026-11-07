"""Grocy-backed endpoints that don't belong in the inventory/expiring routers."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from ..services.grocy import GrocyClient, GrocyError

router = APIRouter(prefix="/grocy", tags=["grocy"])


def _client() -> GrocyClient:
    return GrocyClient()


# Shopping list -----------------------------------------------------------

# Restock suggestions ----------------------------------------------------

@router.get("/suggestions")
async def get_suggestions(days: int = 30, min_consumes: int = 2):
    """Return products consumed recently that are now out of stock."""
    g = _client()
    try:
        items = await g.get_restock_suggestions(days=days, min_consumes=min_consumes)
    except GrocyError as e:
        raise HTTPException(502, str(e))
    return {"items": items, "days": days}


@router.get("/runout")
async def get_runout(horizon: int = 7):
    """In-stock products expected to run out soon, from this kitchen's own
    consumption rate (FoodAssistant-i9flm). Empty when the feature is off or
    nothing has been learned yet."""
    from ..config import settings
    from ..services import kitchen_history
    if not settings.use_kitchen_history:
        return {"items": []}
    # It reads a state file, so it runs off the event loop.
    rates = await asyncio.to_thread(kitchen_history.consumption_rates)
    if not rates:
        return {"items": []}
    g = _client()
    try:
        stock_rows = await g._get("/stock")
        products = await g.get_products()
    except GrocyError as e:
        raise HTTPException(502, str(e))
    stock = {}
    for row in stock_rows or []:
        try:
            stock[str(row.get("product_id"))] = float(row.get("amount") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
    names = {str(p.get("id")): p.get("name") or "" for p in products or []}
    items = kitchen_history.forecast_runout(
        rates, stock, names, horizon_days=max(1, min(horizon, 30)))
    return {"items": items}


# Stock journal -----------------------------------------------------------

@router.get("/stock-log")
async def get_stock_log(limit: int = 50):
    g = _client()
    try:
        rows = await g.get_stock_log(limit=min(limit, 200))
    except GrocyError as e:
        raise HTTPException(502, str(e))
    return {"entries": rows}
