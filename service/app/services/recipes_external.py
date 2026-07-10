"""External recipe sources for inventory-based suggestions.

Selected via settings.recipe_source:
  themealdb  : free public API (test key "1"); premium supporter key
                unlocks the full catalog and removes rate limits
  spoonacular: large catalog, requires an API key (free tier ~150 pts/day,
                so results are cached aggressively)
  off        : no external suggestions

Results are normalized to the same shape Mealie recipes use internally so
the tier classifier treats every source identically.
"""
import asyncio
import re
import time

import httpx

from ..config import settings
from .mealie import _PHRASE_MODIFIERS, _STOP_WORDS

_client = httpx.AsyncClient(timeout=15.0)


# ── Ingredient-name normalization ──────────────────────────────────────────────
#
# Grocy stock names are branded / sized / descriptor-laden ("Baby Spinach",
# "Chicken Breast 1lb", "Organic Whole Milk"). External catalogs (TheMealDB's
# filter.php especially) only match canonical single-ingredient terms, so we
# reduce a stock name to its core ingredient word(s) first.
#
# Preparation / processing / marketing modifiers that describe how a retail
# item is cut, cured, or sold but not what it fundamentally is. Stripped so a
# name like "Shredded Swiss Cheese" reduces toward "cheese" and "Pickled Red
# Onions" toward "onions". Data-driven and easy to extend.
_PREP_MODIFIERS = {
    "pickled", "shredded", "grated", "crumbled", "sliced", "diced", "cubed",
    "minced", "chopped", "ground", "crushed", "mashed", "shaved", "julienned",
    "roasted", "toasted", "smoked", "cured", "brined", "marinated", "seasoned",
    "breaded", "peeled", "shelled", "pitted", "seedless", "boneless",
    "skinless", "trimmed", "halved", "quartered",
    "fresh", "frozen", "canned", "jarred", "bottled", "dried", "dehydrated",
    "cooked", "uncooked", "raw", "ripe",
    "unsalted", "salted", "unsweetened", "sweetened",
    "reduced", "low", "nonfat", "fat", "free", "lite", "light",
    "organic", "natural",
}

# Cheese-variety qualifiers dropped ONLY when the word "cheese" is also present,
# so "Shredded Swiss Cheese" -> "cheese" (matchable) while a variety name that
# stands on its own (e.g. bare "feta") is left intact for the head-noun retry.
_CHEESE_VARIETIES = {
    "swiss", "cheddar", "mozzarella", "provolone", "gouda", "gruyere",
    "parmesan", "parmigiano", "reggiano", "romano", "asiago", "havarti",
    "muenster", "munster", "colby", "monterey", "jack", "pepper", "american",
    "feta", "brie", "camembert", "ricotta", "mascarpone", "gorgonzola",
    "blue", "cream", "cottage", "string", "goat", "sharp", "mild", "aged",
}

# Reuses the descriptor/stop-word sets already maintained in mealie.py and
# extends them with brand/size/packaging words specific to retail product names.
_NOISE_WORDS = (
    _STOP_WORDS
    | _PHRASE_MODIFIERS
    | _PREP_MODIFIERS
    | {
        # quality / marketing descriptors
        "premium", "range", "grass", "fed",
        "lean", "baby", "mini", "jumbo", "value", "family", "size",
        "sized", "select", "choice", "grade", "all", "purpose", "pure",
        "skin", "bone", "in", "on",
        "skim", "thick", "thin", "cut",
        "cuts", "style", "homestyle", "classic", "original", "deluxe",
        # packaging / quantity words
        "pack", "packs", "packet", "carton", "tub", "tin", "tray", "loaf",
        "bunch", "head", "stick", "sticks", "fillet", "fillets",
        "count", "ct", "ea", "each", "qty", "approx",
        # units (single-letter handled by length filter)
        "kg", "mg", "lbs", "pound", "pounds", "ounce", "ounces", "fl",
        "liter", "litre", "liters", "litres", "quart", "quarts", "pint",
        "pints", "gallon", "dozen",
    }
)


