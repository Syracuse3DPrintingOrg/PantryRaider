from datetime import date, timedelta

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel

from ..config import settings
from ..services.grocy import GrocyClient
from ..services.mealie import MealieClient, MealieError, suggest_recipes

router = APIRouter(prefix="/mealie", tags=["mealie"])


def _client() -> MealieClient:
    if not settings.mealie_configured():
        raise HTTPException(400, "Mealie is not configured — add its URL and API token in /setup.")
    return MealieClient()


@router.get("/status")
async def status():
    if not settings.mealie_configured():
        return {"configured": False, "ok": False}
    ok = await MealieClient().health_check()
    return {"configured": True, "ok": ok, "base_url": settings.mealie_base_url}


# ── Meal plan ────────────────────────────────────────────────────────────────

@router.get("/mealplan")
async def get_mealplan(days: int = Query(7, ge=1, le=31)):
    m = _client()
    start = date.today()
    end = start + timedelta(days=days - 1)
    try:
        entries = await m.get_mealplan(start.isoformat(), end.isoformat())
    except MealieError as e:
        raise HTTPException(502, str(e))

    by_date: dict[str, list] = {}
    d = start
    while d <= end:
        by_date[d.isoformat()] = []
        d += timedelta(days=1)
    for e in entries:
        by_date.setdefault(e.get("date", ""), []).append({
            "id": e.get("id"),
            "entry_type": e.get("entryType"),
            "title": e.get("title") or (e.get("recipe") or {}).get("name") or "",
            "recipe_slug": (e.get("recipe") or {}).get("slug"),
        })
    return {"start": start.isoformat(), "end": end.isoformat(), "days": by_date,
            "mealie_url": settings.mealie_base_url.rstrip("/")}


class MealplanEntryPayload(BaseModel):
    date: str
    entry_type: str = "dinner"   # breakfast | lunch | dinner | side
    recipe_id: str | None = None
    title: str = ""


@router.post("/mealplan")
async def add_mealplan_entry(payload: MealplanEntryPayload):
    m = _client()
    if not payload.recipe_id and not payload.title:
        raise HTTPException(400, "Provide a recipe or a free-text title.")
    try:
        entry = await m.add_mealplan_entry(
            payload.date, payload.entry_type,
            recipe_id=payload.recipe_id, title=payload.title,
        )
    except MealieError as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "id": entry.get("id")}


@router.delete("/mealplan/{entry_id}")
async def delete_mealplan_entry(entry_id: int):
    try:
        await _client().delete_mealplan_entry(entry_id)
    except MealieError as e:
        raise HTTPException(502, str(e))
    return {"ok": True}


# ── Recipes ──────────────────────────────────────────────────────────────────

@router.get("/recipes")
async def search_recipes(search: str = ""):
    try:
        items = await _client().search_recipes(search)
    except MealieError as e:
        raise HTTPException(502, str(e))
    return [{"id": r.get("id"), "name": r.get("name"), "slug": r.get("slug")}
            for r in items]


@router.get("/suggest")
async def suggest(top: int = Query(10, ge=1, le=30)):
    """Recipes ranked by current inventory coverage, boosted when they use
    items that expire within 5 days."""
    m = _client()
    try:
        recipes = await m.get_recipes_with_ingredients()
    except MealieError as e:
        raise HTTPException(502, str(e))
    try:
        stock = await GrocyClient().get_full_stock()
    except Exception:
        stock = []
    return {
        "suggestions": suggest_recipes(recipes, stock, top=top),
        "recipes_considered": len(recipes),
        "inventory_items": len(stock),
        "mealie_url": settings.mealie_base_url.rstrip("/"),
    }


# ── Shopping lists ───────────────────────────────────────────────────────────

@router.get("/shopping")
async def get_shopping(list_id: str = ""):
    m = _client()
    try:
        lists = await m.get_shopping_lists()
        if not lists:
            return {"lists": [], "list": None, "items": []}
        selected = next((l for l in lists if l.get("id") == list_id), lists[0])
        detail = await m.get_shopping_list(selected["id"])
    except MealieError as e:
        raise HTTPException(502, str(e))

    items = detail.get("listItems") or []
    items.sort(key=lambda i: (bool(i.get("checked")), (i.get("note") or "").lower()))
    return {
        "lists": [{"id": l.get("id"), "name": l.get("name")} for l in lists],
        "list": {"id": selected.get("id"), "name": selected.get("name")},
        "items": items,
    }


class ShoppingItemPayload(BaseModel):
    list_id: str
    note: str
    quantity: float = 1.0


@router.post("/shopping/items")
async def add_shopping_item(payload: ShoppingItemPayload):
    if not payload.note.strip():
        raise HTTPException(400, "Item text is required.")
    try:
        item = await _client().add_shopping_item(
            payload.list_id, payload.note.strip(), payload.quantity)
    except MealieError as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "id": item.get("id")}


@router.put("/shopping/items/{item_id}")
async def update_shopping_item(item_id: str, item: dict = Body(...)):
    """Forward a full item update to Mealie (used to toggle `checked`)."""
    try:
        await _client().update_shopping_item(item_id, item)
    except MealieError as e:
        raise HTTPException(502, str(e))
    return {"ok": True}


@router.delete("/shopping/items/{item_id}")
async def delete_shopping_item(item_id: str):
    try:
        await _client().delete_shopping_item(item_id)
    except MealieError as e:
        raise HTTPException(502, str(e))
    return {"ok": True}
