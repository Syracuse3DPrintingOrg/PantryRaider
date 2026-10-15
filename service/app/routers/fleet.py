"""Relays a 0.21 satellite calls on its main server (/api/fleet/*).

A satellite that relays (it sends X-PR-Caps relay1) holds no AI or recipe
keys and no camera logins. It asks this server instead:

- POST /api/fleet/ai runs one AI provider call with this server's provider,
  counted against this server's token budget.
- POST /api/fleet/recipes-external runs one TheMealDB or Spoonacular lookup.
- POST /api/fleet/camera-ai answers a Reolink camera's detection state, so
  detection pop-ups keep working on satellites without the camera's login.

Only satellite, older full-access and admin keys are served. Every request is
checked against an explicit table of operations and argument shapes; a
caller's string never selects a function by name.

Nested in routers/satellite.py rather than included by main.py, so the app's
entry module never imports this module at import time.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re

from fastapi import APIRouter, HTTPException, Request
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..services.ttl_cache import TTLCache

logger = logging.getLogger("foodassistant.fleet")

router = APIRouter(prefix="/api/fleet", tags=["fleet"])

BAD_REQUEST_MSG = "The main server could not read that request."
TOO_LARGE_MSG = "That picture is too large to send to the main server."
AI_FAILED_MSG = "The main server could not reach its AI provider."
RECIPES_FAILED_MSG = "The main server could not reach the recipe service."
UNKNOWN_CAMERA_MSG = "Unknown camera."

# A 15 MB picture in base64 is 20 MB; the JSON around it fits in the rest.
MAX_BODY_BYTES = 21 * 1024 * 1024
MAX_IMAGE_BYTES = 15 * 1024 * 1024
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif",
                         "image/heic", "image/heif"})

_ALLOWED_SCOPES = ("satellite", "legacy", "admin")

# health_check answers, per role, for five minutes.
_health_vision = TTLCache(300.0)
_health_enrich = TTLCache(300.0)
# Reolink detection answers, per camera, for two seconds: several satellites
# polling at once cost the camera one login.
_camera_cache: dict[str, TTLCache] = {}


class _Invalid(Exception):
    """A request this server will not run."""


class _TooLarge(Exception):
    """A request whose picture is over the limit."""


def _require_fleet_key(request: Request) -> None:
    from ..services import access
    p = access.principal(request)
    if p.kind == "key" and p.scope in _ALLOWED_SCOPES:
        return
    if p.kind == "anonymous" or p.bad_key or p.scope in _ALLOWED_SCOPES:
        raise HTTPException(status_code=401, detail="Unauthorized")
    raise HTTPException(status_code=403, detail=access.SCOPE_MSG)


async def _json_body(request: Request, limit: int) -> dict:
    declared = request.headers.get("content-length", "")
    if declared:
        try:
            if int(declared) > limit:
                raise _TooLarge()
        except ValueError:
            raise _Invalid() from None
    # Read in chunks and stop at the limit, so a body sent without a length
    # (chunked) is never held in memory past it.
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _TooLarge()
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise _Invalid() from None
    if not isinstance(body, dict):
        raise _Invalid()
    return body


# --- argument checks -----------------------------------------------------------


def _text(value, lo: int, hi: int) -> str:
    if not isinstance(value, str) or not (lo <= len(value) <= hi):
        raise _Invalid()
    return value


def _int(value, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (lo <= value <= hi):
        raise _Invalid()
    return value


def _str_list(value, max_items: int, max_len: int) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise _Invalid()
    return [_text(v, 0, max_len) for v in value]


def _json_dict(value, max_bytes: int) -> dict:
    if not isinstance(value, dict):
        raise _Invalid()
    try:
        size = len(json.dumps(value, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        raise _Invalid() from None
    if size > max_bytes:
        raise _Invalid()
    return value


def _only(args: dict, required: set, optional: set = frozenset()) -> dict:
    if not isinstance(args, dict):
        raise _Invalid()
    keys = set(args)
    if not required <= keys or not keys <= (required | set(optional)):
        raise _Invalid()
    return args


def _image(body: dict) -> tuple[bytes, str]:
    raw = body.get("image_b64")
    mime = body.get("mime_type")
    if not isinstance(raw, str) or not raw or mime not in IMAGE_TYPES:
        raise _Invalid()
    # Cheap size check before decoding: 4 base64 characters carry 3 bytes.
    if len(raw) // 4 * 3 > MAX_IMAGE_BYTES + 3:
        raise _TooLarge()
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise _Invalid() from None
    if len(data) > MAX_IMAGE_BYTES:
        raise _TooLarge()
    if not data:
        raise _Invalid()
    return data, mime


def _no_image(body: dict) -> None:
    if body.get("image_b64") is not None:
        raise _Invalid()


def _v_image_op(body, args):
    _only(args, set())
    data, mime = _image(body)
    return {"image_data": data, "mime_type": mime}


def _v_health(body, args):
    _only(args, set())
    _no_image(body)
    return {}


def _v_enrich(body, args):
    _only(args, {"info"})
    _no_image(body)
    return {"info": _json_dict(args["info"], 64 * 1024)}


def _v_shelf_lives(body, args):
    # A receipt's lines: a satellite estimates at most 60 of them.
    _only(args, {"items"})
    _no_image(body)
    items = args["items"]
    if not isinstance(items, list) or len(items) > 100:
        raise _Invalid()
    return {"items": [_json_dict(item, 4 * 1024) for item in items]}


def _v_generate(body, args):
    _only(args, {"name"}, {"extra_instructions"})
    _no_image(body)
    return {"name": _text(args["name"], 1, 200),
            "extra_instructions": _text(args.get("extra_instructions", ""), 0, 2000)}


def _v_suggest(body, args):
    _only(args, {"items"}, {"limit", "preferences"})
    _no_image(body)
    return {"items": _str_list(args["items"], 500, 200),
            "limit": _int(args.get("limit", 8), 1, 50),
            "preferences": _text(args.get("preferences", ""), 0, 2000)}


def _v_nutrition(body, args):
    _only(args, {"name"}, {"servings"})
    _no_image(body)
    servings = args.get("servings", 1.0)
    if (isinstance(servings, bool) or not isinstance(servings, (int, float))
            or not (0 < servings <= 100)):
        raise _Invalid()
    return {"name": _text(args["name"], 1, 200), "servings": float(servings)}


def _v_extract_recipe(body, args):
    if body.get("image_b64") is not None:
        _only(args, set())
        data, mime = _image(body)
        return {"image_data": data, "mime_type": mime}
    _only(args, {"page_text"})
    return {"page_text": _text(args["page_text"], 0, 200000)}


def _v_optimize(body, args):
    _only(args, {"recipe"})
    _no_image(body)
    return {"recipe": _json_dict(args["recipe"], 256 * 1024)}


def _v_parse_ingredients(body, args):
    _only(args, {"lines"})
    _no_image(body)
    return {"lines": _str_list(args["lines"], 200, 500)}


_BARCODE_RE = re.compile(r"^[0-9]{6,32}$")


def _v_barcode(body, args):
    _only(args, {"barcode"})
    _no_image(body)
    code = args["barcode"]
    if not isinstance(code, str) or not _BARCODE_RE.match(code):
        raise _Invalid()
    return {"barcode": code}


# op -> (argument check, the provider call). The call is a fixed lambda per
# op, so the operation a caller names only ever picks a row in this table.
_AI_OPS = {
    "analyze_food": (_v_image_op, lambda prov, a: prov.analyze_food(**a)),
    "analyze_receipt": (_v_image_op, lambda prov, a: prov.analyze_receipt(**a)),
    "extract_receipt_prices": (_v_image_op,
                               lambda prov, a: prov.extract_receipt_prices(**a)),
    "health_check": (_v_health, lambda prov, a: prov.health_check()),
    "enrich_product": (_v_enrich, lambda prov, a: prov.enrich_product(**a)),
    "estimate_shelf_lives": (_v_shelf_lives,
                             lambda prov, a: prov.estimate_shelf_lives(**a)),
    "generate_recipe": (_v_generate, lambda prov, a: prov.generate_recipe(**a)),
    "suggest_from_inventory": (_v_suggest, lambda prov, a: prov.suggest_from_inventory(**a)),
    "estimate_nutrition": (_v_nutrition, lambda prov, a: prov.estimate_nutrition(**a)),
    "extract_recipe": (_v_extract_recipe, lambda prov, a: prov.extract_recipe(**a)),
    "optimize_recipe": (_v_optimize, lambda prov, a: prov.optimize_recipe(**a)),
    "parse_ingredients": (_v_parse_ingredients, lambda prov, a: prov.parse_ingredients(**a)),
    "identify_barcode": (_v_barcode, lambda prov, a: prov.identify_barcode(**a)),
}
AI_OPS = tuple(_AI_OPS)


def _health_cache(role: str) -> TTLCache:
    return _health_enrich if role == "enrich" else _health_vision


def reset_caches() -> None:
    """Forget cached health and camera answers (tests, settings changes)."""
    _health_vision.invalidate()
    _health_enrich.invalidate()
    _camera_cache.clear()


def _provider(role: str):
    from .. import dependencies
    if role == "enrich":
        return dependencies.get_enrich_provider()
    return dependencies.get_vision_provider()


def _result(value):
    from ..models.food import AnalysisResult
    if isinstance(value, AnalysisResult):
        return value.model_dump(mode="json")
    return value


@router.post("/ai")
async def fleet_ai(request: Request):
    """Run one AI provider call for a satellite with this server's provider."""
    _require_fleet_key(request)
    try:
        body = await _json_body(request, MAX_BODY_BYTES)
        role = body.get("role")
        op = body.get("op")
        if role not in ("vision", "enrich") or not isinstance(op, str) or op not in _AI_OPS:
            raise _Invalid()
        check, call = _AI_OPS[op]
        args = check(body, body.get("args") if body.get("args") is not None else {})
    except _TooLarge:
        raise HTTPException(status_code=413, detail=TOO_LARGE_MSG) from None
    except _Invalid:
        raise HTTPException(status_code=400, detail=BAD_REQUEST_MSG) from None

    if op == "health_check":
        cached = _health_cache(role).get()
        if cached is not None:
            return {"ok": True, "result": cached}
    else:
        from ..services import usage
        if usage.over_budget():
            from .analyze import _BUDGET_MSG
            raise HTTPException(status_code=429, detail=_BUDGET_MSG)

    try:
        result = await call(_provider(role), args)
    except StarletteHTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - one answer for every provider failure
        logger.warning("fleet AI %s failed: %s", op, type(exc).__name__)
        if op == "health_check":
            result = False
        else:
            raise HTTPException(status_code=502, detail=AI_FAILED_MSG) from None
    if op == "health_check":
        result = bool(result)
        _health_cache(role).set(result)
    return {"ok": True, "result": _result(result)}