def _core_ingredient(name: str) -> str:
    """Reduce a Grocy stock name to its core ingredient term(s).

    Strips brand/size/descriptor/packaging/preparation words, units, and
    embedded quantities ("1lb", "500g", "2 x"), then drops trailing noise so a
    name like "Boneless Skinless Chicken Thighs" -> "chicken thighs" and
    "Shredded Swiss Cheese" -> "cheese".

    A retail name often reads "primary ingredient with garnish" ("Pickled Red
    Onions with Peppers"); the trailing "with ..." clause is a secondary item,
    so it is dropped and the primary noun ("onions") survives.

    Pure and deterministic: returns a space-separated lowercase string
    (possibly empty if every token was noise). Callers convert spaces to
    underscores for TheMealDB's filter taxonomy.
    """
    text = (name or "").lower()
    # "X with Y" names the primary ingredient first; drop the trailing garnish.
    text = text.split(" with ")[0]
    # drop embedded quantities glued to units: "1lb", "500g", "12oz"
    text = re.sub(r"\b\d+(?:\.\d+)?\s*[a-z]+\b", " ", text)
    # drop bare numbers and the multiplier "x"
    text = re.sub(r"\b\d+(?:\.\d+)?\b", " ", text)
    words = re.findall(r"[a-z]+", text)
    # Cheese-variety qualifiers only count as noise when "cheese" is present.
    has_cheese = "cheese" in words
    core = []
    for w in words:
        if len(w) < 3 or w == "x" or w in _NOISE_WORDS:
            continue
        if has_cheese and w != "cheese" and w in _CHEESE_VARIETIES:
            continue
        core.append(w)
    return " ".join(core)

# (source, query/id) keyed caches, expired together. Per-process scope is
# fine here: the cached data is read-only third-party recipe listings, so the
# worst a second worker could see is an independently fetched copy.
_search_cache: dict[tuple, list[str]] = {}
_recipe_cache: dict[tuple, dict] = {}
_cache_at: float = 0.0
_CACHE_TTL = 3600  # seconds


def _expire_cache() -> None:
    global _search_cache, _recipe_cache, _cache_at
    if time.time() - _cache_at > _CACHE_TTL:
        _search_cache = {}
        _recipe_cache = {}


def _touch_cache() -> None:
    global _cache_at
    if not _cache_at:
        _cache_at = time.time()


# ── TheMealDB ─────────────────────────────────────────────────────────────────

def _mealdb_base() -> str:
    key = settings.themealdb_api_key.strip() or "1"
    return f"https://www.themealdb.com/api/json/v1/{key}"


async def _mealdb_filter(ingredient: str) -> list[str]:
    q = re.sub(r"\s+", "_", ingredient.strip().lower())
    ck = ("themealdb", q)
    if ck in _search_cache:
        return _search_cache[ck]
    try:
        r = await _client.get(f"{_mealdb_base()}/filter.php", params={"i": q})
        r.raise_for_status()
        meals = (r.json() or {}).get("meals") or []
    except Exception:
        meals = []
    ids = [m["idMeal"] for m in meals if m.get("idMeal")]
    _search_cache[ck] = ids
    return ids


async def _mealdb_lookup(meal_id: str) -> dict | None:
    ck = ("themealdb", meal_id)
    if ck in _recipe_cache:
        return _recipe_cache[ck]
    try:
        r = await _client.get(f"{_mealdb_base()}/lookup.php", params={"i": meal_id})
        r.raise_for_status()
        meals = (r.json() or {}).get("meals") or []
    except Exception:
        return None
    if not meals:
        return None
    recipe = _mealdb_normalize(meals[0])
    _recipe_cache[ck] = recipe
    return recipe


def _mealdb_normalize(meal: dict) -> dict:
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
    return _normalized(
        name=meal.get("strMeal"),
        external_id=str(meal.get("idMeal")),
        source="themealdb",
        description=", ".join(filter(None, [meal.get("strArea"), meal.get("strCategory")])),
        image=meal.get("strMealThumb"),
        source_url=meal.get("strSource") or f"https://www.themealdb.com/meal/{meal.get('idMeal')}",
        ingredients=ingredients,
        instructions=instructions,
        cuisine=(meal.get("strArea") or "").strip(),
    )


async def _mealdb_search_name(query: str, limit: int) -> list[dict]:
    ck = ("themealdb-name", query.lower())
    if ck in _recipe_cache:
        return _recipe_cache[ck]
    try:
        r = await _client.get(f"{_mealdb_base()}/search.php", params={"s": query})
        r.raise_for_status()
        meals = (r.json() or {}).get("meals") or []
    except Exception:
        meals = []
    recipes = [_mealdb_normalize(m) for m in meals[:limit]]
    _recipe_cache[ck] = recipes
    return recipes


