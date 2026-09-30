"""Integration tests for the real MealieClient against a mock Mealie server.

A small Starlette ASGI app stands in for Mealie. The module-level
``app.services.mealie._client`` is swapped for an ``httpx.AsyncClient`` wired to
that app via ``httpx.ASGITransport`` so the genuine client code path runs with
no network. ``reset_cache()`` is called between version scenarios because the
v1/v2 scope decision is cached in a module global.

Covered:
  (a) v2 auto-detects the ``/api/households/`` scope
  (b) v1 falls back to ``/api/groups/`` after a 404
  (c) mealplan + shopping-list parsing
  (d) get_recipes_with_ingredients concurrent detail fetch
  (e) end-to-end classify_recipes tiering off Mealie-shaped recipes
"""
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.config import settings
from app.services import mealie as m
from app.services.mealie import MealieClient, classify_recipes


# ── Mock Mealie ASGI app ─────────────────────────────────────────────────────

RECIPE_SUMMARIES = [
    {"slug": "chicken-rice", "name": "Chicken & Rice"},
    {"slug": "salmon-curry", "name": "Salmon Curry"},
]
RECIPE_DETAILS = {
    "chicken-rice": {
        "slug": "chicken-rice", "name": "Chicken & Rice", "id": "r1",
        "recipeIngredient": [{"note": "chicken breast"}, {"note": "white rice"}],
    },
    "salmon-curry": {
        "slug": "salmon-curry", "name": "Salmon Curry", "id": "r2",
        "recipeIngredient": [
            {"note": "salmon"}, {"note": "coconut milk"}, {"note": "curry paste"},
        ],
    },
}


def _make_mock_app(scope_path: str) -> Starlette:
    """Build a mock Mealie app exposing scoped routes under /api/<scope_path>/.

    Requests to the *other* scope return 404 so the client's fallback probe is
    exercised realistically.
    """
    other = "groups" if scope_path == "households" else "households"

    async def users_self(request):
        return JSONResponse({"username": "tester", "email": "t@example.com"})

    async def recipes_list(request):
        return JSONResponse({"items": RECIPE_SUMMARIES})

    async def recipe_detail(request):
        slug = request.path_params["slug"]
        return JSONResponse(RECIPE_DETAILS[slug])

    async def mealplans(request):
        return JSONResponse({"items": [
            {"id": 1, "date": "2026-06-17", "title": "Chicken & Rice",
             "recipe": {"slug": "chicken-rice"}},
        ]})

    async def shopping_lists(request):
        return JSONResponse({"items": [
            {"id": "list-1", "name": "This week"},
        ]})

    async def not_found(request):
        return JSONResponse({"detail": "Not found"}, status_code=404)

    routes = [
        Route("/api/users/self", users_self),
        Route("/api/recipes", recipes_list),
        Route("/api/recipes/{slug}", recipe_detail),
        Route(f"/api/{scope_path}/mealplans", mealplans),
        Route(f"/api/{scope_path}/shopping/lists", shopping_lists),
        # The wrong scope must 404 so _scoped() falls back.
        Route(f"/api/{other}/mealplans", not_found),
        Route(f"/api/{other}/shopping/lists", not_found),
    ]
    return Starlette(routes=routes)


@pytest.fixture
def mealie_env(monkeypatch):
    """Configure settings + swap the module-level httpx client for an ASGI one."""
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test")
    monkeypatch.setattr(settings, "mealie_api_key", "test-token")
    original_client = m._client

    def wire(scope_path: str) -> MealieClient:
        m.reset_cache()
        transport = httpx.ASGITransport(app=_make_mock_app(scope_path))
        client = httpx.AsyncClient(transport=transport, base_url="http://mealie.test")
        m._client = client
        return MealieClient()

    yield wire

    m._client = original_client
    m.reset_cache()


# ── (a) v2 households auto-detection ─────────────────────────────────────────

