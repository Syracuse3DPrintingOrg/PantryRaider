"""Grocy-backed endpoints that don't belong in the inventory/expiring routers."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from ..services.grocy import GrocyClient, GrocyError

router = APIRouter(prefix="/grocy", tags=["grocy"])


def _client() -> GrocyClient:
    return GrocyClient()


# Shopping list -----------------------------------------------------------

@router.get("/shopping")
async def get_shopping():
    """Return the default shopping list with its items."""
    g = _client()
    try:
        list_id = await g.ensure_shopping_list()
        lists = await g.get_shopping_lists()
        items = await g.get_shopping_items(list_id)
    except GrocyError as e:
        raise HTTPException(502, str(e))
    return {
        "list_id": list_id,
        "lists": [{"id": int(l["id"]), "name": l["name"]} for l in lists],
        "items": [
            {
                "id": int(i["id"]),
                "note": i.get("note") or "",
                "product_name": i.get("product_name") or "",
                "amount": float(i.get("amount") or 1),
                "done": bool(int(i.get("done") or 0)),
            }
            for i in items
        ],
    }


@router.get("/shopping/{list_id}")
async def get_shopping_list(list_id: int):
    """Return a specific shopping list with its items."""
    g = _client()
    try:
        lists = await g.get_shopping_lists()
        items = await g.get_shopping_items(list_id)
    except GrocyError as e:
        raise HTTPException(502, str(e))
    return {
        "list_id": list_id,
        "lists": [{"id": int(l["id"]), "name": l["name"]} for l in lists],
        "items": [
            {
                "id": int(i["id"]),
                "note": i.get("note") or "",
                "product_name": i.get("product_name") or "",
                "amount": float(i.get("amount") or 1),
                "done": bool(int(i.get("done") or 0)),
            }
            for i in items
        ],
    }


class ShoppingItemPayload(BaseModel):
    list_id: int | None = None
    note: str
    amount: float = 1.0


@router.post("/shopping/items")
async def add_shopping_item(payload: ShoppingItemPayload):
    g = _client()
    try:
        list_id = payload.list_id or await g.ensure_shopping_list()
        await g.add_shopping_item(list_id, payload.note.strip(), payload.amount)
    except GrocyError as e:
        raise HTTPException(502, str(e))
    return {"ok": True}


@router.put("/shopping/items/{item_id}")
async def toggle_shopping_item(item_id: int, body: dict = Body(...)):
    g = _client()
    try:
        await g.toggle_shopping_item(item_id, bool(body.get("done", False)))
    except GrocyError as e:
        raise HTTPException(502, str(e))
    return {"ok": True}


@router.delete("/shopping/items/{item_id}")
async def delete_shopping_item(item_id: int):
    g = _client()
    try:
        await g.delete_shopping_item(item_id)
    except GrocyError as e:
        raise HTTPException(502, str(e))
    return {"ok": True}


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


@router.post("/shopping/clear-done")
async def clear_done_items(body: dict = Body(...)):
    g = _client()
    try:
        list_id = int(body.get("list_id") or await g.ensure_shopping_list())
        removed = await g.clear_done_shopping_items(list_id)
    except GrocyError as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "removed": removed}
