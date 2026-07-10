"""Forager community recipes as a recipe source (FoodAssistant-l2hk, Stage 3a).

Talks to the Forager cloud recipe endpoints and normalizes the replies into the
same recipe shape the other external sources use (recipes_external), so the
community catalog plugs straight into the existing browse / search / preview /
save flow. Every result carries ``source="forager"`` so a later source-badge or
cook-count change (FoodAssistant-5frk, -bjps) can key off it.

Auth reuses the install's own Forager instance token exactly like the AI proxy
(providers/cloud.py): a bearer token against ``settings.cloud_base_url``.

Everything here fails soft. A short timeout guards each call, and any network or
HTTP error resolves to an empty list / ``None`` (browse) so the rest of recipe
browsing keeps working when Forager is unreachable or the device is not linked.
The parse/normalize/payload helpers are pure so they unit-test without a network.

Cloud endpoints (deployed as Stage 1/2, not changed here):
  GET  /v1/recipes?query=&page=&per_page=  -> approved community cards
  GET  /v1/recipes/{id}                    -> full recipe
  POST /v1/recipes                         -> submit (attribution required)
"""
from __future__ import annotations

import logging

import httpx

from ..config import settings

logger = logging.getLogger("foodassistant.recipes_forager")

# Short, browse-friendly timeouts: a slow community lookup must never hold up the
# recipes page, so a miss just drops the community source for that request.
_SEARCH_TIMEOUT = httpx.Timeout(6.0, connect=4.0)
_DETAIL_TIMEOUT = httpx.Timeout(6.0, connect=4.0)
_SUBMIT_TIMEOUT = httpx.Timeout(8.0, connect=4.0)

SOURCE = "forager"


def _headers() -> dict:
    from ..config import APP_VERSION
    return {
        "Authorization": f"Bearer {settings.cloud_instance_token}",
        "X-Device-Version": APP_VERSION,
        "X-Device-Mode": settings.deployment_mode or "server",
    }


def _base() -> str:
    return (settings.cloud_base_url or "").rstrip("/")


def _client(timeout: httpx.Timeout,
            transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    # Injectable transport so tests exercise the real request/parse path against
    # httpx.MockTransport with no network (matching providers/cloud.py).
    kwargs: dict = {"timeout": timeout}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)


# ── Normalization ──────────────────────────────────────────────────────────────

def _normalize_card(card: dict) -> dict:
    """A community list card -> the app's external-recipe browse shape.

    Pure. Carries the attribution and rating through so the browse row can show
    who to credit and the community rating without another round trip."""
    rid = card.get("id")
    return {
        "name": (card.get("title") or "").strip(),
        "slug": None,
        "external_id": str(rid) if rid is not None else "",
        "source": SOURCE,
        "description": (card.get("description") or "").strip(),
        "servings": "",
        "total_time": "",
        "image": card.get("image_url") or None,
        "source_url": "",
        "cuisine": "",
        "attribution": (card.get("attribution") or "").strip(),
        "average_rating": card.get("average_rating"),
        "rating_count": card.get("rating_count"),
        "ingredients": [],
        "instructions": [],
        "recipeIngredient": [],
    }


def _as_str_list(value) -> list[str]:
    """Coerce a cloud ingredients/steps field (list of strings, or list of
    objects with a text/note/name key) into a clean list of step strings."""
    out: list[str] = []
    for item in value or []:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = (item.get("text") or item.get("note") or item.get("name")
                    or item.get("display") or "")
        else:
            text = str(item)
        text = (text or "").strip()
        if text:
            out.append(text)
    return out


def _normalize_detail(detail: dict) -> dict:
    """A full community recipe -> the app's normalized recipe shape.

    Mirrors recipes_external._normalized, including the ``recipeIngredient``
    field the tier classifier and the Mealie save path read, so a downloaded
    community recipe imports exactly like a TheMealDB/Spoonacular one. The
    attribution is folded into the description so credit rides along into the
    saved recipe even though Mealie has no attribution field of its own."""
    rid = detail.get("id")
    ingredients = _as_str_list(detail.get("ingredients"))
    instructions = _as_str_list(detail.get("steps") or detail.get("instructions"))
    attribution = (detail.get("attribution") or "").strip()
    description = (detail.get("description") or "").strip()
    credited = _with_attribution(description, attribution)
    return {
        "name": (detail.get("title") or "").strip(),
        "slug": None,
        "external_id": str(rid) if rid is not None else "",
        "source": SOURCE,
        "description": credited,
        "servings": str(detail.get("servings") or ""),
        "total_time": str(detail.get("total_time") or ""),
        "image": detail.get("image_url") or None,
        "source_url": detail.get("source_url") or "",
        "cuisine": "",
        "attribution": attribution,
        "average_rating": detail.get("average_rating"),
        "rating_count": detail.get("rating_count"),
        "ingredients": ingredients,
        "instructions": instructions,
        "recipeIngredient": [{"note": i} for i in ingredients],
    }


def _with_attribution(description: str, attribution: str) -> str:
    """Append a 'Shared to the Forager community by X' credit line to a
    description, unless the attribution is already mentioned. Pure."""
    if not attribution:
        return description
    credit = f"Shared to the Forager community by {attribution}."
    if attribution in description:
        return description
    return f"{description}\n\n{credit}".strip() if description else credit


