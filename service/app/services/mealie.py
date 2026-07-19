"""Client for the Mealie recipe manager (https://mealie.io).

Mealie v2 renamed the group-scoped API routes from /api/groups/... to
/api/households/...; the client probes once and remembers which scheme the
server speaks, so both v1.x and v2.x installs work.
"""
import asyncio
import re
import time
from pathlib import Path

import httpx

from ..config import settings
from . import recipe_source


class MealieError(Exception):
    """Raised with Mealie's actual error message instead of a bare HTTP status.

    Also raised, with an honest user-forward message, when Mealie (or the main
    server, on a satellite) cannot be reached at all, so every route that
    handles MealieError degrades the same way during an outage instead of
    letting a raw httpx connection error bubble up as a 500.
    """


def unreachable_message() -> str:
    """The user-forward message for a dead upstream connection.

    A satellite talks to Mealie through the main server's proxy, so a connect
    failure there means the main server is gone, not Mealie itself.
    """
    if settings.is_satellite():
        return ("The main server is not reachable. Recipes, meal plan, and "
                "shopping will return when it is.")
    return ("Mealie is not reachable. Recipes, meal plan, and shopping will "
            "return when it is.")


_client = httpx.AsyncClient(timeout=20.0)

# "households" (v2+) or "groups" (v1.x); probed on first scoped request.
# Per-process on purpose: the API generation of a given Mealie server never
# changes at runtime, and reset_cache() clears it on a settings save.
_scope: str | None = None

# Recipe-with-ingredients cache shared across requests: slug -> detail dict.
# Per-process scope, acknowledged (security review, Jul 2026): the write paths
# below invalidate only this process's copy, so under several uvicorn workers
# another worker could serve up to TTL-stale recipes. The deployment runs a
# single worker and instance_guard warns loudly if it ever does not, so the
# short TTL is the proportionate fix; worst case is staleness, never wrong
# writes (mutations always go straight to Mealie).
_recipe_cache: dict[str, dict] = {}
_recipe_cache_at: float = 0.0
_RECIPE_CACHE_TTL = 600  # seconds

# Foods and units catalog, keyed by normalized (lower/stripped) name -> Mealie id
# (FoodAssistant-djwe). Mealie 3.19 rejects a recipeIngredient that names a food
# or unit it has not seen, so a parsed ingredient has to reference the food/unit
# by id, creating it first if it does not exist (exactly what Mealie's own editor
# does when you type a new food). These maps let a single save resolve every name
# without re-listing, and the short TTL keeps them fresh for later saves.
# Per-process scope, same trade-off as the recipe cache above: a single worker in
# practice, worst case a brief staleness that only means an extra list call.
_foods_cache: dict[str, str] = {}
_units_cache: dict[str, str] = {}
# Original food names (as Mealie stores them), kept alongside the id map so the
# shopping quick-add typeahead can show real casing without a second listing.
_food_names: list[str] = []
_catalog_at: float = 0.0
_CATALOG_TTL = 300  # seconds


def _normalize_name(name) -> str:
    """Case-insensitive, whitespace-trimmed key for matching a food or unit.

    A food is matched (and never duplicated) on this normalized name, so
    "Flour", "flour", and "  flour " all resolve to the same Mealie food.
    """
    return str(name or "").strip().lower()


def reset_cache() -> None:
    global _scope, _catalog_at
    _scope = None
    _catalog_at = 0.0
    _foods_cache.clear()
    _units_cache.clear()
    _food_names.clear()
    _invalidate_recipe_cache()


def _invalidate_recipe_cache() -> None:
    global _recipe_cache, _recipe_cache_at
    _recipe_cache = {}
    _recipe_cache_at = 0.0


# ── Structured ingredient normalization (FoodAssistant-au59) ─────────────────
# Turn an AI provider's parsed {quantity, unit, food, note} objects into the
# recipeIngredient shape Mealie stores. Pure and shared so the save path and the
# "Parse ingredients" action agree, and so it is cheap to unit-test. The guiding
# rule is that an ingredient is NEVER lost: a line the model could not parse (no
# food) falls back to the plain note entry Mealie has always accepted, exactly
# like the pre-au59 behavior.