async def _mealdb_filter_term(term: str) -> list[str]:
    """filter.php for a reduced term, falling back to its head noun.

    TheMealDB's filter.php matches ONE ingredient from its fixed taxonomy, so a
    multi-word core ("shredded swiss cheese" -> already reduced to "cheese", but
    "chicken breast" or "red onions") often returns nothing as a phrase. When
    the full term misses we retry the head noun (the last word, e.g.
    "red onions" -> "onions") and then the remaining words, taking the first
    that yields hits. Every attempt is cached in _mealdb_filter, so words shared
    across many stock items (cheese, onions, chicken) are fetched only once.
    """
    ids = await _mealdb_filter(term)
    if ids:
        return ids
    words = term.split()
    if len(words) < 2:
        return ids
    # Head noun first (TheMealDB taxonomy is the base food word), then the rest.
    for w in [words[-1], *words[:-1]]:
        if len(w) < 3:
            continue
        ids = await _mealdb_filter(w)
        if ids:
            return ids
    return []


# Cap on distinct query terms sent to TheMealDB per Cook request. Widened past
# the first few perishables so a matchable staple (chicken, onion, egg) is still
# reached when the top perishables are all specialty items, while keeping the
# number of (cached) filter.php calls bounded.
_MAX_MEALDB_QUERIES = 12


async def _mealdb_find(ingredients: list[str], limit: int) -> list[dict]:
    # Reduce branded/sized stock names to canonical ingredient terms so
    # filter.php (which only matches its single-ingredient taxonomy) gets hits.
    # Scan the whole stock (deduped, capped) rather than only the first items,
    # so a common staple deeper in the list still reaches the taxonomy.
    seen: set[str] = set()
    queries: list[str] = []
    for raw in ingredients:
        core = _core_ingredient(raw)
        if len(core) >= 3 and core not in seen:
            seen.add(core)
            queries.append(core)
        if len(queries) >= _MAX_MEALDB_QUERIES:
            break
    if not queries:
        return []
    id_lists = await asyncio.gather(*(_mealdb_filter_term(q) for q in queries))
    hit_count: dict[str, int] = {}
    for ids in id_lists:
        for mid in ids[:25]:
            hit_count[mid] = hit_count.get(mid, 0) + 1
    ranked = sorted(hit_count, key=lambda m: hit_count[m], reverse=True)[:limit]
    recipes = await asyncio.gather(*(_mealdb_lookup(mid) for mid in ranked))
    return [r for r in recipes if r]


# ── Spoonacular ───────────────────────────────────────────────────────────────

_SPOON_BASE = "https://api.spoonacular.com"


# Cook-page diet labels → Spoonacular query params. `diet` accepts one value;
# `intolerances` is additive. Labels with no Spoonacular equivalent (Low Carb)
# are dropped silently.
_SPOON_DIETS = {
    "vegan": "vegan",
    "vegetarian": "vegetarian",
    "keto": "ketogenic",
    "pescatarian": "pescetarian",
    "gluten free": "gluten free",
}
_SPOON_INTOLERANCES = {
    "dairy free": "dairy",
    "gluten free": "gluten",
    "nut free": "tree nut,peanut",
}


def _spoon_diet_params(dietary: str) -> dict:
    """Translate Cook-page diet labels into Spoonacular diet/intolerances params."""
    labels = [d.strip().lower() for d in dietary.split(",") if d.strip()]
    diets = [_SPOON_DIETS[d] for d in labels if d in _SPOON_DIETS]
    intol = [_SPOON_INTOLERANCES[d] for d in labels if d in _SPOON_INTOLERANCES]
    params: dict = {}
    if diets:
        params["diet"] = diets[0]
    if intol:
        params["intolerances"] = ",".join(intol)
    return params


