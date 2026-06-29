"""REST for the active recipe and the shared timer registry (main server).

Both features are server-side foundation for the Current Recipe epic: the active
recipe and the timers live in process memory on the main server so the future
web UI tab, Stream Deck, and satellites consume the same state. These routes sit
behind the app's normal require_auth middleware, so they need no extra auth.

Two APIRouters share this module: one under /current-recipe, one under /timers.
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..services import action_items, current_recipe, recipe_timers, timers

recipe_router = APIRouter(prefix="/current-recipe", tags=["current-recipe"])
timers_router = APIRouter(prefix="/timers", tags=["timers"])


class IngredientIn(BaseModel):
    name: str
    quantity: float | None = None
    unit: str | None = None


class RecipeIn(BaseModel):
    title: str = ""
    source: str = ""
    id: str | None = None
    servings: int = 1
    servings_scale: float = 1.0
    ingredients: list[IngredientIn] = []
    steps: list[str] = []
    notes: str = ""


class ScaleIn(BaseModel):
    factor: float


class TimerIn(BaseModel):
    label: str = ""
    seconds: float


class StartSuggestionIn(BaseModel):
    """Start a real timer from a suggestion. Identify the suggestion by
    step_index OR label; seconds is optional and, when omitted, is filled from
    the matching suggestion so a surface can fire one without re-deriving it."""
    step_index: int | None = None
    label: str | None = None
    seconds: float | None = None


# --- Active recipe -------------------------------------------------------


@recipe_router.get("")
def get_current_recipe():
    """Return the active recipe, or {"recipe": null} when none is loaded."""
    return {"recipe": current_recipe.get_active()}


@recipe_router.post("")
def set_current_recipe(payload: RecipeIn):
    """Replace the active recipe and return the normalized form."""
    recipe = current_recipe.set_active(payload.model_dump())
    return {"recipe": recipe}


class FromMealieIn(BaseModel):
    slug: str


@recipe_router.post("/from-mealie")
async def set_current_from_mealie(payload: FromMealieIn):
    """Load a Mealie recipe (by slug) as the active recipe (FoodAssistant-1g4l).

    Fetches the full recipe from Mealie, normalizes its ingredients/steps/yield
    into the active-recipe shape, and makes it current. This is what the Recipes
    and Cook page 'Cook this' buttons call so a recipe can be launched without
    leaving the app."""
    slug = (payload.slug or "").strip()
    if not slug:
        return JSONResponse({"detail": "A recipe slug is required."}, status_code=400)
    from ..services.mealie import MealieClient, MealieError
    try:
        detail = await MealieClient().get_recipe(slug)
    except MealieError as e:
        return JSONResponse({"detail": f"Could not load the recipe from Mealie: {e}"},
                            status_code=502)
    if not detail:
        return JSONResponse({"detail": "Recipe not found in Mealie."}, status_code=404)
    recipe = current_recipe.set_active(current_recipe.from_mealie_detail(detail, slug))
    return {"recipe": recipe}


@recipe_router.delete("")
def clear_current_recipe():
    """Clear the active recipe."""
    current_recipe.clear_active()
    return {"ok": True, "recipe": None}


@recipe_router.post("/scale")
def scale_current_recipe(payload: ScaleIn):
    """Set the servings-scale multiplier on the active recipe."""
    recipe = current_recipe.scale_servings(payload.factor)
    if recipe is None:
        return JSONResponse({"detail": "No active recipe"}, status_code=404)
    return {"recipe": recipe}


# How many days a saved leftover keeps before it counts as expiring. A sensible
# fridge default; the user can edit the date in Grocy after it is created.
_LEFTOVER_DEFAULT_DAYS = 4


async def _consume_active_recipe(recipe: dict) -> list[str]:
    """Consume one unit of each Grocy stock item matching an active-recipe
    ingredient. Best-effort and pure of HTTP failures: returns the names
    consumed, or [] when Grocy is unreachable."""
    from ..services.grocy import GrocyClient
    from ..services.mealie import _tokens
    try:
        stock = await GrocyClient().get_full_stock()
    except Exception:
        return []
    inv = [{"product_id": s["product_id"], "name": s["name"], "amount": s["amount"],
            "tokens": _tokens(s["name"])}
           for s in stock if s.get("product_id") and _tokens(s["name"])]
    consumed: list[str] = []
    seen: set[int] = set()
    grocy = GrocyClient()
    for ing in recipe.get("ingredients") or []:
        toks = _tokens(str(ing.get("name") or ""))
        if not toks:
            continue
        hit = next((s for s in inv if toks & s["tokens"]), None)
        if not hit or hit["product_id"] in seen:
            continue
        seen.add(hit["product_id"])
        try:
            await grocy.consume_stock(hit["product_id"], min(1.0, hit["amount"]))
            consumed.append(hit["name"])
        except Exception:
            pass
    return consumed


@recipe_router.post("/cooked")
async def cook_current_recipe(db: Session = Depends(get_db)):
    """Mark the active recipe cooked: consume matched inventory, raise a
    'save to leftovers?' action item, and clear the active recipe so a finished
    meal does not linger as the current one (FoodAssistant-yurm)."""
    recipe = current_recipe.get_active()
    if recipe is None:
        return JSONResponse({"detail": "No active recipe"}, status_code=404)
    title = recipe.get("title") or "the recipe"
    consumed = await _consume_active_recipe(recipe)
    servings = recipe.get("scaled_servings") or recipe.get("servings") or 1
    item = action_items.create(
        db, action_items.KIND_LEFTOVER_PROMPT,
        f"Save {title} to leftovers?",
        body="Cooked just now. Save the extra portions to your inventory so they "
             "show up in Expiring Soon.",
        dedupe_key=None, level="success",
        payload={"title": title, "servings": servings, "days": _LEFTOVER_DEFAULT_DAYS},
    )
    current_recipe.clear_active()
    return {"ok": True, "consumed": consumed, "action_item": item}


class LeftoverIn(BaseModel):
    title: str = ""
    servings: float = 1.0
    days: int = _LEFTOVER_DEFAULT_DAYS
    action_item_id: int | None = None


@recipe_router.post("/leftover")
async def save_leftover(payload: LeftoverIn, db: Session = Depends(get_db)):
    """Save a cooked meal to Grocy as a short-expiry leftover so it appears in
    inventory and Expiring Soon like any other item (FoodAssistant-fu1u). Then
    resolve the originating action item, if one was given."""
    if not settings.mealie_configured() and not settings.grocy_base_url:
        return JSONResponse({"detail": "Grocy is not configured."}, status_code=400)
    name = (payload.title or "Leftovers").strip()
    if not name.lower().startswith("leftover"):
        name = f"Leftovers: {name}"
    from ..models.food import FoodItem, FoodCategory, StorageType
    from ..services.grocy import GrocyClient
    days = max(0, int(payload.days or _LEFTOVER_DEFAULT_DAYS))
    item = FoodItem(
        name=name,
        quantity=max(1.0, float(payload.servings or 1)),
        category=FoodCategory.other,
        storage_type=StorageType.refrigerated,
        best_by_date=date.today() + timedelta(days=days),
    )
    try:
        result = await GrocyClient().import_item(item)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"detail": f"Could not save the leftover: {e}"}, status_code=502)
    if payload.action_item_id is not None:
        action_items.resolve(db, payload.action_item_id)
    return {"ok": True, "product_id": result.get("product_id"), "name": name}


@recipe_router.get("/timer-suggestions")
def get_timer_suggestions():
    """Return ordered timer suggestions parsed from the active recipe's steps,
    each {label, seconds, step_index}. Empty list when no recipe is active. This
    only OFFERS timers; nothing is created until /current-recipe/timers/start or
    POST /timers is called."""
    suggestions = recipe_timers.suggestions_for_recipe(current_recipe.get_active())
    return {"suggestions": suggestions}


@recipe_router.post("/timers/start")
def start_suggested_timer(payload: StartSuggestionIn = Body(...)):
    """Create a real timer from a suggestion. Pick the suggestion by step_index
    or label; seconds is taken from the payload when given, otherwise from the
    matching suggestion. Reuses the shared timer service so the countdown shows
    up on every surface."""
    suggestions = recipe_timers.suggestions_for_recipe(current_recipe.get_active())

    match = None
    for s in suggestions:
        if payload.step_index is not None and s["step_index"] != payload.step_index:
            continue
        if payload.label is not None and s["label"] != payload.label:
            continue
        match = s
        break

    seconds = payload.seconds if payload.seconds is not None else (match or {}).get("seconds")
    if seconds is None:
        return JSONResponse({"detail": "No matching suggestion"}, status_code=404)

    label = payload.label or (match or {}).get("label") or ""
    try:
        timer = timers.create_timer(label, seconds)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"timer": timer}


# --- Timers --------------------------------------------------------------


@timers_router.get("")
def get_timers():
    """List every timer with a fresh server-computed remaining/state."""
    return {"timers": timers.list_timers()}


@timers_router.post("")
def post_timer(payload: TimerIn = Body(...)):
    """Create and start a timer for `seconds`."""
    try:
        timer = timers.create_timer(payload.label, payload.seconds)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"timer": timer}


@timers_router.get("/{timer_id}")
def show_timer(timer_id: int):
    """Return one timer's current state."""
    timer = timers.get_timer(timer_id)
    if timer is None:
        return JSONResponse({"detail": "Timer not found"}, status_code=404)
    return {"timer": timer}


@timers_router.delete("/{timer_id}")
def delete_timer(timer_id: int):
    """Cancel and remove a timer."""
    if not timers.cancel_timer(timer_id):
        return JSONResponse({"detail": "Timer not found"}, status_code=404)
    return {"ok": True}