def build_submit_payload(recipe: dict, attribution: str) -> dict:
    """Build the POST /v1/recipes body from a recipe the user has, plus who to
    credit. Pure, so the router can validate before any network call.

    Raises ValueError (with a user-facing, no-jargon message) when the recipe
    has no name or the required attribution is missing, matching the cloud's own
    validation so the two never disagree."""
    title = (recipe.get("name") or recipe.get("title") or "").strip()
    attribution = (attribution or "").strip()
    if not title:
        raise ValueError("Give the recipe a name before sharing it.")
    if not attribution:
        raise ValueError("Add who to credit before sharing this recipe with the community.")
    ingredients = _as_str_list(recipe.get("ingredients"))
    if not ingredients:
        ingredients = [
            (i.get("note") or "").strip()
            for i in recipe.get("recipeIngredient") or []
            if isinstance(i, dict) and (i.get("note") or "").strip()
        ]
    steps = _as_str_list(recipe.get("instructions") or recipe.get("steps"))
    return {
        "title": title,
        "description": (recipe.get("description") or "").strip(),
        "ingredients": ingredients,
        "steps": steps,
        "attribution": attribution,
        "image_url": (recipe.get("image_url") or recipe.get("image") or "").strip(),
    }


# ── Bundle helpers (pure, testable) ────────────────────────────────────────────

def _title_key(name: str) -> str:
    """Normalize a recipe name for dedupe: trimmed, lower-cased, single-spaced."""
    return " ".join((name or "").strip().lower().split())


def partition_new(cards: list[dict], existing_titles) -> tuple[list[dict], list[dict]]:
    """Split fetched community cards into (new, already_present) by title.

    Pure, so the "which of these do we still need" decision unit-tests without a
    network. ``existing_titles`` is any iterable of recipe names already in the
    local library; matching ignores case and surrounding whitespace. A card with
    no usable name, one whose title is already in the library, or a repeat of an
    earlier card in the same batch all land in already_present (skipped), so a
    re-run never duplicates a recipe."""
    have = {_title_key(t) for t in existing_titles or []}
    seen: set[str] = set()
    new: list[dict] = []
    present: list[dict] = []
    for card in cards or []:
        key = _title_key(card.get("name"))
        if not key or key in have or key in seen:
            present.append(card)
            continue
        seen.add(key)
        new.append(card)
    return new, present


def format_bundle_summary(added: int, skipped: int, failed: int) -> str:
    """A user-forward, no-jargon result line for the bundle action. Pure."""
    if not added and not skipped and not failed:
        return "No community recipes were available to add right now."
    parts = [f"Added {added} recipe{'' if added == 1 else 's'}"]
    if skipped:
        parts.append(f"skipped {skipped} already in your library")
    if failed:
        parts.append(f"{failed} could not be added this time")
    return ", ".join(parts) + "."


# ── Cloud calls (all fail soft) ────────────────────────────────────────────────

async def search_recipes(query: str = "", limit: int = 12,
                         transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """Approved community recipes matching ``query`` (empty = browse), as browse
    cards. Returns [] when not linked, disabled, or the cloud call fails."""
    if not settings.forager_recipes_active():
        return []
    params = {"query": query.strip(), "page": 1, "per_page": max(1, min(limit, 50))}
    try:
        async with _client(_SEARCH_TIMEOUT, transport) as client:
            resp = await client.get(f"{_base()}/v1/recipes", params=params,
                                    headers=_headers())
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:  # unreachable / unlinked / bad reply: drop the source
        logger.info("forager recipes: search failed, omitting community source: %s", exc)
        return []
    items = body.get("recipes") if isinstance(body, dict) else body
    if items is None and isinstance(body, dict):
        items = body.get("items") or body.get("results")
    cards = [_normalize_card(c) for c in (items or []) if isinstance(c, dict)]
    return [c for c in cards if c["name"] and c["external_id"]]


async def get_recipe(recipe_id: str,
                     transport: httpx.AsyncBaseTransport | None = None) -> dict | None:
    """One full community recipe by id, normalized, or None on any failure."""
    if not settings.forager_recipes_active():
        return None
    rid = (recipe_id or "").strip()
    if not rid:
        return None
    try:
        async with _client(_DETAIL_TIMEOUT, transport) as client:
            resp = await client.get(f"{_base()}/v1/recipes/{rid}", headers=_headers())
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        logger.info("forager recipes: detail %s failed: %s", rid, exc)
        return None
    if not isinstance(body, dict):
        return None
    detail = body.get("recipe") if isinstance(body.get("recipe"), dict) else body
    recipe = _normalize_detail(detail)
    return recipe if recipe["name"] else None


async def submit_recipe(payload: dict,
                        transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """POST a recipe to the community. Returns a small status dict the router
    maps to a user-facing message; never raises on a network/HTTP error:

        {"status": int, "id": str|None, "error": str|None, "body": dict}

    ``status`` 0 means the cloud was unreachable. The caller (routers/mealie.py
    share endpoint) turns 422 into the attribution message, 429 into the
    friendly rate-limit note, and anything else into a soft failure."""
    if not settings.cloud_instance_token:
        return {"status": 401, "id": None, "error": "not linked", "body": {}}
    try:
        async with _client(_SUBMIT_TIMEOUT, transport) as client:
            resp = await client.post(f"{_base()}/v1/recipes", json=payload,
                                     headers=_headers())
    except Exception as exc:
        logger.info("forager recipes: submit unreachable: %s", exc)
        return {"status": 0, "id": None, "error": str(exc), "body": {}}
    try:
        body = resp.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    return {"status": resp.status_code, "id": body.get("id"),
            "error": body.get("detail") or body.get("error"), "body": body}
