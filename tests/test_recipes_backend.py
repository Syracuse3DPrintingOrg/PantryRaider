"""The recipe backend seam and the one-click Mealie migration (FoodAssistant-zwwe).

Covers:
  * active_backend derivation: explicit setting wins; unset keeps Mealie only
    when Mealie is configured (existing installs), native otherwise (new ones).
  * The /mealie recipe endpoints working with NO Mealie configured when the
    backend is native: create, list/search, detail (quick-view shape),
    Cook this (current-recipe/from-mealie), suggest, ready-count, delete.
  * URL import going through the in-process scraper on the native backend.
  * POST /recipes/migrate-from-mealie: copies every Mealie recipe into the
    native store (read-only toward Mealie), skips duplicates on a re-run, and
    flips recipes_backend to native.

Mealie HTTP and the scraper are mocked; no network. The app's real SQLite is
used (as other router tests do), so recipe names carry a unique suffix and the
tests clean up after themselves.
"""
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"

_TAG = uuid.uuid4().hex[:8]


def _name(base: str) -> str:
    return f"{base} {_TAG}"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings

        data_dir = tmp_path_factory.mktemp("data")
        settings.data_dir = str(data_dir)

        from app.main import app

        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        # No Mealie and no AI: the native backend must carry the page alone.
        settings.mealie_base_url = ""
        settings.mealie_api_key = ""
        settings.recipes_backend = ""
        settings.vision_provider = ""
        settings.gemini_api_key = ""
        settings.auth_required = False
        settings.auth_password = ""

        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


def _cleanup(client, slugs):
    for slug in slugs:
        client.delete(f"/recipes/{slug}")


# ── Backend derivation ────────────────────────────────────────────────────────

def test_active_backend_derivation(client):
    from app.config import settings
    from app.services import recipe_source

    settings.recipes_backend = ""
    settings.mealie_base_url = ""
    settings.mealie_api_key = ""
    assert recipe_source.active_backend() == "native"

    settings.mealie_base_url = "http://mealie.test"
    settings.mealie_api_key = "key"
    assert recipe_source.active_backend() == "mealie"

    # An explicit choice always wins over the derived default.
    settings.recipes_backend = "native"
    assert recipe_source.active_backend() == "native"
    settings.recipes_backend = "mealie"
    settings.mealie_base_url = ""
    settings.mealie_api_key = ""
    assert recipe_source.active_backend() == "mealie"


def test_native_badge():
    from app.services.recipe_source import source_badge
    assert source_badge("native", False)["label"] == "My recipes"
    assert source_badge("native", True)["label"] == "Imported"


# ── Native CRUD through the /mealie endpoints ────────────────────────────────

def test_native_create_list_detail_cook_delete(client):
    name = _name("Backend Chili")
    r = client.post("/mealie/recipes/create", json={
        "name": name,
        "description": "Test chili.",
        "servings": "4 servings",
        "total_time": "45 minutes",
        "ingredients": ["2 cups kidney beans", "1 tbsp chili powder"],
        "instructions": ["Simmer.", "Serve."],
    })
    assert r.status_code == 200, r.text
    slug = r.json()["slug"]
    assert r.json()["mealie_url"] is None

    try:
        # List and search work with no Mealie at all.
        r = client.get("/mealie/recipes", params={"search": _TAG, "mine": True})
        assert r.status_code == 200
        rows = [x for x in r.json() if x["slug"] == slug]
        assert rows and rows[0]["source"] == "mealie"
        assert rows[0]["badge"]["label"] == "My recipes"

        # Quick-view detail in the preview shape, with no Mealie link.
        r = client.get("/mealie/recipes/detail", params={"slug": slug})
        assert r.status_code == 200
        d = r.json()
        assert d["name"] == name
        assert d["ingredients"] == ["2 cups kidney beans", "1 tbsp chili powder"]
        assert d["instructions"] == ["Simmer.", "Serve."]
        assert d["mealie_url"] is None

        # Cook this: the recipe becomes the Current Recipe from the store.
        r = client.post("/current-recipe/from-mealie", json={"slug": slug})
        assert r.status_code == 200
        recipe = r.json()["recipe"]
        assert recipe["title"] == name
        assert recipe["id"] == slug
        client.delete("/current-recipe")

        # Suggest and the deck ready-count consult the native library.
        r = client.get("/mealie/suggest")
        assert r.status_code == 200
        assert r.json()["recipes_considered"] >= 1
        r = client.get("/mealie/suggest/ready-count")
        assert r.status_code == 200
        assert "count" in r.json()
    finally:
        r = client.delete(f"/recipes/{slug}")
        assert r.status_code == 200

    assert client.get("/mealie/recipes/detail", params={"slug": slug}).status_code == 404
    assert client.delete(f"/recipes/{slug}").status_code == 404


