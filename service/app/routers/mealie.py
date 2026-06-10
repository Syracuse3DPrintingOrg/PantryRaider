import html as html_lib
import re
from datetime import date, timedelta

import httpx
from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile
from pydantic import BaseModel

from ..config import settings
from ..services.grocy import GrocyClient
from ..services.mealie import MealieClient, MealieError, classify_recipes
from ..services import recipes_external

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
            "mealie_url": settings.mealie_link_url()}


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
async def search_recipes(search: str = "", per_page: int = Query(50, ge=1, le=200)):
    try:
        items = await _client().search_recipes(search, per_page=per_page)
    except MealieError as e:
        raise HTTPException(502, str(e))
    return [{
        "id": r.get("id"),
        "name": r.get("name"),
        "slug": r.get("slug"),
        "description": (r.get("description") or "")[:160],
        "total_time": r.get("totalTime"),
        "rating": r.get("rating"),
    } for r in items]


def _strip_html(html: str, limit: int = 18000) -> str:
    """Reduce a page to readable text for LLM recipe extraction."""
    text = re.sub(r"(?is)<(script|style|nav|header|footer|svg|noscript|form)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()[:limit]


class ImportUrlPayload(BaseModel):
    url: str


@router.post("/recipes/import-url")
async def import_recipe_url(payload: ImportUrlPayload):
    """Import a recipe from a webpage.

    Tries Mealie's built-in scraper first (handles most recipe sites via
    structured data). If that fails, fetches the page and has the LLM
    extract a draft for the user to review before saving.
    """
    m = _client()
    url = payload.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Enter a full URL starting with http:// or https://")

    try:
        slug = await m.create_recipe_from_url(url)
        if slug:
            return {"ok": True, "saved": True, "slug": slug,
                    "mealie_url": settings.mealie_link_url(),
                    "message": "Imported via Mealie's scraper."}
    except Exception:
        pass  # fall through to LLM extraction

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (FoodAssistant)"})
            r.raise_for_status()
            page_text = _strip_html(r.text)
    except Exception as e:
        raise HTTPException(502, f"Could not fetch the page: {e}")
    if len(page_text) < 200:
        raise HTTPException(422, "The page had no readable text to extract a recipe from.")

    from ..dependencies import get_enrich_provider
    try:
        recipe = await get_enrich_provider().extract_recipe(page_text=page_text)
    except Exception as e:
        raise HTTPException(502, f"LLM extraction failed: {e}")
    if not recipe or not recipe.get("name"):
        raise HTTPException(422, "Could not find a recipe on that page.")
    return {"ok": True, "saved": False, "recipe": recipe,
            "message": "Mealie's scraper couldn't read this site — review the AI extraction below, then save."}


@router.post("/recipes/extract-photo")
async def extract_recipe_photo(file: UploadFile = File(...)):
    """Vision-LLM extraction of a photographed recipe (card, cookbook page,
    handwritten note). Returns a draft for review — nothing is saved yet."""
    _client()  # 400 early if Mealie isn't configured — there'd be nowhere to save
    image_data = await file.read()
    if not image_data:
        raise HTTPException(400, "Empty upload.")

    from ..dependencies import get_vision_provider
    try:
        recipe = await get_vision_provider().extract_recipe(
            image_data=image_data, mime_type=file.content_type or "image/jpeg")
    except Exception as e:
        raise HTTPException(502, f"Vision extraction failed: {e}")
    if not recipe or not recipe.get("name"):
        raise HTTPException(422, "Could not read a recipe from that photo — try a clearer shot.")
    return {"ok": True, "recipe": recipe}


class CreateRecipePayload(BaseModel):
    name: str
    description: str = ""
    servings: str = ""
    total_time: str = ""
    ingredients: list[str] = []
    instructions: list[str] = []


@router.post("/recipes/create")
async def create_recipe(payload: CreateRecipePayload):
    if not payload.name.strip():
        raise HTTPException(400, "Recipe name is required.")
    try:
        slug = await _client().create_recipe(payload.model_dump())
    except MealieError as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "slug": slug, "mealie_url": settings.mealie_link_url()}


@router.get("/suggest")
async def suggest(top: int = Query(8, ge=1, le=20), external: bool = True):
    """Recipes sorted into three cookability tiers against current inventory:
    ready (stock only), staples (stock + pantry basics), shopping (uses
    perishable stock but needs extra ingredients). Candidates come from
    Mealie plus TheMealDB when external=true."""
    m = _client()
    try:
        recipes = await m.get_recipes_with_ingredients()
    except MealieError as e:
        raise HTTPException(502, str(e))
    try:
        stock = await GrocyClient().get_full_stock()
    except Exception:
        stock = []

    external_count = 0
    if external and stock:
        # Search TheMealDB by stock item names, perishables first so the
        # results lean toward using up what's expiring.
        ordered = sorted(stock, key=lambda s: (s.get("days_remaining") is None,
                                               s.get("days_remaining") or 999))
        try:
            ext = await recipes_external.find_recipes_for_ingredients(
                [s["name"] for s in ordered])
        except Exception:
            ext = []
        mealie_names = {(r.get("name") or "").lower() for r in recipes}
        ext = [r for r in ext if (r.get("name") or "").lower() not in mealie_names]
        external_count = len(ext)
        recipes = recipes + ext

    return {
        "tiers": classify_recipes(recipes, stock, top_per_tier=top),
        "recipes_considered": len(recipes),
        "external_considered": external_count,
        "inventory_items": len(stock),
        "mealie_url": settings.mealie_link_url(),
    }