async def _spoon_find(
    ingredients: list[str], limit: int, dietary: str = "", max_time: int = 0,
    cuisine: str = "",
) -> list[dict]:
    """Find recipes by stock ingredients, then fetch details.

    With no diet/time/cuisine filter this uses findByIngredients (ranked to
    minimize missing items). When a filter is active it switches to
    complexSearch, the only endpoint that honours diet/intolerances/
    maxReadyTime/cuisine alongside includeIngredients. Each call costs API
    points, so phases are cached.
    """
    # Normalize stock names to core terms (Spoonacular tolerates noise but
    # matches more recipes against clean ingredient words); dedupe + cap at 6.
    seen: set[str] = set()
    terms: list[str] = []
    for raw in ingredients:
        core = _core_ingredient(raw) or (raw or "").strip().lower()
        if core and core not in seen:
            seen.add(core)
            terms.append(core)
        if len(terms) >= 6:
            break
    query = ",".join(terms)
    if not query:
        return []

    diet_params = _spoon_diet_params(dietary)
    # Spoonacular's `cuisine` param is OR-combined; pass the selected labels as-is.
    cuisines = ",".join(c.strip() for c in cuisine.split(",") if c.strip())
    use_complex = bool(diet_params) or max_time > 0 or bool(cuisines)
    ck = ("spoonacular", query, dietary, max_time, cuisines)
    if ck in _search_cache:
        ids = _search_cache[ck]
    elif use_complex:
        params = {
            "includeIngredients": query,
            "number": limit,
            "sort": "min-missing-ingredients",
            "ignorePantry": "true",
            "apiKey": settings.spoonacular_api_key,
            **diet_params,
        }
        if max_time > 0:
            params["maxReadyTime"] = max_time
        if cuisines:
            params["cuisine"] = cuisines
        try:
            r = await _client.get(f"{_SPOON_BASE}/recipes/complexSearch", params=params)
            r.raise_for_status()
            ids = [str(m["id"]) for m in (r.json() or {}).get("results", [])]
        except Exception:
            ids = []
        _search_cache[ck] = ids
    else:
        try:
            r = await _client.get(f"{_SPOON_BASE}/recipes/findByIngredients", params={
                "ingredients": query,
                "number": limit,
                "ranking": 2,          # minimize missing ingredients
                "ignorePantry": "true",
                "apiKey": settings.spoonacular_api_key,
            })
            r.raise_for_status()
            ids = [str(m["id"]) for m in r.json() or []]
        except Exception:
            ids = []
        _search_cache[ck] = ids

    recipes = await asyncio.gather(*(_spoon_lookup(rid) for rid in ids))
    return [r for r in recipes if r]


async def _spoon_search_name(query: str, limit: int) -> list[dict]:
    """complexSearch returns id/title/image only: enough for a result list.
    Full details are fetched on import via get_external_recipe."""
    ck = ("spoonacular-name", query.lower())
    if ck in _recipe_cache:
        return _recipe_cache[ck]
    try:
        r = await _client.get(f"{_SPOON_BASE}/recipes/complexSearch", params={
            "query": query,
            "number": limit,
            "apiKey": settings.spoonacular_api_key,
        })
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
    except Exception:
        results = []
    recipes = [_normalized(
        name=m.get("title"),
        external_id=str(m.get("id")),
        source="spoonacular",
        description="",
        image=m.get("image"),
        source_url="",
        ingredients=[],
        instructions=[],
    ) for m in results if m.get("id")]
    _recipe_cache[ck] = recipes
    return recipes


async def _spoon_lookup(recipe_id: str) -> dict | None:
    ck = ("spoonacular", recipe_id)
    if ck in _recipe_cache:
        return _recipe_cache[ck]
    try:
        r = await _client.get(f"{_SPOON_BASE}/recipes/{recipe_id}/information", params={
            "includeNutrition": "false",
            "apiKey": settings.spoonacular_api_key,
        })
        r.raise_for_status()
        info = r.json()
    except Exception:
        return None

    steps = []
    for block in info.get("analyzedInstructions") or []:
        steps += [s["step"].strip() for s in block.get("steps") or [] if s.get("step")]
    if not steps and info.get("instructions"):
        text = re.sub(r"(?s)<[^>]+>", " ", info["instructions"])
        steps = [s.strip() for s in re.split(r"[\r\n]+", text) if s.strip()]

    recipe = _normalized(
        name=info.get("title"),
        external_id=str(info.get("id")),
        source="spoonacular",
        description=f"Ready in {info['readyInMinutes']} min" if info.get("readyInMinutes") else "",
        image=info.get("image"),
        source_url=info.get("sourceUrl") or "",
        ingredients=[i.get("original", "").strip() for i in info.get("extendedIngredients") or [] if i.get("original")],
        instructions=steps,
        servings=str(info.get("servings") or ""),
        total_time=f"{info['readyInMinutes']} minutes" if info.get("readyInMinutes") else "",
    )
    _recipe_cache[ck] = recipe
    return recipe