def test_native_import_url_uses_scraper(client, monkeypatch):
    from app.services import recipe_scrape

    name = _name("Scraped Pie")

    async def fake_scrape(url):
        assert url == "https://example.com/pie"
        return {"name": name, "description": "", "servings": "8",
                "total_time": "1 hr", "ingredients": ["apples", "crust"],
                "instructions": ["Bake."], "source": url,
                "source_url": url, "image": None}

    monkeypatch.setattr(recipe_scrape, "scrape_url", fake_scrape)
    r = client.post("/mealie/recipes/import-url",
                    json={"url": "https://example.com/pie"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["saved"] is True
    slug = d["slug"]
    try:
        detail = client.get("/mealie/recipes/detail", params={"slug": slug}).json()
        assert detail["name"] == name
        # An imported recipe carries its origin badge in the browse list.
        rows = client.get("/mealie/recipes", params={"search": _TAG}).json()
        row = next(x for x in rows if x["slug"] == slug)
        assert row["badge"]["label"] == "Imported"
    finally:
        _cleanup(client, [slug])


def test_native_import_url_bad_url(client):
    r = client.post("/mealie/recipes/import-url", json={"url": "ftp://nope"})
    assert r.status_code == 400


def test_native_import_file(client):
    name = _name("File Soup")
    payload = ('{"name": "%s", "ingredients": ["water", "salt"], '
               '"instructions": ["Boil."]}' % name).encode()
    r = client.post("/mealie/recipes/import-file",
                    files={"file": ("soup.json", payload, "application/json")})
    assert r.status_code == 200, r.text
    slug = r.json()["slug"]
    try:
        assert r.json()["mealie_url"] is None
        detail = client.get("/mealie/recipes/detail", params={"slug": slug}).json()
        assert detail["ingredients"] == ["water", "salt"]
    finally:
        _cleanup(client, [slug])


# ── Editable times, blank when unstated (FoodAssistant-u65k) ───────────────────

def test_native_times_round_trip_and_blank_stays_blank(client):
    """Servings and prep/cook/total time round-trip through create and edit, and
    a field the source never stated (a cocktail has no cook time) persists blank,
    never a fake default."""
    name = _name("Negroni")
    r = client.post("/mealie/recipes/create", json={
        "name": name,
        "ingredients": ["1 oz gin", "1 oz Campari", "1 oz sweet vermouth"],
        "instructions": ["Stir with ice, strain."],
        # No servings and no times supplied.
    })
    assert r.status_code == 200, r.text
    slug = r.json()["slug"]
    try:
        d = client.get("/mealie/recipes/detail", params={"slug": slug}).json()
        assert d["servings"] == "" and d["prep_time"] == "" \
            and d["cook_time"] == "" and d["total_time"] == ""

        # The edit form sets three of the four; cook time stays blank on purpose.
        r = client.put(f"/mealie/recipes/{slug}", json={
            "name": name,
            "servings": "1 drink", "prep_time": "3 minutes",
            "cook_time": "", "total_time": "3 minutes",
            "ingredients": ["1 oz gin", "1 oz Campari", "1 oz sweet vermouth"],
            "instructions": ["Stir with ice, strain."],
        })
        assert r.status_code == 200, r.text

        d = client.get("/mealie/recipes/detail", params={"slug": slug}).json()
        assert d["servings"] == "1 drink"
        assert d["prep_time"] == "3 minutes"
        assert d["total_time"] == "3 minutes"
        assert d["cook_time"] == ""   # left blank, still blank
    finally:
        _cleanup(client, [slug])


# ── Ingredient sections end to end (FoodAssistant-zq7k) ────────────────────────

def test_native_sections_round_trip_through_endpoints(client):
    """A sectioned recipe created via the API groups in the detail shape the edit
    form and preview read, survives an edit that renames a group, and a flat
    recipe carries no headings."""
    name = _name("Sectioned Lasagna")
    r = client.post("/mealie/recipes/create", json={
        "name": name,
        "ingredients": ["1 lb ground beef", "2 cups tomatoes", "1 cup breadcrumbs"],
        "ingredient_sections": ["Meat sauce", "Meat sauce", "Topping"],
        "instructions": ["Assemble and bake."],
    })
    assert r.status_code == 200, r.text
    slug = r.json()["slug"]
    try:
        d = client.get("/mealie/recipes/detail", params={"slug": slug}).json()
        assert d["ingredients"] == \
            ["1 lb ground beef", "2 cups tomatoes", "1 cup breadcrumbs"]
        # The detail shape the editor and preview consume carries a heading per
        # line so the groups render and re-edit without loss.
        assert d["ingredient_sections"] == ["Meat sauce", "Meat sauce", "Topping"]

        # Rename the first group; the edit persists and reloads.
        r = client.put(f"/mealie/recipes/{slug}", json={
            "name": name,
            "ingredients": ["1 lb ground beef", "2 cups tomatoes", "1 cup breadcrumbs"],
            "ingredient_sections": ["Bolognese", "Bolognese", "Topping"],
            "instructions": ["Assemble and bake."],
        })
        assert r.status_code == 200, r.text
        d = client.get("/mealie/recipes/detail", params={"slug": slug}).json()
        assert d["ingredient_sections"] == ["Bolognese", "Bolognese", "Topping"]
    finally:
        _cleanup(client, [slug])


def test_native_flat_recipe_has_empty_sections(client):
    """Backward-compat: a recipe created with no ingredient_sections reports an
    all-empty parallel list, so the UI renders it as one flat group as before."""
    name = _name("Flat Chili")
    r = client.post("/mealie/recipes/create", json={
        "name": name,
        "ingredients": ["2 cups beans", "1 tbsp chili powder"],
        "instructions": ["Simmer."],
    })
    assert r.status_code == 200, r.text
    slug = r.json()["slug"]
    try:
        d = client.get("/mealie/recipes/detail", params={"slug": slug}).json()
        assert d["ingredient_sections"] == ["", ""]
        assert not any(d["ingredient_sections"])
    finally:
        _cleanup(client, [slug])


def test_mealie_parsed_preserves_and_omits_sections():
    """The Mealie-to-native migration reducer carries a recipe's ingredient groups
    across (title on the run-start entry, denormalized onto its lines) and adds no
    section key at all for an ungrouped recipe (FoodAssistant-zq7k)."""
    from app.routers.recipes import _mealie_parsed

    parsed, _ = _mealie_parsed({
        "name": "Grouped",
        "recipeIngredient": [
            {"title": "Sauce", "note": "tomato"},
            {"note": "basil"},
            {"title": "Base", "note": "flour"},
        ],
        "recipeInstructions": [{"text": "Mix."}],
    })
    assert parsed["ingredients"] == ["tomato", "basil", "flour"]
    assert parsed["ingredient_sections"] == ["Sauce", "Sauce", "Base"]

    flat, _ = _mealie_parsed({
        "name": "Flat",
        "recipeIngredient": [{"note": "a"}, {"note": "b"}],
        "recipeInstructions": [{"text": "x"}],
    })
    assert "ingredient_sections" not in flat


# ── Migration ────────────────────────────────────────────────────────────────

_MIGRATE_DETAILS = [
    {
        "id": "aaa-111",
        "name": _name("Mealie Chili"),
        "slug": f"mealie-chili-{_TAG}",
        "description": "From Mealie.",
        "recipeYield": "4 servings",
        "totalTime": "45 minutes",
        "orgURL": "https://example.com/original",
        "recipeIngredient": [
            {"display": "2 cups kidney beans", "quantity": 2,
             "unit": {"name": "cups"}, "food": {"name": "kidney beans"}},
            {"note": "salt to taste"},
        ],
        "recipeInstructions": [{"text": "Simmer."}, {"text": "Serve."}],
        "tags": [{"name": "dinner"}],
    },
    {
        "id": "bbb-222",
        "name": _name("Mealie Salad"),
        "slug": f"mealie-salad-{_TAG}",
        "recipeYield": "2",
        "recipeIngredient": [{"note": "greens"}],
        "recipeInstructions": [{"text": "Toss."}],
    },
]


def test_migrate_requires_mealie(client):
    from app.config import settings
    settings.mealie_base_url = ""
    settings.mealie_api_key = ""
    r = client.post("/recipes/migrate-from-mealie")
    assert r.status_code == 400


def test_migrate_from_mealie(client, monkeypatch):
    from app.config import settings
    from app.services import recipe_store
    from app.services.mealie import MealieClient

    settings.mealie_base_url = "http://mealie.test"
    settings.mealie_api_key = "key"
    settings.recipes_backend = ""

    async def fake_library(self, limit=200):
        return [dict(d) for d in _MIGRATE_DETAILS]

    async def no_image(url, headers=None):
        return None

    monkeypatch.setattr(MealieClient, "get_recipes_with_ingredients", fake_library)
    monkeypatch.setattr(recipe_store, "fetch_image", no_image)

    slugs = [d["slug"] for d in _MIGRATE_DETAILS]
    try:
        r = client.post("/recipes/migrate-from-mealie")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["imported"] == 2
        assert d["skipped"] == 0
        assert d["errors"] == []
        # The install now uses the native library.
        assert settings.recipes_backend == "native"

        # The Mealie slug is kept, so cook counts keyed on it carry over.
        detail = client.get("/mealie/recipes/detail",
                            params={"slug": slugs[0]}).json()
        assert detail["name"] == _MIGRATE_DETAILS[0]["name"]
        assert detail["ingredients"][0] == "2 cups kidney beans"
        assert detail["instructions"] == ["Simmer.", "Serve."]

        # Idempotent: a second run skips everything.
        r = client.post("/recipes/migrate-from-mealie")
        assert r.status_code == 200
        assert r.json()["imported"] == 0
        assert r.json()["skipped"] == 2
    finally:
        _cleanup(client, slugs)


def test_migrate_refused_on_satellite(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    # A configured satellite (so the setup-redirect middleware stays out of
    # the way) still may not migrate: the store lives on the main server.
    monkeypatch.setattr(settings, "remote_server_url", "http://server.test")
    monkeypatch.setattr(settings, "upstream_api_key", "up-key")
    r = client.post("/recipes/migrate-from-mealie")
    assert r.status_code == 400
