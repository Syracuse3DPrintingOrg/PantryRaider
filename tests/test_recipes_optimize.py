"""Optimize a recipe draft (FoodAssistant-fjxy).

Drives POST /mealie/recipes/optimize with the AI provider mocked, so no network
is needed. Also asserts the optimize prompt is explicit that ingredients and
quantities must not change, and that a reformat makes timing cues explicit for
the app's timer parser.

Covered:
  * a draft comes back reformatted for review (nothing saved)
  * no AI provider configured -> 503 with the setup pointer
  * an empty draft -> 400
  * the shared prompt forbids ingredient/quantity/technique changes
  * an optimized step's timing cue parses into a timer suggestion
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVICE_DIR = Path(__file__).parent.parent / "service"


class _FakeProvider:
    """Returns a tidied draft and remembers the recipe it was handed."""
    last_recipe = None

    async def optimize_recipe(self, recipe):
        _FakeProvider.last_recipe = recipe
        return {
            "name": recipe["name"],
            "description": "Weeknight chicken.",
            "servings": recipe.get("servings") or "4 servings",
            "total_time": "40 minutes",
            "ingredients": recipe["ingredients"],
            "instructions": ["Brown the chicken.", "Simmer for 20 minutes."],
        }


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


_DRAFT = {
    "name": "Chicken Stew",
    "ingredients": ["2 chicken thighs", "1 cup stock"],
    "instructions": ["cook chicken then simmer about 20 min"],
}


def test_optimize_returns_reformatted_draft(client, monkeypatch):
    import app.dependencies as deps
    monkeypatch.setattr(deps, "get_enrich_provider", lambda: _FakeProvider())

    r = client.post("/mealie/recipes/optimize", json=_DRAFT)
    assert r.status_code == 200, r.text
    body = r.json()
    # Nothing is saved: the endpoint only returns a draft for the review editor.
    assert "slug" not in body
    assert body["recipe"]["name"] == "Chicken Stew"
    # Same ingredients and amounts flow through untouched.
    assert body["recipe"]["ingredients"] == _DRAFT["ingredients"]
    assert _FakeProvider.last_recipe["ingredients"] == _DRAFT["ingredients"]


def test_optimize_timer_cue_parses(client, monkeypatch):
    import app.dependencies as deps
    from app.services.recipe_timers import parse_step_durations
    monkeypatch.setattr(deps, "get_enrich_provider", lambda: _FakeProvider())

    r = client.post("/mealie/recipes/optimize", json=_DRAFT)
    step = r.json()["recipe"]["instructions"][1]   # "Simmer for 20 minutes."
    # The explicit "for 20 minutes" phrasing is what the timer parser recognizes.
    assert parse_step_durations(step) == [("Simmer", 1200)]


def test_optimize_no_provider_returns_503(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "gemini_api_key", "")
    r = client.post("/mealie/recipes/optimize", json=_DRAFT)
    assert r.status_code == 503
    assert r.json()["detail"]["setup_url"] == "/setup"


def test_optimize_empty_draft_returns_400(client, monkeypatch):
    import app.dependencies as deps
    monkeypatch.setattr(deps, "get_enrich_provider", lambda: _FakeProvider())
    r = client.post("/mealie/recipes/optimize",
                    json={"name": "", "ingredients": [], "instructions": []})
    assert r.status_code == 400


def test_optimize_prompt_forbids_content_changes():
    import sys
    sys.path.insert(0, str(_SERVICE_DIR))
    from app.providers.base import _OPTIMIZE_RECIPE_PROMPT
    p = _OPTIMIZE_RECIPE_PROMPT.lower()
    # The instruction must explicitly forbid changing the recipe's substance.
    assert "do not add, remove, or substitute any ingredient" in p
    assert "do not change any quantity" in p
    assert "do not change the cooking method" in p
    # And it must steer timing cues into a parser-friendly form.
    assert '"for n minutes"' in p or "for n minutes" in p
