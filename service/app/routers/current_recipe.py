"""REST for the active recipe and the shared timer registry (main server).

Both features are server-side foundation for the Current Recipe epic: the active
recipe and the timers live in process memory on the main server so the future
web UI tab, Stream Deck, and satellites consume the same state. These routes sit
behind the app's normal require_auth middleware, so they need no extra auth.

Two APIRouters share this module: one under /current-recipe, one under /timers.
"""
from __future__ import annotations

from datetime import date, timedelta

import httpx
from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..services import action_items, cook_counts, current_recipe, recipe_timers, timers
from ..services.ttl_cache import TTLCache

recipe_router = APIRouter(prefix="/current-recipe", tags=["current-recipe"])
timers_router = APIRouter(prefix="/timers", tags=["timers"])

# Forwarding client for the satellite -> main server case (see _upstream).
# The tight connect timeout matters on a kiosk LAN: a button press should fail
# fast and report that the main server is not reachable rather than hang the
# tap.
_fwd_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))

# Satellite-only micro-cache for GET /timers. Several surfaces poll it in the
# same second (Timers page, Start Page faces, screensaver pills, the deck via
# the app); holding the last upstream body briefly turns that burst into one
# round trip to the main server. Any mutation forwarded through this satellite
# invalidates it so a just-started or just-cancelled timer shows immediately.
_TIMERS_CACHE_TTL = 2.0
_timers_cache = TTLCache(_TIMERS_CACHE_TTL)


def _upstream() -> str | None:
    """The main server's base URL if this device is a satellite, else None.

    Timers live on the MAIN server so every surface agrees on the same running
    countdowns (see services/timers.py). A satellite keeps no timer registry of
    its own: it forwards every /timers call to the server and shows what the
    server returns. That way a timer started on a Pi Remote kiosk or deck is
    immediately visible on the server and every other device, and vice versa.
    """
    if settings.is_satellite() and settings.remote_server_url and settings.upstream_api_key:
        return settings.remote_server_url.rstrip("/")
    return None


async def _forward(request: Request, path: str) -> Response:
    """Proxy this timer request to the main server, preserving method/body.

    Authenticated with the satellite's upstream API key, the same key the
    pending and Grocy/Mealie proxies use. Returns the server's response
    verbatim (status included) so the caller sees exactly what it would see
    talking to the server directly.
    """
    base = _upstream()
    headers = {"X-API-Key": settings.upstream_api_key}
    ct = request.headers.get("content-type")
    if ct:
        headers["Content-Type"] = ct
    body = await request.body()
    if request.method != "GET":
        # A mutation is about to land upstream: the cached timer list is stale
        # the moment it succeeds, so drop it before AND regardless of outcome
        # (a failed forward just means the next GET refetches, which is fine).
        _timers_cache.invalidate()
    try:
        up = await _fwd_client.request(
            request.method,
            f"{base}{path}",
            headers=headers,
            params=dict(request.query_params),
            content=body or None,
        )
    except Exception:
        return JSONResponse(
            {"detail": "The main server is not reachable. "
                       "This will work again when it is."},
            status_code=502,
        )
    media = up.headers.get("content-type", "application/json")
    return Response(content=up.content, status_code=up.status_code, media_type=media)


async def _create_timer_upstream(label: str, seconds: float) -> Response:
    """Create a timer on the main server from an already-resolved label and
    duration. The recipe-suggestion start needs this seam: the suggestion is
    matched against THIS device's active recipe (recipe state is local to each
    device), but the timer itself must land in the server registry, so we post
    the resolved values to the shared POST /timers endpoint."""
    base = _upstream()
    _timers_cache.invalidate()
    try:
        up = await _fwd_client.request(
            "POST",
            f"{base}/timers",
            headers={"X-API-Key": settings.upstream_api_key},
            json={"label": label, "seconds": seconds},
        )
    except Exception:
        return JSONResponse(
            {"detail": "The main server is not reachable. "
                       "This will work again when it is."},
            status_code=502,
        )
    media = up.headers.get("content-type", "application/json")
    return Response(content=up.content, status_code=up.status_code, media_type=media)


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


class ExtendIn(BaseModel):
    """Add time to a running timer; 60 seconds is the on-screen "+1 min"."""
    seconds: float = 60


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
    # When true, add this recipe as another concurrent course rather than
    # replacing the primary recipe (FoodAssistant-dbgx).
    as_course: bool = False


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
    normalized = current_recipe.from_mealie_detail(detail, slug)
    recipe = (current_recipe.add_recipe(normalized) if payload.as_course
              else current_recipe.set_active(normalized))
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


async def _cook_recipe(recipe: dict, slot: int, db: Session) -> dict:
    """Shared cook flow for a recipe (primary or a course): consume matched
    inventory, raise a 'save to leftovers?' action item, and clear that slot."""
    title = recipe.get("title") or "the recipe"
    # Bump the made-before count for this recipe before consuming, so any cook
    # path (primary or a course) feeds the same tally (FoodAssistant-bjps). The
    # id is the Mealie slug for a saved recipe; fails soft to no-op.
    cook_counts.record_cook(db, recipe.get("source"), slug=recipe.get("id"),
                            title=recipe.get("title"))
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
    current_recipe.clear_recipe(slot)
    return {"ok": True, "consumed": consumed, "action_item": item}


