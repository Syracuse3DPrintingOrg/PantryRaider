"""External recipe source: TheMealDB (https://www.themealdb.com).

Free API (test key "1" is officially open for non-commercial use); full
recipes with ingredients and instructions, searchable by ingredient.
Results are normalized to the same shape Mealie recipes use internally so
the tier classifier can treat both sources identically.
"""
import asyncio
import re
import time

import httpx

_BASE = "https://www.themealdb.com/api/json/v1/1"

_client = httpx.AsyncClient(timeout=15.0)

# ingredient-query -> list of meal ids; meal id -> normalized recipe
_search_cache: dict[str, list[str]] = {}
_meal_cache: dict[str, dict] = {}
_cache_at: float = 0.0
_CACHE_TTL = 3600  # seconds


def _expire_cache() -> None:
    global _search_cache, _meal_cache, _cache_at
    if time.time() - _cache_at > _CACHE_TTL:
        _search_cache = {}
        _meal_cache = {}


async def _filter_by_ingredient(ingredient: str) -> list[str]:
    """Meal ids whose recipe uses `ingredient` (TheMealDB wants underscores)."""
    q = re.sub(r"\s+", "_", ingredient.strip().lower())
    if q in _search_cache:
        return _search_cache[q]
    try:
        r = await _client.get(f"{_BASE}/filter.php", params={"i": q})
        r.raise_for_status()
        meals = (r.json() or {}).get("meals") or []
    except Exception:
        meals = []
    ids = [m["idMeal"] for m in meals if m.get("idMeal")]
    _search_cache[q] = ids
    return ids


async def _lookup(meal_id: str) -> dict | None:
    if meal_id in _meal_cache:
        return _meal_cache[meal_id]
    try:
        r = await _client.get(f"{_BASE}/lookup.php", params={"i": meal_id})
        r.raise_for_status()
        meals = (r.json() or {}).get("meals") or []
    except Exception:
        return None
    if not meals:
        return None
    recipe = _normalize(meals[0])
    _meal_cache[meal_id] = recipe
    return recipe


def _normalize(meal: dict) -> dict:
    """TheMealDB's strIngredient1..20 / strMeasure1..20 -> our recipe shape."""
    ingredients = []
    for n in range(1, 21):
        ing = (meal.get(f"strIngredient{n}") or "").strip()
        if not ing:
            continue
        measure = (meal.get(f"strMeasure{n}") or "").strip()
        ingredients.append(f"{measure} {ing}".strip())

    instructions = [
        s.strip() for s in re.split(r"[\r\n]+", meal.get("strInstructions") or "")
        if s.strip()
    ]

    return {
        "name": meal.get("strMeal"),
        "slug": None,                       # not in Mealie (yet)
        "external_id": meal.get("idMeal"),
        "source": "themealdb",
        "description": ", ".join(filter(None, [meal.get("strArea"), meal.get("strCategory")])),
        "servings": "",
        "total_time": "",
        "image": meal.get("strMealThumb"),
        "source_url": meal.get("strSource") or f"https://www.themealdb.com/meal/{meal.get('idMeal')}",
        "ingredients": ingredients,
        "instructions": instructions,
        # tier classifier reads Mealie's field name
        "recipeIngredient": [{"note": i} for i in ingredients],
    }


async def find_recipes_for_ingredients(ingredients: list[str], limit: int = 12) -> list[dict]:
    """Recipes from TheMealDB that use any of the given stock ingredients.

    Queries each ingredient in parallel; meals matching several of them
    bubble up first. Returns normalized full recipes.
    """
    _expire_cache()
    queries = [i for i in ingredients[:8] if i and len(i) >= 3]
    if not queries:
        return []

    id_lists = await asyncio.gather(*(_filter_by_ingredient(q) for q in queries))

    hit_count: dict[str, int] = {}
    for ids in id_lists:
        for mid in ids[:25]:
            hit_count[mid] = hit_count.get(mid, 0) + 1
    ranked = sorted(hit_count, key=lambda m: hit_count[m], reverse=True)[:limit]

    recipes = await asyncio.gather(*(_lookup(mid) for mid in ranked))
    global _cache_at
    if not _cache_at:
        _cache_at = time.time()
    return [r for r in recipes if r]


async def get_external_recipe(meal_id: str) -> dict | None:
    """Full normalized recipe for one TheMealDB id (for import into Mealie)."""
    _expire_cache()
    return await _lookup(meal_id)
