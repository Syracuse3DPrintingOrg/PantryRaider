"""Client for the Mealie recipe manager (https://mealie.io).

Mealie v2 renamed the group-scoped API routes from /api/groups/... to
/api/households/...; the client probes once and remembers which scheme the
server speaks, so both v1.x and v2.x installs work.
"""
import asyncio
import re
import time

import httpx

from ..config import settings


class MealieError(Exception):
    """Raised with Mealie's actual error message instead of a bare HTTP status."""


_client = httpx.AsyncClient(timeout=20.0)

# "households" (v2+) or "groups" (v1.x); probed on first scoped request
_scope: str | None = None

# Recipe-with-ingredients cache shared across requests: slug -> detail dict
_recipe_cache: dict[str, dict] = {}
_recipe_cache_at: float = 0.0
_RECIPE_CACHE_TTL = 600  # seconds


def reset_cache() -> None:
    global _scope, _recipe_cache, _recipe_cache_at
    _scope = None
    _recipe_cache = {}
    _recipe_cache_at = 0.0


class MealieClient:
    def __init__(self):
        self.base = settings.mealie_base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {settings.mealie_api_key}",
            "Content-Type": "application/json",
        }

    @property
    def configured(self) -> bool:
        return bool(settings.mealie_base_url and settings.mealie_api_key)

    async def _request(self, method: str, path: str, body=None, params=None):
        r = await _client.request(
            method, f"{self.base}/api{path}",
            headers=self.headers, json=body, params=params,
        )
        if r.status_code >= 400:
            detail = r.text[:300].strip() or r.reason_phrase
            raise MealieError(f"Mealie {r.status_code} on {path}: {detail}")
        return r.json() if r.content else {}

    async def _scoped(self, method: str, path: str, body=None, params=None):
        """Request under /households/ (v2) with automatic fallback to /groups/ (v1)."""
        global _scope
        if _scope:
            return await self._request(method, f"/{_scope}{path}", body, params)
        try:
            result = await self._request(method, f"/households{path}", body, params)
            _scope = "households"
            return result
        except MealieError as e:
            if not str(e).startswith("Mealie 404 "):
                raise
            result = await self._request(method, f"/groups{path}", body, params)
            _scope = "groups"
            return result

    # ── Connectivity ────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            await self._request("GET", "/users/self")
            return True
        except Exception:
            return False

    async def whoami(self) -> dict:
        return await self._request("GET", "/users/self")

    # ── Recipes ─────────────────────────────────────────────────────────────

    async def search_recipes(self, search: str = "", per_page: int = 50) -> list[dict]:
        data = await self._request("GET", "/recipes", params={
            "search": search or None,
            "perPage": per_page,
            "orderBy": "created_at",
            "orderDirection": "desc",
        })
        return data.get("items", [])

    async def get_recipe(self, slug: str) -> dict:
        return await self._request("GET", f"/recipes/{slug}")

    async def get_recipes_with_ingredients(self, limit: int = 200) -> list[dict]:
        """All recipes with their ingredient lists, cached for a few minutes.

        The list endpoint only returns summaries, so details are fetched
        concurrently the first time (and after the TTL expires).
        """
        global _recipe_cache, _recipe_cache_at
        if _recipe_cache and time.time() - _recipe_cache_at < _RECIPE_CACHE_TTL:
            return list(_recipe_cache.values())

        summaries = await self.search_recipes(per_page=limit)
        sem = asyncio.Semaphore(8)

        async def fetch(slug: str):
            async with sem:
                try:
                    return await self.get_recipe(slug)
                except Exception:
                    return None

        details = await asyncio.gather(*(fetch(s["slug"]) for s in summaries))
        _recipe_cache = {d["slug"]: d for d in details if d}
        _recipe_cache_at = time.time()
        return list(_recipe_cache.values())

    # ── Meal plan ───────────────────────────────────────────────────────────

    async def get_mealplan(self, start_date: str, end_date: str) -> list[dict]:
        data = await self._scoped("GET", "/mealplans", params={
            "start_date": start_date,
            "end_date": end_date,
            "perPage": 200,
        })
        return data.get("items", [])

    async def add_mealplan_entry(self, date: str, entry_type: str,
                                 recipe_id: str | None = None,
                                 title: str = "", text: str = "") -> dict:
        return await self._scoped("POST", "/mealplans", body={
            "date": date,
            "entryType": entry_type,
            "title": title,
            "text": text,
            "recipeId": recipe_id,
        })

    async def delete_mealplan_entry(self, entry_id: int) -> None:
        await self._scoped("DELETE", f"/mealplans/{entry_id}")

    # ── Shopping lists ──────────────────────────────────────────────────────

    async def get_shopping_lists(self) -> list[dict]:
        data = await self._scoped("GET", "/shopping/lists", params={"perPage": 50})
        return data.get("items", [])

    async def get_shopping_list(self, list_id: str) -> dict:
        return await self._scoped("GET", f"/shopping/lists/{list_id}")

    async def add_shopping_item(self, list_id: str, note: str,
                                quantity: float = 1.0) -> dict:
        return await self._scoped("POST", "/shopping/items", body={
            "shoppingListId": list_id,
            "note": note,
            "quantity": quantity,
            "isFood": False,
            "checked": False,
        })

    async def update_shopping_item(self, item_id: str, item: dict) -> dict:
        return await self._scoped("PUT", f"/shopping/items/{item_id}", body=item)

    async def delete_shopping_item(self, item_id: str) -> None:
        await self._scoped("DELETE", f"/shopping/items/{item_id}")


