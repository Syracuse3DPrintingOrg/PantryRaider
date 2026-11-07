"""Your own recipe databases (FoodAssistant-1sfs6).

A recipe catalog is any website that serves the Pantry Raider Recipe Catalog
API, a small read-only REST contract documented in docs/recipe-catalogs.md:

  GET {base}/catalog         who the catalog is and how many recipes it holds
  GET {base}/recipes         a page of recipe summaries (with ingredient lines)
  GET {base}/recipes/{id}    one full recipe

Deeserved (deeserved.com) is the first one. Every catalog the user adds shows
up in the same places the web sources do: recipe search, the Cook page's
"what can I make" suggestions (matched against stock), the preview, Cook, and
Save into the library. A catalog recipe's ``source`` is ``catalog:<id>``, so
every existing external-recipe path (preview, import, cook) can route it here
by prefix without a new endpoint.

The catalog list lives in ``settings.recipe_catalogs`` and the optional access
keys in ``settings.recipe_catalog_keys``; this module is the only reader of
either. The parse helpers are pure so the contract stays testable without a
network; the fetchers fail soft (a catalog that is down simply contributes no
recipes, it never breaks a page).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import quote, urlsplit

import httpx

from ..config import APP_VERSION, settings
from . import tls

logger = logging.getLogger("foodassistant.recipe_catalogs")

API_NAME = "pantry-raider-catalog"
API_VERSION = 1
SOURCE_PREFIX = "catalog:"

# How much of one catalog the Cook page considers. The listing carries the
# ingredient lines, so the whole catalog is matched against stock from a few
# paged calls; the cap keeps a very large catalog from turning one Cook page
# load into dozens of requests.
_PAGE_SIZE = 200
_MAX_RECIPES = 2000
# Catalog listings change rarely (a family adds a card now and then), so a
# few minutes of caching makes repeat Cook and search visits instant without
# a newly added recipe taking long to appear.
_CACHE_TTL = 600
_TIMEOUT = httpx.Timeout(12.0, connect=5.0)

# Built-in presets the settings panel offers as one-click adds.
PRESETS = [
    {"id": "deeserved", "name": "Deeserved",
     "url": "https://deeserved.com/api/pantry-raider",
     "description": "A family recipe box of scanned and transcribed recipe cards."},
]

_listing_cache: dict[str, tuple[float, list[dict]]] = {}
# Tests hand in an httpx.MockTransport here; None is the real network.
_transport = None


class CatalogError(Exception):
    """A catalog could not be reached or did not speak the catalog API. The
    message is user-forward: the settings panel shows it as is."""


# ── Configuration (pure over the settings values) ─────────────────────────────

_ID_RE = re.compile(r"[^a-z0-9]+")


def make_id(name: str, taken=()) -> str:
    """A short stable id from a catalog's name, unique among ``taken``."""
    base = _ID_RE.sub("-", (name or "").lower()).strip("-")[:32] or "catalog"
    out, n = base, 2
    while out in taken:
        out = f"{base}-{n}"
        n += 1
    return out


def normalize_base_url(url: str) -> str:
    """The catalog's base address from whatever the user pasted.

    Accepts the base itself or one of its endpoints (a pasted ``.../catalog``
    or ``.../recipes`` link is trimmed back to the base), drops a query string
    and trailing slashes, and refuses anything that is not http(s). Raises
    CatalogError with copy the settings panel can show."""
    raw = (url or "").strip()
    if not raw:
        raise CatalogError("Enter the catalog's address.")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    try:
        parts.port  # a pasted "javascript:..." lands here as a bad port
    except ValueError:
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        raise CatalogError("The address must start with http:// or https://.")
    path = parts.path.rstrip("/")
    for tail in ("/catalog", "/recipes"):
        if path.endswith(tail):
            path = path[: -len(tail)]
    return f"{parts.scheme}://{parts.netloc}{path}".rstrip("/")


def clean_entry(raw) -> dict | None:
    """One stored catalog entry in its canonical shape, or None for junk."""
    if not isinstance(raw, dict):
        return None
    cid = str(raw.get("id") or "").strip()
    url = str(raw.get("url") or "").strip()
    if not cid or not url:
        return None
    return {"id": cid, "name": str(raw.get("name") or cid).strip()[:80] or cid,
            "url": url, "enabled": raw.get("enabled", True) is not False}


def configured() -> list[dict]:
    """Every catalog the user has added, in the order they were added."""
    rows = settings.recipe_catalogs if isinstance(settings.recipe_catalogs, list) else []
    out, seen = [], set()
    for raw in rows:
        entry = clean_entry(raw)
        if entry and entry["id"] not in seen:
            seen.add(entry["id"])
            out.append(entry)
    return out


def enabled() -> list[dict]:
    return [c for c in configured() if c["enabled"]]


