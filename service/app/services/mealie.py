"""Client for the Mealie recipe manager (https://mealie.io).

Mealie v2 renamed the group-scoped API routes from /api/groups/... to
/api/households/...; the client probes once and remembers which scheme the
server speaks, so both v1.x and v2.x installs work.
"""
import asyncio
import bisect
import time

import httpx

from ..config import settings
from . import proxy_policy, upstream
from .ingredient_parse import build_recipe_ingredients


class MealieError(Exception):
    """Raised with Mealie's actual error message instead of a bare HTTP status.

    Also raised, with an honest user-forward message, when Mealie (or the main
    server, on a satellite) cannot be reached at all, so every route that
    handles MealieError degrades the same way during an outage instead of
    letting a raw httpx connection error bubble up as a 500.
    """


WEB_PAGE_MESSAGE = ("Mealie answered with a web page instead of data. Check that "
                    "the Mealie address is the plain server address, with no /api "
                    "or other path on the end.")


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
_JSON = {"Content-Type": "application/json"}

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


class MealieClient:
    def __init__(self):
        self._upstream = bool(settings.is_satellite() and settings.upstream_linked())
        if self._upstream:
            # Satellite: reach the server's internal Mealie through the main
            # server's authenticated proxy (see GrocyClient for the rationale).
            self.base = settings.remote_server_url.rstrip("/") + "/api/proxy/mealie"
            self.headers = upstream.request_kwargs(extra=_JSON)["headers"]
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
            return bool(settings.upstream_linked() and settings.mealie_base_url)
        return bool(settings.mealie_base_url and settings.mealie_api_key)

    def _send_kwargs(self) -> dict:
        if self._upstream:
            return upstream.request_kwargs(extra=_JSON)
        return {"headers": self.headers}

    async def _request(self, method: str, path: str, body=None, params=None):
        try:
            r = await _client.request(
                method, f"{self.base}/api{path}",
                json=body, params=params, **self._send_kwargs(),
            )
        except httpx.HTTPError as e:
            # Connection refused, DNS failure, timeout: the service is down or
            # unreachable. Surface it as a MealieError with honest copy so
            # every caller degrades consistently (FoodAssistant-2cmm).
            raise MealieError(unreachable_message()) from e
        if r.status_code >= 400:
            refused = proxy_policy.refusal_detail(r.status_code, r.text or "")
            if refused:
                # The main server's proxy would not carry this call: show its
                # words instead of a bare "Mealie 403".
                raise MealieError(refused)
            detail = r.text[:300].strip() or r.reason_phrase
            raise MealieError(f"Mealie {r.status_code} on {path}: {detail}")
        if not r.content:
            return {}
        # Mealie 3.24+ answers an unrouted GET with its web app (200 text/html)
        # instead of a 404, usually because the configured address carries a
        # path such as /api. Parsing that as JSON would escape every
        # MealieError handler as a raw 500.
        if "json" not in r.headers.get("content-type", "").lower():
            raise MealieError(WEB_PAGE_MESSAGE)
        try:
            return r.json()
        except ValueError as e:
            raise MealieError(WEB_PAGE_MESSAGE) from e

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
            # A Mealie that serves its web page for an unknown route answers
            # the missing scope that way instead of with a 404.
            if not (str(e).startswith("Mealie 404 ") or str(e) == WEB_PAGE_MESSAGE):
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
            # Keep the typeahead in step with the id map, so a food created
            # here is offered straight away rather than after the cache TTL.
            shown = name.strip()
            if not any(_normalize_name(n) == key for n in _food_names):
                bisect.insort(_food_names, shown, key=str.lower)
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