def _to_quantity(value) -> float | None:
    """Coerce a model's quantity into a float, or None when there is no amount.

    Tolerates numbers, numeric strings, simple fractions ("1/2"), and mixed
    numbers ("1 1/2"); anything unrecognized becomes None so "to taste" style
    lines stay amount-free rather than guessing a number.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    total = 0.0
    matched = False
    for part in text.split():
        try:
            if "/" in part:
                num, den = part.split("/", 1)
                total += float(num) / float(den)
            else:
                total += float(part)
            matched = True
        except (ValueError, ZeroDivisionError):
            return None
    return total if matched else None


def structured_ingredient(line: str, parsed) -> dict | None:
    """One Mealie recipeIngredient entry from an original line and its parse.

    ``line`` is always the user's original text and becomes ``originalText`` and
    the fallback note, so it is never dropped or altered. When ``parsed`` has no
    usable food, the entry is a plain ``{"note": line}`` (today's free-text
    shape), which is what keeps an unparseable ingredient from being lost.
    """
    line = (line or "").strip()
    if not line:
        return None
    food = ""
    unit = ""
    note = ""
    quantity = None
    if isinstance(parsed, dict):
        food = str(parsed.get("food") or "").strip()
        unit = str(parsed.get("unit") or "").strip()
        note = str(parsed.get("note") or "").strip()
        quantity = _to_quantity(parsed.get("quantity"))
    if not food:
        return {"note": line}
    return {
        "quantity": quantity,
        # This is the intermediate {name} shape. Mealie 3.19 will not accept a
        # food or unit named this way, so the async resolver on MealieClient
        # (resolve_structured_ids) turns each {"name": ...} into {"id", "name"}
        # by finding or creating the food/unit before the recipe is saved. null
        # means "no unit" (fine for "2 eggs" or "salt to taste").
        "unit": {"name": unit} if unit else None,
        "food": {"name": food},
        "note": note,
        "originalText": line,
        # Show the amount: false keeps quantity/unit visible. A null quantity
        # simply renders as the food alone, so "salt to taste" reads correctly.
        "disableAmount": False,
    }


def structured_recipe_ingredients(lines: list[str], parsed_list) -> list[dict]:
    """Normalize whole ingredient lists, one entry per non-empty line, in order.

    Aligns the model's objects to the original lines by position. A short,
    over-long, or missing result never drops a line: any line without a matching
    parse falls back to a plain note entry.
    """
    parsed_list = parsed_list if isinstance(parsed_list, list) else []
    out: list[dict] = []
    for i, line in enumerate(lines or []):
        if not (line or "").strip():
            continue
        parsed = parsed_list[i] if i < len(parsed_list) else None
        entry = structured_ingredient(line, parsed)
        if entry:
            out.append(entry)
    return out


async def build_recipe_ingredients(lines: list[str]) -> list[dict]:
    """Intermediate recipeIngredient list for a create/patch, AI-parsed when able.

    When an AI provider is configured, the lines are parsed into
    quantity/unit/food so the recipe can land parsed in Mealie. This returns the
    {name} intermediate shape; MealieClient.resolve_structured_ids then swaps
    each food/unit name for its Mealie id before the save. Resilient by design:
    no provider, a provider that does not support parsing (returns None), or any
    error falls back to the plain note entries Mealie has always taken, so a save
    never breaks and no ingredient is lost.
    """
    lines = [line.strip() for line in (lines or []) if line and line.strip()]
    if not lines:
        return []
    if settings.ai_configured():
        try:
            from ..dependencies import get_enrich_provider
            parsed = await get_enrich_provider().parse_ingredients(lines)
            if parsed:
                return structured_recipe_ingredients(lines, parsed)
        except Exception:
            # A bad reply, a provider outage, or an unsupported provider all fall
            # through to the free-text shape rather than failing the save.
            pass
    return [{"note": line} for line in lines]


class MealieClient:
    def __init__(self):
        if settings.is_satellite() and settings.remote_server_url and settings.upstream_api_key:
            # Satellite: reach the server's internal Mealie through the main
            # server's authenticated proxy (see GrocyClient for the rationale).
            self.base = settings.remote_server_url.rstrip("/") + "/api/proxy/mealie"
            self.headers = {
                "X-API-Key": settings.upstream_api_key,
                "Content-Type": "application/json",
            }
        else:
            self.base = settings.mealie_base_url.rstrip("/")
            self.headers = {
                "Authorization": f"Bearer {settings.mealie_api_key}",
                "Content-Type": "application/json",
            }

    @property
    def configured(self) -> bool:
        # On a satellite, Mealie is reachable via the proxy; it counts as
        # configured when the upstream link is set and the server reported a
        # Mealie base URL during config sync (so we don't enable it when the
        # server has no Mealie).
        if settings.is_satellite():
            return bool(settings.remote_server_url and settings.upstream_api_key
                        and settings.mealie_base_url)
        return bool(settings.mealie_base_url and settings.mealie_api_key)

    async def _request(self, method: str, path: str, body=None, params=None):
        try:
            r = await _client.request(
                method, f"{self.base}/api{path}",
                headers=self.headers, json=body, params=params,
            )
        except httpx.HTTPError as e:
            # Connection refused, DNS failure, timeout: the service is down or
            # unreachable. Surface it as a MealieError with honest copy so
            # every caller degrades consistently (FoodAssistant-2cmm).
            raise MealieError(unreachable_message()) from e
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

    # ── Foods and units (parsed-ingredient resolution) ───────────────────────
    # A parsed ingredient must reference a food (and unit) that already exists in
    # Mealie by id; Mealie 3.19 rejects one named inline (FoodAssistant-djwe).
    # These helpers find the id for a name, creating the food/unit the first time
    # it is used. Only names the AI actually parsed reach here, so nothing is
    # created from a blank or garbage token. Note that only your own Mealie sees
    # these food and unit names; the ingredient text alone goes to your AI
    # provider.

    async def _list_all(self, path: str) -> list[dict]:
        """Page through an unscoped Mealie list endpoint and return every item."""
        items: list[dict] = []
        page = 1
        while True:
            data = await self._request("GET", path,
                                       params={"page": page, "perPage": 100})
            if not isinstance(data, dict):
                break
            batch = data.get("items") or []
            items.extend(batch)
            total_pages = data.get("total_pages") or 1
            if page >= total_pages or not batch:
                break
            page += 1
        return items

    async def _ensure_catalog(self) -> None:
        """Load the foods and units name->id maps once, honoring the short TTL."""
        global _catalog_at, _foods_cache, _units_cache, _food_names
        if _catalog_at and time.time() - _catalog_at < _CATALOG_TTL:
            return
        foods = await self._list_all("/foods")
        units = await self._list_all("/units")
        _foods_cache = {
            _normalize_name(f.get("name")): f["id"]
            for f in foods if f.get("name") and f.get("id")
        }
        _units_cache = {
            _normalize_name(u.get("name")): u["id"]
            for u in units if u.get("name") and u.get("id")
        }
        _food_names = sorted(
            {str(f.get("name")).strip() for f in foods if f.get("name")},
            key=str.lower,
        )
        _catalog_at = time.time()

    async def suggest_foods(self, prefix: str, limit: int = 8) -> list[str]:
        """Food names matching a typed prefix, for the shopping quick-add.

        Prefix hits come first, then names that merely contain the text, each
        group alphabetical. Uses the same short-lived catalog cache as ingredient
        saving, so typing does not hammer Mealie.
        """
        key = _normalize_name(prefix)
        if not key:
            return []
        await self._ensure_catalog()
        starts: list[str] = []
        contains: list[str] = []
        for name in _food_names:
            low = name.lower()
            if low.startswith(key):
                starts.append(name)
            elif key in low:
                contains.append(name)
        return (starts + contains)[: max(1, limit)]

    async def _resolve_food_id(self, name: str) -> str | None:
        """Find (or create) a Mealie food by name and return its id."""
        key = _normalize_name(name)
        if not key:
            return None
        if key in _foods_cache:
            return _foods_cache[key]
        created = await self._request("POST", "/foods", body={"name": name.strip()})
        fid = created.get("id") if isinstance(created, dict) else None
        if fid:
            _foods_cache[key] = fid
        return fid

    async def _resolve_unit_id(self, name: str) -> str | None:
        """Find (or create) a Mealie unit by name and return its id."""
        key = _normalize_name(name)
        if not key:
            return None
        if key in _units_cache:
            return _units_cache[key]
        created = await self._request("POST", "/units", body={"name": name.strip()})
        uid = created.get("id") if isinstance(created, dict) else None
        if uid:
            _units_cache[key] = uid
        return uid

    async def resolve_structured_ids(self, structured: list[dict]) -> list[dict]:
        """Turn the {name} intermediate into the id-based shape Mealie 3.19 takes.

        Each parsed line's food becomes {"id", "name"} and its unit likewise (or
        null when there is none); a note-only line (an ingredient the AI could not
        parse) passes through untouched, so nothing is ever dropped. If a food or
        unit name cannot be resolved, that single line degrades to a plain note
        rather than failing the whole save. When no line carries a food there is
        nothing to resolve, so Mealie is not touched at all.
        """
        if not any(isinstance(e.get("food"), dict)
                   and _normalize_name(e.get("food", {}).get("name"))
                   for e in structured):
            return structured
        await self._ensure_catalog()
        out: list[dict] = []
        for entry in structured:
            food = entry.get("food")
            food_name = food.get("name") if isinstance(food, dict) else None
            if not _normalize_name(food_name):
                out.append(entry)  # note-only line, left as written
                continue
            fid = await self._resolve_food_id(food_name)
            if not fid:
                # Could not resolve the food: keep the line as a plain note so the
                # save still succeeds and the ingredient is not lost.
                out.append({"note": entry.get("originalText") or food_name})
                continue
            resolved = dict(entry)
            resolved["food"] = {"id": fid, "name": food_name}
            unit = entry.get("unit")
            unit_name = unit.get("name") if isinstance(unit, dict) else None
            if _normalize_name(unit_name):
                uid = await self._resolve_unit_id(unit_name)
                resolved["unit"] = {"id": uid, "name": unit_name} if uid else None
            else:
                resolved["unit"] = None
            out.append(resolved)
        return out

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

        Mealie's POST only takes a name; everything else goes in a PATCH. When an
        AI provider is configured the ingredient lines are parsed into Mealie's
        structured quantity/unit/food shape first (FoodAssistant-au59), so the
        recipe lands already parsed instead of showing Mealie's "click Parse"
        prompt. This is the single choke point every save path flows through
        (URL, PDF, photo, file, community, manual, optimize), so they all
        benefit. Falls back to plain note entries on any parse failure.
        """
        slug = await self._request("POST", "/recipes", body={"name": data["name"]})
        if not isinstance(slug, str):
            slug = slug.get("slug", "")
        lines = [i for i in data.get("ingredients") or [] if i and i.strip()]
        base_patch = {
            "description": data.get("description") or "",
            "recipeYield": data.get("servings") or "",
            "totalTime": data.get("total_time") or "",
            # Prep and cook time round-trip on the Mealie backend too, matching
            # the native store (FoodAssistant-u65k). "performTime" is Mealie's
            # field name for cook time (the same one get_recipe returns).
            "prepTime": data.get("prep_time") or "",
            "performTime": data.get("cook_time") or "",
            # Mealie 3.19+ requires each instruction to carry ingredientReferences
            # (its RecipeInstruction model has it as a required field), so a bare
            # {"text": ...} PATCH 500s with a TypeError there. An empty list is
            # valid on older Mealie too, so this works across versions
            # (FoodAssistant-z2qo).
            "recipeInstructions": [
                {"text": s, "ingredientReferences": []}
                for s in data.get("instructions") or [] if s.strip()
            ],
        }
        # Prefer AI-parsed structured ingredients so the recipe lands already
        # parsed (FoodAssistant-au59). Mealie 3.19 only accepts a parsed food or
        # unit referenced by id, so resolve_structured_ids finds or creates each
        # one and rewrites the shape (FoodAssistant-djwe). If resolving fails, or
        # Mealie still rejects the structured PATCH (FoodAssistant-ztjc), fall
        # back to plain-text ingredients: the recipe still saves, unparsed at
        # worst, and never errors out to the user.
        free_text = [{"note": i} for i in lines]
        try:
            structured = await self.resolve_structured_ids(
                await build_recipe_ingredients(lines))
        except MealieError:
            structured = free_text
        try:
            await self._request("PATCH", f"/recipes/{slug}",
                                 body={**base_patch, "recipeIngredient": structured})
        except MealieError:
            if structured != free_text:
                await self._request("PATCH", f"/recipes/{slug}",
                                     body={**base_patch, "recipeIngredient": free_text})
            else:
                raise
        _invalidate_recipe_cache()
        return slug

    async def set_recipe_ingredients(self, slug: str,
                                     ingredients: list[dict]) -> list[dict]:
        """Replace a saved recipe's ingredient list with a parsed one.

        Backs the "Parse ingredients" action for an already-imported recipe
        (FoodAssistant-au59): the recipe stays put, only its recipeIngredient is
        rewritten. Takes the {name} intermediate and resolves each food/unit to a
        Mealie id (FoodAssistant-djwe) so the parse actually applies on Mealie
        3.19. If resolving fails, or Mealie rejects the parsed shape
        (FoodAssistant-ztjc), it falls back to plain-text notes so the recipe is
        never left broken. Returns the list actually written, so the caller can
        report how many lines really parsed. The /recipes/{slug} PATCH is unscoped
        and identical on Mealie v1 and v2, so no scope probe is needed here.
        """
        free_text = [
            {"note": e.get("originalText") or e.get("note") or ""}
            for e in ingredients
        ]
        try:
            resolved = await self.resolve_structured_ids(ingredients)
        except MealieError:
            resolved = free_text
        try:
            await self._request("PATCH", f"/recipes/{slug}",
                                body={"recipeIngredient": resolved})
        except MealieError:
            if resolved != free_text:
                await self._request("PATCH", f"/recipes/{slug}",
                                    body={"recipeIngredient": free_text})
                resolved = free_text
            else:
                raise
        _invalidate_recipe_cache()
        return resolved

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