# --- recipe lookups --------------------------------------------------------------


def _v_search(args):
    _only(args, {"query"}, {"limit"})
    return {"query": _text(args["query"], 0, 200), "limit": _int(args.get("limit", 12), 1, 50)}


def _v_find(args):
    _only(args, {"ingredients"}, {"limit", "dietary", "max_time", "cuisine"})
    return {"ingredients": _str_list(args["ingredients"], 100, 100),
            "limit": _int(args.get("limit", 12), 1, 50),
            "dietary": _text(args.get("dietary", ""), 0, 200),
            "max_time": _int(args.get("max_time", 0), 0, 1440),
            "cuisine": _text(args.get("cuisine", ""), 0, 200)}


def _v_lookup(args):
    _only(args, {"external_id"}, {"source"})
    source = args.get("source", "themealdb")
    if source not in ("themealdb", "spoonacular"):
        raise _Invalid()
    return {"external_id": _text(args["external_id"], 1, 64), "source": source}


def _recipes():
    from ..services import recipes_external
    return recipes_external


_RECIPE_OPS = {
    "search_recipes_by_name": (_v_search,
                               lambda a: _recipes().search_recipes_by_name(**a)),
    "find_recipes_for_ingredients": (_v_find,
                                     lambda a: _recipes().find_recipes_for_ingredients(**a)),
    "get_external_recipe": (_v_lookup, lambda a: _recipes().get_external_recipe(**a)),
}
RECIPE_OPS = tuple(_RECIPE_OPS)


