"""AI ingredient parsing at save time and on demand (FoodAssistant-au59).

Three layers, all offline (provider + Mealie HTTP mocked):

  * the pure normalizer: LLM JSON -> Mealie recipeIngredient shape, a missing
    quantity becomes null, and an unparseable line stays a note-only entry so an
    ingredient is never dropped
  * create_recipe posts structured ingredients when AI is configured and the old
    free-text shape when it is not, and falls back to free text on a provider
    error without losing any line
  * POST /mealie/recipes/parse-ingredients fetches, parses, and rewrites a saved
    recipe, returns the count, and is gated on both AI and Mealie
"""
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

_SERVICE_DIR = Path(__file__).parent.parent / "service"


# ── Pure normalizer ──────────────────────────────────────────────────────────

def test_normalizer_builds_mealie_shape():
    from app.services.mealie import structured_recipe_ingredients
    lines = ["2 cups flour", "1/2 cup sugar", "salt to taste"]
    parsed = [
        {"quantity": 2, "unit": "cup", "food": "flour", "note": ""},
        {"quantity": 0.5, "unit": "cup", "food": "sugar", "note": ""},
        {"quantity": None, "unit": "", "food": "salt", "note": "to taste"},
    ]
    out = structured_recipe_ingredients(lines, parsed)
    assert len(out) == 3
    assert out[0]["quantity"] == 2.0
    assert out[0]["unit"] == {"name": "cup"}
    assert out[0]["food"] == {"name": "flour"}
    assert out[0]["originalText"] == "2 cups flour"
    # No amount stays null rather than being guessed.
    assert out[2]["quantity"] is None
    assert out[2]["unit"] is None
    assert out[2]["food"] == {"name": "salt"}
    assert out[2]["note"] == "to taste"


def test_normalizer_keeps_unparseable_line_as_note():
    from app.services.mealie import structured_recipe_ingredients
    lines = ["1 cup milk", "a pinch of something odd"]
    # The model returned a food for the first line but not the second.
    parsed = [
        {"quantity": 1, "unit": "cup", "food": "milk", "note": ""},
        {"quantity": None, "unit": "", "food": "", "note": ""},
    ]
    out = structured_recipe_ingredients(lines, parsed)
    # Both lines survive: the second falls back to a plain note entry.
    assert len(out) == 2
    assert out[1] == {"note": "a pinch of something odd"}


def test_normalizer_never_drops_when_parse_is_short():
    from app.services.mealie import structured_recipe_ingredients
    lines = ["2 eggs", "1 cup flour", "1 tsp vanilla"]
    parsed = [{"quantity": 2, "unit": "", "food": "eggs", "note": ""}]  # only one back
    out = structured_recipe_ingredients(lines, parsed)
    # Every original line is represented; the unmatched ones become notes.
    assert len(out) == 3
    assert out[1] == {"note": "1 cup flour"}
    assert out[2] == {"note": "1 tsp vanilla"}


def test_quantity_coercion_handles_fractions_and_mixed_numbers():
    from app.services.mealie import _to_quantity
    assert _to_quantity("1/2") == 0.5
    assert _to_quantity("1 1/2") == 1.5
    assert _to_quantity(3) == 3.0
    assert _to_quantity("2") == 2.0
    assert _to_quantity(None) is None
    assert _to_quantity("") is None
    assert _to_quantity("some") is None


# ── create_recipe choke point ────────────────────────────────────────────────

class _FakeParseProvider:
    """Parses each line into a fixed structured shape and records the call."""
    last_lines = None

    async def parse_ingredients(self, lines):
        _FakeParseProvider.last_lines = list(lines)
        return [
            {"quantity": 2, "unit": "cup", "food": "flour", "note": "sifted"}
            for _ in lines
        ]