# Measurement, quantity, and packaging words that ride along in a real recipe
# ingredient ("3 tablespoons unsalted butter", "1 teaspoon kosher salt") but say
# nothing about what the food is. Ignored when deciding if an ingredient is a
# staple, so a verbose ingredient still matches "butter"/"salt"/"olive oil"
# instead of falling out of the "with pantry staples" tier (the reason that tier
# often came back empty). Stock matching is unaffected: it intersects on the food
# word regardless of these.
_MEASURE_TOKENS = {
    "teaspoon", "teaspoons", "tsp", "tablespoon", "tablespoons", "tbsp", "tbs",
    "cup", "cups", "ounce", "ounces", "oz", "pound", "pounds", "lb", "lbs",
    "gram", "grams", "gm", "gms", "kilogram", "kilograms", "kg",
    "milliliter", "milliliters", "ml", "liter", "liters", "litre", "litres",
    "pint", "pints", "quart", "quarts", "gallon", "gallons",
    "pinch", "pinches", "dash", "dashes", "handful", "splash", "drizzle",
    "can", "cans", "package", "packages", "pkg", "packet", "packets",
    "jar", "jars", "box", "boxes", "bag", "bags", "bottle", "bottles",
    "container", "containers", "slice", "slices", "piece", "pieces",
    "sprig", "sprigs", "stalk", "stalks", "bunch", "bunches", "knob",
}