class ImportExternalPayload(BaseModel):
    external_id: str
    add_missing_to_list: bool = False
    list_id: str = ""


@router.post("/recipes/import-external")
async def import_external_recipe(payload: ImportExternalPayload):
    """Save a TheMealDB recipe into Mealie; optionally also send its
    missing ingredients to the shopping list in the same click."""
    m = _client()
    recipe = await recipes_external.get_external_recipe(payload.external_id)
    if not recipe:
        raise HTTPException(404, "Recipe not found in TheMealDB.")
    try:
        slug = await m.create_recipe(recipe)
    except MealieError as e:
        raise HTTPException(502, str(e))

    result = {"ok": True, "slug": slug, "name": recipe["name"],
              "mealie_url": settings.mealie_link_url(),
              "message": f"\"{recipe['name']}\" saved to Mealie."}
    if payload.add_missing_to_list:
        listing = await add_missing_ingredients(
            AddMissingPayload(slug=slug, list_id=payload.list_id))
        result["added_to_list"] = listing.get("added", 0)
        result["message"] += f" {listing.get('message', '')}"
    return result


class AddMissingPayload(BaseModel):
    slug: str
    list_id: str = ""   # empty = use first available list


@router.post("/suggest/add-missing")
async def add_missing_ingredients(payload: AddMissingPayload):
    """Add unmatched ingredients of a recipe to the Mealie shopping list.

    Re-runs the match logic against the current inventory so the list
    reflects what you actually have right now, not a cached snapshot.
    """
    m = _client()
    try:
        recipe = await m.get_recipe(payload.slug)
    except MealieError as e:
        raise HTTPException(502, str(e))

    try:
        stock = await GrocyClient().get_full_stock()
    except Exception:
        stock = []

    # Replicate the matching logic from suggest_recipes for this one recipe
    from ..services.mealie import _tokens, _ingredient_text
    inv_tokens = [{"tokens": _tokens(s["name"])} for s in stock if _tokens(s["name"])]

    missing = []
    for ing in recipe.get("recipeIngredient") or []:
        text = _ingredient_text(ing).strip()
        if not text:
            continue
        ing_toks = _tokens(text)
        if not ing_toks:
            continue
        already_have = any(ing_toks & s["tokens"] for s in inv_tokens)
        if not already_have:
            missing.append(text)

    if not missing:
        return {"ok": True, "added": 0, "message": "You already have all ingredients."}

    # Resolve shopping list
    try:
        lists = await m.get_shopping_lists()
    except MealieError as e:
        raise HTTPException(502, str(e))
    if not lists:
        raise HTTPException(400, "No shopping lists found in Mealie — create one first.")

    target = next((l for l in lists if l.get("id") == payload.list_id), lists[0])
    list_id = target["id"]

    import asyncio
    results = await asyncio.gather(
        *(m.add_shopping_item(list_id, item) for item in missing),
        return_exceptions=True,
    )
    added = sum(1 for r in results if not isinstance(r, Exception))
    return {
        "ok": True,
        "added": added,
        "list_name": target.get("name", ""),
        "items": missing,
        "message": f"Added {added} item{'s' if added != 1 else ''} to \"{target.get('name', 'Shopping List')}\".",
    }


# ── Shopping lists ───────────────────────────────────────────────────────────

@router.get("/shopping/summary")
async def shopping_summary():
    """Lean unchecked-items view for Home Assistant REST sensors."""
    if not settings.mealie_configured():
        return {"count": 0, "items": [], "list_name": ""}
    m = MealieClient()
    try:
        lists = await m.get_shopping_lists()
        if not lists:
            return {"count": 0, "items": [], "list_name": ""}
        detail = await m.get_shopping_list(lists[0]["id"])
    except Exception:
        return {"count": 0, "items": [], "list_name": "", "error": "unreachable"}

    unchecked = [i for i in detail.get("listItems") or [] if not i.get("checked")]
    names = []
    for i in unchecked[:40]:
        label = i.get("display") or i.get("note") or (i.get("food") or {}).get("name") or ""
        if label.strip():
            names.append(label.strip())
    return {"count": len(unchecked), "items": names,
            "list_name": lists[0].get("name", "Shopping List")}


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
