"""Your own recipe databases (FoodAssistant-1sfs6): add, check, switch off,
and remove the recipe catalogs services/recipe_catalogs.py reads.

These live under /setup so only an admin can point the server at a new site
or hand it an access key. A key is stored apart from the list
(recipe_catalog_keys) and never comes back out: the list answers only whether
a catalog has one.
"""

import asyncio

from fastapi import HTTPException
from pydantic import BaseModel

from ...config import settings
from ...services import recipe_catalogs
from ._common import router

# One add or remove at a time, so two quick clicks never each read the same
# list and one drops the other's catalog.
_edit_lock = asyncio.Lock()


def _public_rows() -> list[dict]:
    return [{**c, "has_key": bool(recipe_catalogs.key_for(c["id"]))}
            for c in recipe_catalogs.configured()]


def _listing() -> dict:
    rows = recipe_catalogs.configured()
    taken = {c["url"] for c in rows} | {c["name"].lower() for c in rows}
    return {"catalogs": _public_rows(),
            "presets": [p for p in recipe_catalogs.PRESETS
                        if p["url"] not in taken and p["name"].lower() not in taken]}


async def _store(rows: list[dict], keys: dict) -> None:
    await asyncio.to_thread(settings.save, {"recipe_catalogs": rows,
                                            "recipe_catalog_keys": keys})


def _keys() -> dict:
    keys = settings.recipe_catalog_keys
    return dict(keys) if isinstance(keys, dict) else {}


class CatalogPayload(BaseModel):
    url: str
    name: str = ""
    # Blank keeps the stored key on an edit; clear_key removes it.
    api_key: str = ""
    clear_key: bool = False
    enabled: bool = True
    # Set to edit an existing catalog; empty adds a new one.
    id: str = ""


class CatalogTestPayload(BaseModel):
    url: str
    api_key: str = ""
    id: str = ""


class CatalogEnabledPayload(BaseModel):
    enabled: bool


@router.get("/recipe-catalogs")
async def list_recipe_catalogs():
    return _listing()


@router.post("/recipe-catalogs/test")
async def test_recipe_catalog(payload: CatalogTestPayload):
    key = payload.api_key.strip() or (recipe_catalogs.key_for(payload.id) if payload.id else "")
    try:
        info = await recipe_catalogs.probe(payload.url, key)
    except recipe_catalogs.CatalogError as e:
        return {"ok": False, "message": str(e)}
    count = info.get("recipe_count")
    name = info.get("name") or "The catalog"
    return {"ok": True, "info": info,
            "message": f"{name} answered" + (f" with {count} recipes." if count is not None else ".")}


@router.post("/recipe-catalogs")
async def save_recipe_catalog(payload: CatalogPayload):
    async with _edit_lock:
        rows = recipe_catalogs.configured()
        keys = _keys()
        existing = next((c for c in rows if c["id"] == payload.id), None) if payload.id else None
        if payload.id and existing is None:
            raise HTTPException(404, "That recipe database is no longer in your list.")
        key = payload.api_key.strip()
        if not key and existing and not payload.clear_key:
            key = keys.get(existing["id"], "")
        # Check the site speaks the catalog API before saving it, so a typo
        # never lands in the list as a source that silently finds nothing.
        try:
            info = await recipe_catalogs.probe(payload.url, key)
        except recipe_catalogs.CatalogError as e:
            raise HTTPException(422, str(e))
        base = info["base_url"]
        if any(c["url"] == base and c is not existing for c in rows):
            raise HTTPException(409, "That recipe database is already in your list.")
        name = (payload.name.strip() or info.get("name") or "Recipe database")[:80]
        if existing:
            cid = existing["id"]
            existing.update({"name": name, "url": base, "enabled": payload.enabled})
        else:
            cid = recipe_catalogs.make_id(name, {c["id"] for c in rows})
            rows.append({"id": cid, "name": name, "url": base, "enabled": payload.enabled})
        if key:
            keys[cid] = key
        else:
            keys.pop(cid, None)
        await _store(rows, keys)
        recipe_catalogs.forget_cache(cid)
    count = info.get("recipe_count")
    return {"ok": True, "id": cid, **_listing(),
            "message": f"{name} added" + (f", {count} recipes." if count is not None else ".")
                       if not existing else f"{name} saved."}


@router.post("/recipe-catalogs/{catalog_id}/enabled")
async def set_recipe_catalog_enabled(catalog_id: str, payload: CatalogEnabledPayload):
    async with _edit_lock:
        rows = recipe_catalogs.configured()
        entry = next((c for c in rows if c["id"] == catalog_id), None)
        if entry is None:
            raise HTTPException(404, "That recipe database is no longer in your list.")
        entry["enabled"] = payload.enabled
        await asyncio.to_thread(settings.save, {"recipe_catalogs": rows})
    return {"ok": True, **_listing()}


@router.delete("/recipe-catalogs/{catalog_id}")
async def delete_recipe_catalog(catalog_id: str):
    async with _edit_lock:
        rows = recipe_catalogs.configured()
        keep = [c for c in rows if c["id"] != catalog_id]
        if len(keep) == len(rows):
            raise HTTPException(404, "That recipe database is no longer in your list.")
        keys = _keys()
        keys.pop(catalog_id, None)
        await _store(keep, keys)
        recipe_catalogs.forget_cache(catalog_id)
    return {"ok": True, **_listing()}