def _load_staples_file() -> list[frozenset[str]] | None:
    """Read staples.txt and return each line as a frozen token set.

    Uses phrase-level matching rather than a merged token soup so that
    "Chicken stock" in the file does NOT make bare "chicken" a staple.

    Search order:
      1. <data_dir>/staples.txt : user-customisable, gitignored volume file
      2. <app>/data/staples_default.txt: bundled default, shipped with the image
    Returns None only when both files are absent or yield no phrases.
    """
    candidates = [
        Path(settings.data_dir) / "staples.txt",
        Path(__file__).parent.parent / "data" / "staples_default.txt",
    ]
    for staples_path in candidates:
        if not staples_path.exists():
            continue
        phrases: list[frozenset[str]] = []
        for line in staples_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                toks = _tokens(line)
                if toks:
                    phrases.append(frozenset(toks))
        if phrases:
            return phrases
    return None


# Cached list of phrase token-sets built from staples.txt (invalidated on save).
# Per-process scope is acceptable: the file changes only through a settings
# save, which calls reset_staple_cache() in the same process that serves it.
_staple_phrases_cache: list[frozenset[str]] | None = None
_staple_phrases_loaded: bool = False


def _active_staple_phrases() -> list[frozenset[str]] | None:
    """Phrase token-sets from staples.txt, loaded once and cached."""
    global _staple_phrases_cache, _staple_phrases_loaded
    if not _staple_phrases_loaded:
        _staple_phrases_cache = _load_staples_file()
        _staple_phrases_loaded = True
    return _staple_phrases_cache