@router.post("/recipes-external")
async def fleet_recipes_external(request: Request):
    """Run one external recipe lookup for a satellite with this server's keys."""
    _require_fleet_key(request)
    try:
        body = await _json_body(request, 1024 * 1024)
        op = body.get("op")
        if not isinstance(op, str) or op not in _RECIPE_OPS:
            raise _Invalid()
        check, call = _RECIPE_OPS[op]
        args = check(body.get("args") if body.get("args") is not None else {})
    except (_Invalid, _TooLarge):
        raise HTTPException(status_code=400, detail=BAD_REQUEST_MSG) from None
    try:
        result = await call(args)
    except Exception as exc:  # noqa: BLE001 - one answer for every lookup failure
        logger.warning("fleet recipe lookup %s failed: %s", op, type(exc).__name__)
        raise HTTPException(status_code=502, detail=RECIPES_FAILED_MSG) from None
    return {"ok": True, "result": result}


# --- Reolink detection -------------------------------------------------------------


def _find_reolink(name: str):
    from ..config import settings
    from ..services import cameras
    want = name.strip().lower()
    for entry in settings.streamdeck_cameras or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("name", "") or "").strip().lower() == want and cameras.is_reolink(entry):
            return entry
    return None


@router.post("/camera-ai")
async def fleet_camera_ai(request: Request):
    """A Reolink camera's detection state, for a satellite without its login."""
    _require_fleet_key(request)
    try:
        body = await _json_body(request, 4096)
        name = _text(body.get("name"), 1, 120)
    except (_Invalid, _TooLarge):
        raise HTTPException(status_code=400, detail=BAD_REQUEST_MSG) from None
    entry = _find_reolink(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=UNKNOWN_CAMERA_MSG)
    key = name.strip().lower()
    cache = _camera_cache.get(key)
    if cache is None:
        cache = _camera_cache[key] = TTLCache(2.0)
    state = cache.get()
    if state is None:
        from ..services import cameras
        try:
            state = await cameras.fetch_reolink_ai_state(entry)
        except Exception:  # noqa: BLE001 - a camera problem costs the pop-up only
            state = {}
        state = state if isinstance(state, dict) else {}
        cache.set(state)
    return {"ok": True, "state": state}