# ── Common interface ──────────────────────────────────────────────────────────

def _normalized(name, external_id, source, description, image, source_url,
                ingredients, instructions, servings="", total_time="", cuisine="") -> dict:
    return {
        "name": name,
        "slug": None,                       # not in Mealie (yet)
        "external_id": external_id,
        "source": source,
        "description": description,
        "servings": servings,
        "total_time": total_time,
        "image": image,
        "source_url": source_url,
        "cuisine": cuisine,                 # source's region/area, for post-filtering
        "ingredients": ingredients,
        "instructions": instructions,
        # tier classifier reads Mealie's field name
        "recipeIngredient": [{"note": i} for i in ingredients],
    }


# Animal products used to approximate vegan/vegetarian filtering for sources
# (TheMealDB) that expose no diet metadata. Matched as substrings of ingredient
# text, so "chicken breast", "ground beef", "parmesan cheese" all trigger.
_NON_VEGETARIAN = (
    "chicken", "beef", "pork", "bacon", "ham", "sausage", "turkey", "lamb",
    "veal", "duck", "fish", "salmon", "tuna", "shrimp", "prawn", "crab",
    "lobster", "anchovy", "anchovies", "gelatin", "meat",
)
_NON_VEGAN = _NON_VEGETARIAN + (
    "egg", "milk", "butter", "cheese", "cream", "yogurt", "yoghurt", "honey",
    "mayonnaise", "ghee",
)


def _violates_diet(recipe: dict, banned: tuple) -> bool:
    text = " ".join(
        (i.get("note") or i.get("name") or "") if isinstance(i, dict) else str(i)
        for i in (recipe.get("recipeIngredient") or recipe.get("ingredients") or [])
    ).lower()
    return any(b in text for b in banned)


def _filter_by_diet(recipes: list[dict], dietary: str) -> list[dict]:
    """Approximate vegan/vegetarian filtering for sources without diet metadata."""
    labels = {d.strip().lower() for d in dietary.split(",") if d.strip()}
    banned: tuple = ()
    if "vegan" in labels:
        banned = _NON_VEGAN
    elif "vegetarian" in labels:
        banned = _NON_VEGETARIAN
    if not banned:
        return recipes
    return [r for r in recipes if not _violates_diet(r, banned)]


# Cuisine labels offered on the Cook page. Broad regions expand to the set of
# TheMealDB "areas" they cover (TheMealDB tags recipes by country, not region),
# so picking "Asian" still post-filters mealdb results sensibly. Spoonacular
# accepts these labels directly in its `cuisine` param.
_CUISINE_AREAS = {
    "asian": {"chinese", "japanese", "thai", "vietnamese", "malaysian",
              "filipino", "indian", "korean"},
    "mediterranean": {"greek", "italian", "spanish", "turkish", "moroccan",
                      "egyptian", "tunisian"},
    "european": {"french", "italian", "spanish", "greek", "british", "irish",
                 "polish", "portuguese", "dutch", "croatian", "russian"},
    "latin american": {"mexican", "jamaican", "uruguayan"},
    "middle eastern": {"turkish", "egyptian", "moroccan", "tunisian"},
    # Specific cuisines map to the matching area one-to-one.
    "italian": {"italian"}, "french": {"french"}, "greek": {"greek"},
    "spanish": {"spanish"}, "chinese": {"chinese"}, "japanese": {"japanese"},
    "thai": {"thai"}, "vietnamese": {"vietnamese"}, "indian": {"indian"},
    "mexican": {"mexican"}, "british": {"british"}, "moroccan": {"moroccan"},
    "turkish": {"turkish"}, "american": {"american"},
}


def _filter_by_cuisine(recipes: list[dict], cuisine: str) -> list[dict]:
    """Keep recipes whose source area matches any selected cuisine/region.

    Used for TheMealDB (Spoonacular filters server-side). Recipes with no area
    tag are kept, since absence isn't evidence of a mismatch."""
    labels = [c.strip().lower() for c in cuisine.split(",") if c.strip()]
    if not labels:
        return recipes
    wanted: set[str] = set()
    for lab in labels:
        wanted |= _CUISINE_AREAS.get(lab, {lab})
    out = []
    for r in recipes:
        area = (r.get("cuisine") or "").strip().lower()
        if not area or area in wanted:
            out.append(r)
    return out