@pytest.mark.anyio
async def test_v2_autodetects_households(mealie_env):
    client = mealie_env("households")
    plan = await client.get_mealplan("2026-06-15", "2026-06-21")
    assert m._scope == "households"
    assert plan and plan[0]["title"] == "Chicken & Rice"


# ── (b) v1 groups fallback after 404 ─────────────────────────────────────────

@pytest.mark.anyio
async def test_v1_falls_back_to_groups(mealie_env):
    client = mealie_env("groups")
    plan = await client.get_mealplan("2026-06-15", "2026-06-21")
    assert m._scope == "groups"
    assert plan and plan[0]["title"] == "Chicken & Rice"


# ── (c) mealplan + shopping-list parsing ─────────────────────────────────────

@pytest.mark.anyio
async def test_mealplan_and_shopping_parsing(mealie_env):
    client = mealie_env("households")
    plan = await client.get_mealplan("2026-06-15", "2026-06-21")
    assert [e["title"] for e in plan] == ["Chicken & Rice"]

    lists = await client.get_shopping_lists()
    assert [lst["name"] for lst in lists] == ["This week"]


# ── (d) concurrent get_recipes_with_ingredients ──────────────────────────────

@pytest.mark.anyio
async def test_recipes_with_ingredients_concurrent(mealie_env):
    client = mealie_env("households")
    recipes = await client.get_recipes_with_ingredients()
    by_slug = {r["slug"]: r for r in recipes}
    assert set(by_slug) == {"chicken-rice", "salmon-curry"}
    assert by_slug["chicken-rice"]["recipeIngredient"][0]["note"] == "chicken breast"


# ── (e) end-to-end classify_recipes tiering ──────────────────────────────────

@pytest.mark.anyio
async def test_end_to_end_classify_tiering(mealie_env, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "staple_items", "")
    monkeypatch.setattr(settings, "perishable_days", 14)
    monkeypatch.setattr(settings, "expiring_soon_days", 5)
    m.reset_staple_cache()

    client = mealie_env("households")
    recipes = await client.get_recipes_with_ingredients()

    stock = [
        {"name": "Chicken Breast", "days_remaining": 3, "storage_bucket": "refrigerated"},
        {"name": "Rice", "days_remaining": 300, "storage_bucket": "pantry"},
        {"name": "Salmon", "days_remaining": 2, "storage_bucket": "refrigerated"},
    ]
    tiers = classify_recipes(recipes, stock)

    assert {r["name"] for r in tiers["ready"]} == {"Chicken & Rice"}
    # Salmon (perishable, expiring) in stock but coconut milk + curry paste missing.
    assert {r["name"] for r in tiers["shopping"]} == {"Salmon Curry"}
    m.reset_staple_cache()


@pytest.mark.anyio
async def test_create_recipe_instructions_carry_ingredient_references(monkeypatch):
    """Mealie 3.19+ requires ingredientReferences on each instruction, so the
    create PATCH must include it or the import 500s (FoodAssistant-z2qo)."""
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test")
    monkeypatch.setattr(settings, "mealie_api_key", "t")
    captured = {}

    async def post_recipes(request):
        return JSONResponse("my-recipe")  # Mealie returns the new slug

    async def patch_recipe(request):
        captured["body"] = await request.json()
        return JSONResponse({"slug": "my-recipe"})

    app = Starlette(routes=[
        Route("/api/recipes", post_recipes, methods=["POST"]),
        Route("/api/recipes/{slug}", patch_recipe, methods=["PATCH"]),
    ])
    original = m._client
    m._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mealie.test")
    try:
        slug = await MealieClient().create_recipe({
            "name": "My Recipe",
            "ingredients": ["1 cup flour"],
            "instructions": ["Mix.", "Bake."],
        })
        assert slug == "my-recipe"
        assert captured["body"]["recipeInstructions"] == [
            {"text": "Mix.", "ingredientReferences": []},
            {"text": "Bake.", "ingredientReferences": []},
        ]
    finally:
        m._client = original
        m.reset_cache()