def get(catalog_id: str) -> dict | None:
    return next((c for c in configured() if c["id"] == catalog_id), None)


def key_for(catalog_id: str) -> str:
    keys = settings.recipe_catalog_keys if isinstance(settings.recipe_catalog_keys, dict) else {}
    return str(keys.get(catalog_id) or "")


def source_for(catalog_id: str) -> str:
    return SOURCE_PREFIX + catalog_id


def owns(source: str | None) -> bool:
    """Whether a recipe ``source`` names a catalog."""
    return str(source or "").startswith(SOURCE_PREFIX)


def catalog_id_of(source: str | None) -> str:
    return str(source or "")[len(SOURCE_PREFIX):] if owns(source) else ""


def display_name(source: str | None) -> str:
    """The catalog's name for a ``catalog:<id>`` source, for badges."""
    entry = get(catalog_id_of(source))
    return entry["name"] if entry else "Recipe database"


def badge(source: str | None) -> dict:
    return {"label": display_name(source),
            "css_class": "bg-warning-subtle text-warning-emphasis border"}


# ── Contract parsing (pure) ───────────────────────────────────────────────────

def _text(value, limit: int = 2000) -> str:
    return str(value).strip()[:limit] if value not in (None, "") else ""


def _lines(value, limit: int = 300) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:limit]:
        if isinstance(item, dict):
            item = item.get("text") or item.get("name") or ""
        text = _text(item)
        if text:
            out.append(text)
    return out


def _groups(value) -> list[tuple[str | None, list[str]]]:
    """[(heading, lines)] from a contract ``*_groups`` list."""
    if not isinstance(value, list):
        return []
    out = []
    for group in value[:50]:
        if not isinstance(group, dict):
            continue
        lines = _lines(group.get("items"))
        if lines:
            out.append((_text(group.get("heading"), 120) or None, lines))
    return out


def _image(value) -> str:
    url = _text(value, 2000)
    return url if url.startswith(("https://", "http://")) else ""