def _native_cuisine_haystack(recipe: dict) -> str:
    """Free text to search for a cuisine/region hint on a native recipe: its
    name, description, and any tags/categories it was imported or saved with.
    Native recipes have no structured "area" field like the external sources
    do, so this is the closest thing to one."""
    parts = [recipe.get("name") or "", recipe.get("description") or ""]
    parts += [str(t) for t in (recipe.get("tags") or [])]
    parts += [str(c) for c in (recipe.get("categories") or [])]
    return " ".join(parts).lower()


def filter_native_by_cuisine(recipes: list[dict], cuisine: str) -> list[dict]:
    """Keep native/local recipes that actually mention the selected cuisine or
    region in their tags, categories, name, or description.

    Deliberately the opposite of ``_filter_by_cuisine``'s leniency: a native
    recipe with no cuisine hint at all is DROPPED rather than kept. Otherwise
    every recipe in the user's library (almost none of which are cuisine
    tagged) would pass every cuisine question unchanged, which is the bug
    this guards against (FoodAssistant-nomr): the Cook wizard's guided finder
    showed local recipes regardless of whether they matched the cuisine
    question."""
    labels = [c.strip().lower() for c in cuisine.split(",") if c.strip()]
    if not labels:
        return recipes
    wanted: set[str] = set()
    for lab in labels:
        wanted |= _CUISINE_AREAS.get(lab, {lab})
        wanted.add(lab)
    out = []
    for r in recipes:
        hay = _native_cuisine_haystack(r)
        if any(w in hay for w in wanted):
            out.append(r)
    return out


def filter_native_recipes(recipes: list[dict], *, cuisine: str = "",
                          dietary: str = "") -> list[dict]:
    """Narrow the user's own recipe library to the ones that actually match
    the wizard's cuisine/diet answers.

    ``/mealie/suggest`` used to hand the native library straight to
    ``classify_recipes`` with no regard for the cuisine/dietary query params,
    so the Cook wizard's guided finder showed every local recipe regardless
    of the answers, and (being classified first) those unrelated recipes sat
    above the external results that *did* honor the filters
    (FoodAssistant-nomr). Reuses the same diet violation scan the external
    source uses (ingredient text, so it works on any recipe shape) plus a
    stricter, tag-based cuisine check since native recipes carry no area
    field."""
    out = _filter_by_diet(recipes, dietary)
    return filter_native_by_cuisine(out, cuisine)


async def find_recipes_for_ingredients(
    ingredients: list[str], limit: int = 12, dietary: str = "",
    max_time: int = 0, cuisine: str = "",
) -> list[dict]:
    """External recipes using the given stock ingredients, per settings source.

    ``dietary`` and ``cuisine`` are comma-separated Cook-page labels. Spoonacular
    filters natively where it can; TheMealDB has no diet/cuisine API, so
    vegan/vegetarian and cuisine are approximated by post-filtering (ingredient
    scan for diet, ``strArea`` match for cuisine)."""
    _expire_cache()
    _touch_cache()
    source = settings.recipe_source
    if source == "off":
        return []
    if source == "spoonacular" and settings.spoonacular_api_key:
        return await _spoon_find(ingredients, limit, dietary=dietary,
                                 max_time=max_time, cuisine=cuisine)
    recipes = await _mealdb_find(ingredients, limit)
    recipes = _filter_by_diet(recipes, dietary)
    return _filter_by_cuisine(recipes, cuisine)


async def search_recipes_by_name(query: str, limit: int = 12) -> list[dict]:
    """External recipes matching a name search, per settings source."""
    _expire_cache()
    _touch_cache()
    query = query.strip()
    if not query:
        return []
    source = settings.recipe_source
    if source == "off":
        return []
    if source == "spoonacular" and settings.spoonacular_api_key:
        return await _spoon_search_name(query, limit)
    return await _mealdb_search_name(query, limit)


async def get_external_recipe(external_id: str, source: str = "themealdb") -> dict | None:
    """Full normalized recipe by id (for import into Mealie)."""
    _expire_cache()
    _touch_cache()
    if source == "spoonacular":
        return await _spoon_lookup(external_id)
    return await _mealdb_lookup(external_id)