@recipe_router.post("/cooked")
async def cook_current_recipe(db: Session = Depends(get_db)):
    """Mark the primary recipe cooked (FoodAssistant-yurm)."""
    recipe = current_recipe.get_active()
    if recipe is None:
        return JSONResponse({"detail": "No active recipe"}, status_code=404)
    return await _cook_recipe(recipe, current_recipe.PRIMARY_SLOT, db)


# --- Multiple concurrent recipes (FoodAssistant-dbgx) --------------------

@recipe_router.get("/all")
def list_all_recipes(db: Session = Depends(get_db)):
    """Every recipe currently in progress (primary first), each with its slot,
    annotated with how many times it has been cooked (FoodAssistant-bjps)."""
    recipes = current_recipe.list_all()
    cook_counts.annotate(db, recipes)
    return {"recipes": recipes}


@recipe_router.get("/equipment")
def recipe_equipment(slot: int = 0):
    """Cookware and utensils a recipe likely needs, plus any appliances the user
    has not marked as owned (FoodAssistant-ooq3)."""
    from ..services import utensils
    recipe = current_recipe.get_recipe(slot)
    if recipe is None:
        return JSONResponse({"detail": "No recipe in that slot"}, status_code=404)
    equipment = utensils.detect_equipment(recipe)
    return {
        "equipment": equipment,
        "missing_appliances": utensils.missing_appliances(
            equipment, settings.kitchen_appliances),
    }


@recipe_router.post("/courses")
def add_course(payload: RecipeIn):
    """Add another concurrent recipe (a course) without clearing the others."""
    return {"recipe": current_recipe.add_recipe(payload.model_dump())}


@recipe_router.post("/{slot}/scale")
def scale_one(slot: int, payload: ScaleIn):
    recipe = current_recipe.scale_recipe(slot, payload.factor)
    if recipe is None:
        return JSONResponse({"detail": "No recipe in that slot"}, status_code=404)
    return {"recipe": recipe}


@recipe_router.delete("/{slot}")
def clear_one(slot: int):
    return {"ok": current_recipe.clear_recipe(slot)}


@recipe_router.post("/{slot}/cooked")
async def cook_one(slot: int, db: Session = Depends(get_db)):
    recipe = current_recipe.get_recipe(slot)
    if recipe is None:
        return JSONResponse({"detail": "No recipe in that slot"}, status_code=404)
    return await _cook_recipe(recipe, slot, db)


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
async def start_suggested_timer(payload: StartSuggestionIn = Body(...)):
    """Create a real timer from a suggestion. Pick the suggestion by step_index
    or label; seconds is taken from the payload when given, otherwise from the
    matching suggestion. Reuses the shared timer service so the countdown shows
    up on every surface.

    On a satellite the suggestion is still resolved here, against this device's
    own active recipe (recipe state is per-device), but the resulting timer is
    created on the main server, where all timers live."""
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
    if _upstream():
        return await _create_timer_upstream(label, float(seconds))
    try:
        timer = timers.create_timer(label, seconds)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"timer": timer}


# --- Timers --------------------------------------------------------------


@timers_router.get("")
async def get_timers(request: Request):
    """List every timer with a fresh server-computed remaining/state."""
    if _upstream():
        hit = _timers_cache.get()
        if hit is not None:
            content, status, media = hit
            return Response(content=content, status_code=status, media_type=media)
        resp = await _forward(request, "/timers")
        if resp.status_code == 200:
            _timers_cache.set((resp.body, resp.status_code, resp.media_type))
        return resp
    return {"timers": timers.list_timers()}


@timers_router.post("")
async def post_timer(request: Request, payload: TimerIn = Body(...)):
    """Create and start a timer for `seconds`."""
    if _upstream():
        return await _forward(request, "/timers")
    try:
        timer = timers.create_timer(payload.label, payload.seconds)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"timer": timer}


@timers_router.get("/{timer_id}")
async def show_timer(timer_id: int, request: Request):
    """Return one timer's current state."""
    if _upstream():
        return await _forward(request, f"/timers/{timer_id}")
    timer = timers.get_timer(timer_id)
    if timer is None:
        return JSONResponse({"detail": "Timer not found"}, status_code=404)
    return {"timer": timer}


@timers_router.post("/{timer_id}/extend")
async def extend_timer(timer_id: int, request: Request, payload: ExtendIn = Body(...)):
    """Add seconds to a running timer. An expired timer cannot be extended
    (dismiss it and start a new one), so it 404s like a missing id."""
    if _upstream():
        return await _forward(request, f"/timers/{timer_id}/extend")
    try:
        timer = timers.extend_timer(timer_id, payload.seconds)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    if timer is None:
        return JSONResponse({"detail": "Timer not found or already done"}, status_code=404)
    return {"timer": timer}


@timers_router.delete("/{timer_id}")
async def delete_timer(timer_id: int, request: Request):
    """Cancel and remove a timer."""
    if _upstream():
        return await _forward(request, f"/timers/{timer_id}")
    if not timers.cancel_timer(timer_id):
        return JSONResponse({"detail": "Timer not found"}, status_code=404)
    return {"ok": True}


@timers_router.delete("")
async def delete_all_timers(request: Request):
    """Cancel and remove every timer at once (the Timers page Clear all).
    Clearing an already-empty registry succeeds with cleared 0."""
    if _upstream():
        return await _forward(request, "/timers")
    return {"ok": True, "cleared": timers.clear_all()}