def _mock_mealie_client(captured: dict):
    """An httpx.AsyncClient wired to a mock Mealie that captures the PATCH body."""
    async def post_recipes(request):
        return JSONResponse("my-recipe")

    async def patch_recipe(request):
        captured["body"] = await request.json()
        return JSONResponse({"slug": "my-recipe"})

    app = Starlette(routes=[
        Route("/api/recipes", post_recipes, methods=["POST"]),
        Route("/api/recipes/{slug}", patch_recipe, methods=["PATCH"]),
    ])
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mealie.test")


@pytest.mark.anyio
async def test_create_recipe_posts_structured_when_ai_configured(monkeypatch):
    from app.config import settings
    from app.services import mealie as m
    import app.dependencies as deps
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test")
    monkeypatch.setattr(settings, "mealie_api_key", "t")
    monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(deps, "get_enrich_provider", lambda: _FakeParseProvider())

    captured: dict = {}
    original = m._client
    m._client = _mock_mealie_client(captured)
    try:
        slug = await m.MealieClient().create_recipe({
            "name": "My Recipe",
            "ingredients": ["2 cups flour", "1 cup milk"],
            "instructions": ["Mix."],
        })
    finally:
        m._client = original
        m.reset_cache()

    assert slug == "my-recipe"
    ings = captured["body"]["recipeIngredient"]
    assert len(ings) == 2
    assert ings[0]["quantity"] == 2.0
    assert ings[0]["unit"] == {"name": "cup"}
    assert ings[0]["food"] == {"name": "flour"}
    assert ings[0]["originalText"] == "2 cups flour"
    # The provider saw the raw ingredient lines.
    assert _FakeParseProvider.last_lines == ["2 cups flour", "1 cup milk"]


@pytest.mark.anyio
async def test_create_recipe_falls_back_to_free_text_when_structured_rejected(monkeypatch):
    # Some Mealie versions 500 on the structured ingredient shape (ztjc); the
    # save must fall back to plain-text ingredients rather than fail.
    from app.config import settings
    from app.services import mealie as m
    import app.dependencies as deps
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test")
    monkeypatch.setattr(settings, "mealie_api_key", "t")
    monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(deps, "get_enrich_provider", lambda: _FakeParseProvider())

    calls = {"n": 0, "final": None}

    async def post_recipes(request):
        return JSONResponse("my-recipe")

    async def patch_recipe(request):
        calls["n"] += 1
        body = await request.json()
        if calls["n"] == 1:
            # The structured attempt is rejected, exactly like Mealie 3.19.
            return JSONResponse(
                {"detail": {"message": "Unknown Error", "error": True,
                            "exception": "ValueError"}}, status_code=500)
        calls["final"] = body
        return JSONResponse({"slug": "my-recipe"})

    app = Starlette(routes=[
        Route("/api/recipes", post_recipes, methods=["POST"]),
        Route("/api/recipes/{slug}", patch_recipe, methods=["PATCH"]),
    ])
    original = m._client
    m._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mealie.test")
    try:
        slug = await m.MealieClient().create_recipe({
            "name": "My Recipe",
            "ingredients": ["2 cups flour", "1 cup milk"],
            "instructions": ["Mix."],
        })
    finally:
        m._client = original
        m.reset_cache()

    assert slug == "my-recipe"
    assert calls["n"] == 2  # structured attempted, then the free-text fallback
    ings = calls["final"]["recipeIngredient"]
    assert ings == [{"note": "2 cups flour"}, {"note": "1 cup milk"}]


@pytest.mark.anyio
async def test_create_recipe_posts_free_text_without_ai(monkeypatch):
    from app.config import settings
    from app.services import mealie as m
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test")
    monkeypatch.setattr(settings, "mealie_api_key", "t")
    monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "")  # no AI configured

    captured: dict = {}
    original = m._client
    m._client = _mock_mealie_client(captured)
    try:
        await m.MealieClient().create_recipe({
            "name": "My Recipe",
            "ingredients": ["2 cups flour", "1 cup milk"],
            "instructions": ["Mix."],
        })
    finally:
        m._client = original
        m.reset_cache()

    # Old behavior preserved exactly: plain note entries, no food/quantity keys.
    assert captured["body"]["recipeIngredient"] == [
        {"note": "2 cups flour"}, {"note": "1 cup milk"}]