def reset_staple_cache() -> None:
    """Invalidate the staples file cache (call after settings save)."""
    global _staple_phrases_loaded
    _staple_phrases_loaded = False


def _active_staple_tokens() -> set[str]:
    """Fuzzy staple token set: settings UI field → built-in fallback.

    The staples.txt file is handled separately via phrase matching in
    classify_recipes (see _active_staple_phrases) to avoid false positives
    like "chicken" matching because "chicken stock" is in the file.
    """
    if settings.staple_items.strip():
        toks: set[str] = set()
        for item in settings.staple_items.split(","):
            toks |= _tokens(item)
        if toks:
            return toks
    return _STAPLE_TOKENS


# Modifier/packaging tokens stripped from phrase token sets before matching.
# "canned chickpeas" → core tokens {"chickpea"}, so "chickpeas" in a recipe matches.
# "chicken stock" → core tokens {"chicken", "stock"} (stock is NOT a modifier),
# so bare "chicken" does NOT match.
_PHRASE_MODIFIERS = {
    "canned", "dried", "smoked", "ground", "crushed", "whole",
    "hard", "dry", "extra", "virgin", "unsalted", "salted",
    "granulated", "confectioner", "table", "white", "brown",
    "black", "red", "grating", "large", "small", "medium",
}


# Generic descriptor words a recipe may tack onto a staple without changing
# what it is: "parmesan cheese" is still the Parmesan staple, "grated parmesan"
# and "fresh garlic" likewise. These are allowed as EXTRA ingredient tokens
# beyond a staple phrase's core. A distinct food word (e.g. "coconut" in
# "coconut milk") is deliberately NOT here, so "coconut milk" stays non-staple.
_STAPLE_DESCRIPTORS = (
    _PHRASE_MODIFIERS
    | _STAPLE_GLUE_TOKENS
    | _MEASURE_TOKENS
    | {
        "cheese", "fresh", "chopped", "diced", "sliced", "minced", "grated",
        "shredded", "freshly", "fine", "finely", "coarse", "coarsely", "good",
        "quality", "best", "pure", "raw", "flaky", "fillet", "fillets",
        "leaf", "leave", "stick", "stalk", "head", "bunch",
    }
)