# ── Recipe ↔ inventory matching ──────────────────────────────────────────────

_STOP_WORDS = {
    "the", "and", "with", "for", "fresh", "large", "small", "medium", "cup",
    "cups", "tbsp", "tsp", "oz", "lb", "gram", "grams", "ml", "can", "cans",
    "package", "bag", "box", "jar", "bottle", "piece", "pieces", "of", "to",
    "or", "optional", "taste", "chopped", "diced", "sliced", "minced",
}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", (text or "").lower())
    return {w.rstrip("s") for w in words if len(w) >= 3 and w not in _STOP_WORDS}


def _ingredient_text(ing: dict) -> str:
    food = ing.get("food") or {}
    return food.get("name") or ing.get("display") or ing.get("note") or ""


def suggest_recipes(recipes: list[dict], stock: list[dict], top: int = 10) -> list[dict]:
    """Rank recipes by how much of the current inventory they use, with a
    strong boost for ingredients matching items that expire soon."""
    inv = []
    for s in stock:
        toks = _tokens(s["name"])
        if toks:
            inv.append({"name": s["name"], "tokens": toks,
                        "days_remaining": s.get("days_remaining")})

    results = []
    for r in recipes:
        ingredients = r.get("recipeIngredient") or []
        if not ingredients:
            continue
        matched, expiring_used = [], []
        for ing in ingredients:
            ing_toks = _tokens(_ingredient_text(ing))
            if not ing_toks:
                continue
            hit = next((s for s in inv if ing_toks & s["tokens"]), None)
            if hit:
                matched.append(_ingredient_text(ing).strip())
                d = hit["days_remaining"]
                if d is not None and d <= 5 and hit["name"] not in expiring_used:
                    expiring_used.append(hit["name"])
        if not matched:
            continue
        coverage = len(matched) / len(ingredients)
        score = coverage + 0.35 * len(expiring_used)
        results.append({
            "name": r.get("name"),
            "slug": r.get("slug"),
            "description": (r.get("description") or "")[:160],
            "total_ingredients": len(ingredients),
            "matched_ingredients": matched,
            "expiring_items_used": expiring_used,
            "coverage": round(coverage, 2),
            "score": round(score, 3),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top]