def parse_info(body) -> dict:
    """The /catalog answer, checked against the contract."""
    if not isinstance(body, dict) or body.get("api") != API_NAME:
        raise CatalogError("That address answered, but it is not a Pantry Raider "
                           "recipe catalog.")
    try:
        version = int(body.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < 1:
        raise CatalogError("That catalog did not say which version of the "
                           "catalog API it speaks.")
    try:
        count = int(body.get("recipe_count"))
    except (TypeError, ValueError):
        count = None
    return {"name": _text(body.get("name"), 80), "description": _text(body.get("description"), 300),
            "url": _text(body.get("url"), 500), "version": version,
            "recipe_count": count, "updated_at": _text(body.get("updated_at"), 40)}


def normalize_summary(row, source: str) -> dict | None:
    """One listing row in the shape the external sources share
    (recipes_external._normalized), so search and the Cook page's tier
    classifier treat a catalog recipe like any other web recipe."""
    if not isinstance(row, dict):
        return None
    rid = _text(row.get("id"), 200)
    name = _text(row.get("name"), 200)
    if not rid or not name:
        return None
    ingredients = _lines(row.get("ingredients"))
    categories = _lines(row.get("categories"), 20)
    return {
        "name": name,
        "slug": None,
        "external_id": rid,
        "source": source,
        "description": _text(row.get("description"), 1000),
        "servings": _text(row.get("servings"), 120),
        "total_time": _text(row.get("total_time"), 120),
        "image": _image(row.get("image")),
        "source_url": _image(row.get("url")),
        # The cuisine filter matches this text; a catalog's categories
        # ("Soups and salads", "Italian") are the closest thing it has.
        "cuisine": " ".join([_text(row.get("cuisine"), 80)] + categories).strip(),
        "author": _text(row.get("author"), 120),
        "ingredients": ingredients,
        "instructions": [],
        "recipeIngredient": [{"note": i} for i in ingredients],
    }


def normalize_detail(body, source: str, catalog_name: str = "") -> dict | None:
    """A full /recipes/{id} answer, normalized like a web recipe and ready for
    preview, Cook, and the native store (flat ingredient lines with a parallel
    ``ingredient_sections`` list, which recipe_store keeps as headings)."""
    out = normalize_summary(body, source)
    if out is None:
        return None
    groups = _groups(body.get("ingredient_groups"))
    if groups:
        out["ingredients"] = [line for _, lines in groups for line in lines]
        out["ingredient_sections"] = [h for h, lines in groups for _ in lines]
    steps = _groups(body.get("instruction_groups"))
    out["instructions"] = ([line for _, lines in steps for line in lines]
                           if steps else _lines(body.get("instructions")))
    out["notes"] = _lines(body.get("notes"), 50)
    out["recipeIngredient"] = [{"note": i} for i in out["ingredients"]]
    # Credit rides into the saved recipe the way a community recipe's does.
    credit = []
    if out["author"]:
        credit.append(f"From {out['author']}")
    if catalog_name:
        credit.append(f"shared through {catalog_name}" if credit else f"From {catalog_name}")
    if credit:
        line = ", ".join(credit) + "."
        out["description"] = f"{out['description']}\n\n{line}".strip()
    if out["notes"]:
        out["description"] = (out["description"] + "\n\nNotes: "
                              + " ".join(out["notes"])).strip()
    return out


def search_matches(recipe: dict, query: str) -> bool:
    """Local fallback match for a cached listing (name, description and
    ingredient text, every word present)."""
    words = [w for w in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(w) > 1]
    if not words:
        return True
    hay = " ".join([recipe.get("name") or "", recipe.get("description") or "",
                    recipe.get("cuisine") or ""] + list(recipe.get("ingredients") or [])).lower()
    return all(w in hay for w in words)


# ── Network ───────────────────────────────────────────────────────────────────

def _headers(key: str) -> dict:
    h = {"Accept": "application/json",
         "User-Agent": f"PantryRaider/{APP_VERSION} (recipe catalog client)"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


async def _get(base: str, path: str, key: str, params: dict | None = None):
    url = base.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, verify=tls.verify(),
                                     follow_redirects=True,
                                     transport=_transport) as client:
            resp = await client.get(url, params=params, headers=_headers(key))
    except httpx.HTTPError as exc:
        raise CatalogError("The catalog could not be reached. Check the address "
                           "and that the site is up.") from exc
    if resp.status_code in (401, 403):
        raise CatalogError("The catalog turned down the access key. Check it, or "
                           "leave it blank for a public catalog.")
    if resp.status_code == 404:
        raise CatalogError("Nothing answered at that address. Check it is the "
                           "catalog's API address, not its home page.")
    if resp.status_code >= 400:
        raise CatalogError(f"The catalog answered with an error (HTTP {resp.status_code}).")
    try:
        return resp.json()
    except ValueError as exc:
        raise CatalogError("That address answered, but not with catalog data.") from exc


async def probe(url: str, key: str = "") -> dict:
    """Check one address speaks the catalog API; returns its /catalog info
    plus the normalized base URL. Raises CatalogError."""
    base = normalize_base_url(url)
    info = parse_info(await _get(base, "/catalog", key))
    info["base_url"] = base
    return info


async def _list_one(entry: dict) -> list[dict]:
    """Every recipe summary in one catalog (cached, capped). Fails soft."""
    cid = entry["id"]
    hit = _listing_cache.get(cid)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    source = source_for(cid)
    out: list[dict] = []
    try:
        offset = 0
        while offset < _MAX_RECIPES:
            body = await _get(entry["url"], "/recipes", key_for(cid),
                              {"limit": _PAGE_SIZE, "offset": offset})
            rows = body.get("recipes") if isinstance(body, dict) else None
            if not isinstance(rows, list) or not rows:
                break
            out += [r for r in (normalize_summary(x, source) for x in rows) if r]
            offset += len(rows)
            try:
                total = int(body.get("total"))
            except (TypeError, ValueError):
                total = None
            if (total is not None and offset >= total) or len(rows) < _PAGE_SIZE:
                break
    except CatalogError as exc:
        logger.info("recipe catalog %s unavailable: %s", cid, exc)
        if hit:
            return hit[1]   # a stale copy beats nothing while the site is down
        return []
    _listing_cache[cid] = (time.time(), out)
    return out


async def all_recipes() -> list[dict]:
    """Every recipe across the enabled catalogs, for stock matching."""
    lists = await asyncio.gather(*(_list_one(c) for c in enabled()))
    return [r for rows in lists for r in rows]


async def search(query: str, limit: int = 24) -> list[dict]:
    """Recipes across the enabled catalogs that match a name search. An
    empty query browses (the first recipes of each catalog)."""
    out: list[dict] = []
    for rows in await asyncio.gather(*(_list_one(c) for c in enabled())):
        out += [r for r in rows if search_matches(r, query)][:limit]
    return out


async def get_recipe(source: str, external_id: str) -> dict | None:
    """One full catalog recipe, or None when the catalog or recipe is gone."""
    entry = get(catalog_id_of(source))
    rid = str(external_id or "").strip()
    if not entry or not rid:
        return None
    try:
        body = await _get(entry["url"], "/recipes/" + quote(rid, safe=""),
                          key_for(entry["id"]))
    except CatalogError as exc:
        logger.info("recipe catalog %s detail %s unavailable: %s", entry["id"], rid, exc)
        return None
    return normalize_detail(body, source_for(entry["id"]), entry["name"])


def forget_cache(catalog_id: str | None = None) -> None:
    if catalog_id is None:
        _listing_cache.clear()
    else:
        _listing_cache.pop(catalog_id, None)