# ── Non-JSON answers, typeahead cache, quantity bounds ───────────────────────

def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="http://mealie.test")


@pytest.mark.anyio
async def test_html_answer_becomes_a_clean_mealie_error(monkeypatch):
    """Mealie 3.24+ serves its web app (200 text/html) for an unrouted GET,
    for example when the address ends in /api. That must be a MealieError the
    routes already handle, not a JSONDecodeError 500 (MEALIE-3)."""
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test/api")
    monkeypatch.setattr(settings, "mealie_api_key", "t")
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                              text="<!doctype html><html><body>Mealie</body></html>")

    original = m._client
    m.reset_cache()
    m._client = _mock_client(handler)
    try:
        with pytest.raises(m.MealieError) as err:
            await MealieClient().get_recipe("anything")
        assert "web page" in str(err.value)
        # The households-to-groups probe falls back and still ends cleanly.
        with pytest.raises(m.MealieError) as err:
            await MealieClient().get_shopping_lists()
        assert "web page" in str(err.value)
        assert any("/households/" in p for p in seen)
        assert any("/groups/" in p for p in seen)
    finally:
        m._client = original
        m.reset_cache()


@pytest.mark.anyio
async def test_empty_and_json_answers_still_parse(monkeypatch):
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test")
    monkeypatch.setattr(settings, "mealie_api_key", "t")

    def handler(request):
        if request.url.path == "/api/empty":
            return httpx.Response(200, content=b"")
        return httpx.Response(200, json={"ok": True})

    original = m._client
    m._client = _mock_client(handler)
    try:
        assert await MealieClient()._request("GET", "/empty") == {}
        assert await MealieClient()._request("GET", "/thing") == {"ok": True}
    finally:
        m._client = original
        m.reset_cache()


@pytest.mark.anyio
async def test_new_food_shows_in_typeahead_immediately(monkeypatch):
    """A food created while saving a parsed ingredient is offered by the
    shopping typeahead straight away, not after the catalog TTL (MEALIE-6)."""
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test")
    monkeypatch.setattr(settings, "mealie_api_key", "t")
    foods = [{"id": "f1", "name": "Basil"}, {"id": "f2", "name": "carrots"}]
    lists = []

    def handler(request):
        path = request.url.path
        if request.method == "GET" and path in ("/api/foods", "/api/units"):
            lists.append(path)
            items = foods if path == "/api/foods" else []
            return httpx.Response(200, json={"items": items, "total_pages": 1})
        if request.method == "POST" and path == "/api/foods":
            return httpx.Response(201, json={"id": "f3", "name": "Bay leaves"})
        return httpx.Response(404, json={"detail": "nope"})

    original = m._client
    m.reset_cache()
    m._client = _mock_client(handler)
    try:
        client = MealieClient()
        assert await client.suggest_foods("ba") == ["Basil"]
        assert await client._resolve_food_id("Bay leaves") == "f3"
        assert await client.suggest_foods("ba") == ["Basil", "Bay leaves"]
        # Still sorted case-insensitively, and no second catalog listing.
        assert m._food_names == ["Basil", "Bay leaves", "carrots"]
        assert lists.count("/api/foods") == 1
    finally:
        m._client = original
        m.reset_cache()


@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "Infinity", 1e999,
                                   float("inf"), float("nan"), "1e999",
                                   10000, "25000", -2, True])
def test_to_quantity_rejects_non_finite_and_absurd_amounts(value):
    assert m._to_quantity(value) is None


@pytest.mark.parametrize("value, expected", [
    ("1/2", 0.5), ("1 1/2", 1.5), (3, 3.0), ("2", 2.0), (9999, 9999.0), (0, 0.0)])
def test_to_quantity_keeps_real_amounts(value, expected):
    assert m._to_quantity(value) == expected
