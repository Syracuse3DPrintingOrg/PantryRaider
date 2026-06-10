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
    global _scope
    _scope = None
    _invalidate_recipe_cache()


def _invalidate_recipe_cache() -> None:
    global _recipe_cache, _recipe_cache_at
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

    async def create_recipe_from_url(self, url: str) -> str:
        """Use Mealie's built-in scraper (recipe-scrapers) to import a URL.

        Returns the new recipe slug. Path moved between Mealie versions.
        """
        body = {"url": url, "includeTags": False}
        try:
            result = await self._request("POST", "/recipes/create/url", body=body)
        except MealieError as e:
            if not str(e).startswith("Mealie 404 "):
                raise
            result = await self._request("POST", "/recipes/create-url", body=body)
        _invalidate_recipe_cache()
        return result if isinstance(result, str) else result.get("slug", "")

    async def create_recipe(self, data: dict) -> str:
        """Create a recipe from structured fields and return its slug.

        Mealie's POST only takes a name; everything else goes in a PATCH.
        """
        slug = await self._request("POST", "/recipes", body={"name": data["name"]})
        if not isinstance(slug, str):
            slug = slug.get("slug", "")
        patch = {
            "description": data.get("description") or "",
            "recipeYield": data.get("servings") or "",
            "totalTime": data.get("total_time") or "",
            "recipeIngredient": [{"note": i} for i in data.get("ingredients") or [] if i.strip()],
            "recipeInstructions": [{"text": s} for s in data.get("instructions") or [] if s.strip()],
        }
        await self._request("PATCH", f"/recipes/{slug}", body=patch)
        _invalidate_recipe_cache()
        return slug

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


# Things assumed on hand even if not tracked in inventory.
# Tier 2 ("with pantry staples") recipes may use these freely.
_STAPLE_TOKENS = {
    "egg", "butter", "flour", "sugar", "salt", "pepper", "oil", "olive",
    "milk", "garlic", "onion", "vinegar", "baking", "soda", "vanilla",
    "honey", "mustard", "ketchup", "mayonnaise", "mayo", "soy",
    # common dried spices
    "cumin", "paprika", "oregano", "cinnamon", "nutmeg", "thyme",
    "rosemary", "cayenne", "turmeric", "curry",
}

# Descriptor words allowed alongside staples ("brown sugar", "ground pepper",
# "vanilla extract") without making e.g. "coconut milk" count as milk: an
# ingredient is a staple only if ALL its tokens are staples or descriptors.
_STAPLE_GLUE_TOKENS = {
    "sauce", "powder", "ground", "white", "brown", "black", "red", "dried",
    "extract", "granulated", "unsalted", "salted", "vegetable", "canola",
    "sunflower", "virgin", "extra", "light", "dark", "sea", "kosher", "whole",
    "skim", "plain", "all", "purpose", "self", "raising", "clove", "cloves",
}

# Water (in any temperature) never counts against a recipe.
_FREEBIE_TOKENS = {"water", "ice", "boiling", "warm", "cold", "hot", "tap"}


def _is_perishable(stock_item: dict) -> bool:
    """Refrigerated items and anything expiring within two weeks."""
    if stock_item.get("storage_bucket") == "refrigerated":
        return True
    d = stock_item.get("days_remaining")
    return d is not None and d <= 14


def classify_recipes(recipes: list[dict], stock: list[dict],
                     top_per_tier: int = 8) -> dict[str, list[dict]]:
    """Sort recipes into three cookability tiers against current inventory.

    ready    — every ingredient matches an item in stock
    staples  — in stock + common pantry staples (eggs, butter, flour, ...)
    shopping — uses at least one perishable stock item but needs a shop run

    Recipes that need shopping without using any perishables are dropped:
    they don't help eat down the inventory.
    """
    inv = []
    for s in stock:
        toks = _tokens(s["name"])
        if toks:
            inv.append({"name": s["name"], "tokens": toks,
                        "days_remaining": s.get("days_remaining"),
                        "perishable": _is_perishable(s)})

    tiers: dict[str, list[dict]] = {"ready": [], "staples": [], "shopping": []}
    for r in recipes:
        ingredients = r.get("recipeIngredient") or []
        if not ingredients:
            continue
        matched, staples, unmatched, expiring_used = [], [], [], []
        uses_perishable = False
        for ing in ingredients:
            text = _ingredient_text(ing).strip()
            ing_toks = _tokens(text)
            if not text or not ing_toks or ing_toks <= _FREEBIE_TOKENS:
                continue
            hit = next((s for s in inv if ing_toks & s["tokens"]), None)
            if hit:
                matched.append(text)
                if hit["perishable"]:
                    uses_perishable = True
                d = hit["days_remaining"]
                if d is not None and d <= 5 and hit["name"] not in expiring_used:
                    expiring_used.append(hit["name"])
            elif (ing_toks & _STAPLE_TOKENS
                  and ing_toks <= (_STAPLE_TOKENS | _STAPLE_GLUE_TOKENS | _FREEBIE_TOKENS)):
                staples.append(text)
            else:
                unmatched.append(text)

        if not matched:
            continue
        if not unmatched and not staples:
            tier = "ready"
        elif not unmatched:
            tier = "staples"
        elif uses_perishable:
            tier = "shopping"
        else:
            continue

        coverage = len(matched) / max(len(matched) + len(staples) + len(unmatched), 1)
        score = coverage + 0.35 * len(expiring_used)
        tiers[tier].append({
            "name": r.get("name"),
            "slug": r.get("slug"),
            "id": r.get("id"),
            "source": r.get("source", "mealie"),
            "external_id": r.get("external_id"),
            "image": r.get("image"),
            "description": (r.get("description") or "")[:160],
            "total_ingredients": len(ingredients),
            "matched_ingredients": matched,
            "staple_ingredients": staples,
            "unmatched_ingredients": unmatched,
            "expiring_items_used": expiring_used,
            "coverage": round(coverage, 2),
            "score": round(score, 3),
        })

    for tier in tiers.values():
        tier.sort(key=lambda x: (len(x["expiring_items_used"]), x["score"]), reverse=True)
    return {k: v[:top_per_tier] for k, v in tiers.items()}
