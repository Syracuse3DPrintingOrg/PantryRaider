"""Grocy-backed endpoints that don't belong in the inventory/expiring routers."""
from __future__ import annotations

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


# Stock journal -----------------------------------------------------------

@router.get("/stock-log")
async def get_stock_log(limit: int = 50):
    g = _client()
    try:
        rows = await g.get_stock_log(limit=min(limit, 200))
    except GrocyError as e:
        raise HTTPException(502, str(e))
    return {"entries": rows}