def _phrase_core(phrase: frozenset[str]) -> frozenset[str]:
    """Return phrase tokens with modifier-only tokens removed."""
    core = phrase - _PHRASE_MODIFIERS
    return core if core else phrase   # keep original if modifiers consumed everything


def _is_staple_ingredient(ing_toks: set[str]) -> bool:
    """True if the ingredient tokens match any active staple definition.

    Two pathways:
    1. Fuzzy token check: all ingredient tokens are staple/glue/freebie tokens
       (handles loose pantry items like "flour", "unsalted butter", "table salt").
    2. Phrase core match: a staple phrase's core tokens are contained in the
       ingredient's tokens, and any leftover ingredient tokens are benign
       descriptors. "chickpeas" matches "canned chickpeas" and "parmesan cheese"
       matches "Parmesan", but bare "chicken" does NOT match "chicken stock"
       (the phrase carries an extra token the ingredient lacks) and "coconut
       milk" does NOT match "Milk" ("coconut" is not a descriptor).
    """
    staple_toks = _active_staple_tokens()
    # Pathway 1: original fuzzy check. Measurement/quantity words are ignored so
    # a verbose ingredient ("3 tablespoons unsalted butter") still matches.
    if (ing_toks & staple_toks
            and ing_toks <= (staple_toks | _STAPLE_GLUE_TOKENS | _FREEBIE_TOKENS | _MEASURE_TOKENS)):
        return True
    # Pathway 2: phrase containment match (requires file to be present).
    # A staple phrase matches when its core tokens are all present in the
    # ingredient AND every leftover ingredient token is a benign descriptor
    # (prep words, packaging, "cheese", etc.). This lets real recipe ingredients
    # carry descriptors the file phrase omits ("parmesan cheese" -> "Parmesan",
    # "grated parmesan" -> "Parmesan") while still rejecting "chicken" against
    # "chicken stock" (the phrase has the extra token "stock", not the
    # ingredient) and "coconut milk" against "Milk" ("coconut" is not a
    # descriptor). Skipped when the settings field is set: that list replaces
    # the file.
    if settings.staple_items.strip():
        return False
    phrases = _active_staple_phrases()
    if phrases:
        ing_core = ing_toks - _PHRASE_MODIFIERS
        if ing_core:
            for phrase in phrases:
                phrase_core = _phrase_core(phrase)
                if phrase_core <= ing_core and (ing_core - phrase_core) <= _STAPLE_DESCRIPTORS:
                    return True
    return False