@pytest.mark.anyio
async def test_create_recipe_falls_back_on_provider_error(monkeypatch):
    from app.config import settings
    from app.services import mealie as m
    import app.dependencies as deps
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test")
    monkeypatch.setattr(settings, "mealie_api_key", "t")
    monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")

    class _Boom:
        async def parse_ingredients(self, lines):
            raise RuntimeError("model exploded")

    monkeypatch.setattr(deps, "get_enrich_provider", lambda: _Boom())

    captured: dict = {}
    original = m._client
    m._client = _mock_mealie_client(captured)
    try:
        await m.MealieClient().create_recipe({
            "name": "My Recipe",
            "ingredients": ["2 cups flour", "1 cup milk"],
            "instructions": ["Mix."],
        })
    finally:
        m._client = original
        m.reset_cache()

    # A provider error never loses ingredients: they save as free text.
    assert captured["body"]["recipeIngredient"] == [
        {"note": "2 cups flour"}, {"note": "1 cup milk"}]


# ── Parse-ingredients endpoint ───────────────────────────────────────────────

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings
        settings.data_dir = str(tmp_path_factory.mktemp("data"))
        from app.main import app
        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        settings.vision_provider = "gemini"
        settings.gemini_api_key = "test-gemini-key"
        settings.mealie_base_url = "http://mealie.test"
        settings.mealie_api_key = "test-mealie-key"
        settings.auth_required = False
        settings.auth_password = ""
        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


def _endpoint_mock_mealie(captured: dict):
    async def recipe_detail(request):
        return JSONResponse({
            "slug": "my-recipe", "name": "My Recipe", "id": "r1",
            "recipeIngredient": [{"note": "2 cups flour"}, {"note": "salt to taste"}],
        })

    async def patch_recipe(request):
        captured["body"] = await request.json()
        return JSONResponse({"slug": "my-recipe"})

    app = Starlette(routes=[
        Route("/api/recipes/{slug}", recipe_detail, methods=["GET"]),
        Route("/api/recipes/{slug}", patch_recipe, methods=["PATCH"]),
    ])
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mealie.test")


def test_parse_ingredients_endpoint_updates_and_counts(client, monkeypatch):
    from app.services import mealie as m
    import app.dependencies as deps

    class _Fake:
        async def parse_ingredients(self, lines):
            return [
                {"quantity": 2, "unit": "cup", "food": "flour", "note": ""},
                {"quantity": None, "unit": "", "food": "salt", "note": "to taste"},
            ]

    monkeypatch.setattr(deps, "get_enrich_provider", lambda: _Fake())
    captured: dict = {}
    original = m._client
    m._client = _endpoint_mock_mealie(captured)
    try:
        r = client.post("/mealie/recipes/parse-ingredients", json={"slug": "my-recipe"})
    finally:
        m._client = original
        m.reset_cache()

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert body["message"] == "Parsed 2 ingredients."
    ings = captured["body"]["recipeIngredient"]
    assert ings[0]["food"] == {"name": "flour"}
    assert ings[1]["food"] == {"name": "salt"}
    assert ings[1]["quantity"] is None


def test_parse_ingredients_endpoint_gated_on_ai(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "gemini_api_key", "")
    r = client.post("/mealie/recipes/parse-ingredients", json={"slug": "my-recipe"})
    assert r.status_code == 503
    assert r.json()["detail"]["setup_url"] == "/setup"


def test_parse_ingredients_endpoint_gated_on_mealie(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "mealie_base_url", "")
    monkeypatch.setattr(settings, "mealie_api_key", "")
    r = client.post("/mealie/recipes/parse-ingredients", json={"slug": "my-recipe"})
    assert r.status_code == 400