def partition_recipe_ingredients(ingredients: list[dict], stock: list[dict]) -> dict:
    """Split one recipe's ingredients into what you own and what to buy.

    Reuses the exact matching the suggestion ranker already does: an ingredient
    counts as owned when its food word matches an inventory item (in Grocy
    stock) OR it is on your staples list (the things you always keep on hand);
    everything left is what you actually need to buy. Water and other freebies
    never land on either list. Pure and shared so the shopping-list route and
    its tests agree on the same answer.

    Returns {"owned": [...], "needed": [...]} preserving the recipe's order.
    """
    inv = [toks for toks in (_tokens(s["name"]) for s in stock or []) if toks]
    owned: list[str] = []
    needed: list[str] = []
    for ing in ingredients or []:
        text = _ingredient_text(ing).strip()
        ing_toks = _tokens(text)
        if not text or not ing_toks or ing_toks <= _FREEBIE_TOKENS:
            continue
        in_stock = any(ing_toks & inv_toks for inv_toks in inv)
        if in_stock or _is_staple_ingredient(ing_toks):
            owned.append(text)
        else:
            needed.append(text)
    return {"owned": owned, "needed": needed}


def _is_perishable(stock_item: dict) -> bool:
    """Refrigerated items and anything expiring within the configured window."""
    if stock_item.get("storage_bucket") == "refrigerated":
        return True
    d = stock_item.get("days_remaining")
    return d is not None and d <= settings.perishable_days


def classify_recipes(recipes: list[dict], stock: list[dict],
                     top_per_tier: int = 8) -> dict[str, list[dict]]:
    """Sort recipes into three cookability tiers against current inventory.

    ready   : every ingredient matches an item in stock
    staples : in stock + common pantry staples (eggs, butter, flour, ...)
    shopping: uses at least one perishable stock item but needs a shop run

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

    soon = settings.expiring_soon_days
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
                if d is not None and d <= soon and hit["name"] not in expiring_used:
                    expiring_used.append(hit["name"])
            elif _is_staple_ingredient(ing_toks):
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
            # Where this candidate came from, so the Cook page shows the same
            # source chip as the Recipes page (FoodAssistant-5frk). Mealie detail
            # carries orgURL, which marks an imported recipe.
            "badge": recipe_source.source_badge(
                r.get("source", "mealie"), bool(r.get("orgURL"))),
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
