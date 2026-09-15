"""Section fidelity on the remaining recipe surfaces (FoodAssistant-sz4h).

Four edge cases left over from the zq7k ingredient-sections work:

* The Recipes page editor no longer drops flat lines when a multi-page scan
  returns a MIX of grouped {section, items} objects and bare-string lines:
  ingredientsToText mirrors the server's split_ingredient_sections, ungrouped
  lines land first under no heading, and the textarea round-trips through
  parseIngredientLines to the same headings the server would assign.
* Optimize keeps a draft's ingredient groups: the shared prompt shows the
  headings and asks for the grouped reply form, and the endpoint normalizes
  whatever form the model returns back into the flat lines + parallel
  ingredient_sections shape the editor round-trips.
* On the Line: from_mealie_detail carries ingredient section titles through
  (Mealie's run-start ``title`` mechanism), so the view can render heading rows,
  and the sections survive set_active persistence.
* PUT /mealie/recipes/{slug} applies only the fields actually sent
  (exclude_unset, the /setup/save semantics), so a request that never mentions
  the time fields cannot blank them; an explicit "" still clears.

The JS behavior tests run under node (skipped when absent), and the rendered
pages' inline scripts are syntax-checked with node --check.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

TEMPLATES = SERVICE / "app" / "templates"
_NODE = shutil.which("node")
_TAG = uuid.uuid4().hex[:8]


def _name(base: str) -> str:
    return f"{base} {_TAG}"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    try:
        from app.config import settings

        data_dir = tmp_path_factory.mktemp("data")
        settings.data_dir = str(data_dir)

        from app.main import app

        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        # Native recipe library, no Mealie and no AI unless a test adds one.
        settings.mealie_base_url = ""
        settings.mealie_api_key = ""
        settings.recipes_backend = "native"
        settings.vision_provider = ""
        settings.gemini_api_key = ""
        settings.auth_required = False
        settings.auth_password = ""

        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import Base
    from app.models import db_models  # noqa: F401 - registers the tables

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    engine = create_engine(f"sqlite:///{tmp_path}/store.db")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ── 1. The editor textarea and a mixed-form scan ──────────────────────────────

def _js_function(src: str, name: str) -> str:
    """Extract one top-level function from the template's inline script."""
    start = src.index(f"function {name}")
    body = src[start:]
    return body[: body.index("\n}\n") + 3]


def _run_node(tmp_path: Path, script: str) -> dict:
    harness = tmp_path / "harness.js"
    harness.write_text(script)
    proc = subprocess.run([_NODE, str(harness)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


_MIXED = {
    "ingredients": [
        {"section": "Meat sauce", "items": ["1 lb ground beef", "2 cups tomatoes"]},
        "2 eggs",
        {"section": "Topping", "items": ["1 cup breadcrumbs"]},
        "1 cup milk",
    ],
}


@pytest.mark.skipif(_NODE is None, reason="node is not available")
def test_editor_textarea_keeps_flat_lines_from_a_mixed_scan(tmp_path):
    """A multi-page scan whose pages return mixed grouped+flat forms used to
    drop every bare-string line from the review textarea. Now the flat lines
    render first (the implicit general section) and nothing is lost, and the
    round-trip agrees with the server's split_ingredient_sections on which
    heading each line sits under."""
    src = (TEMPLATES / "recipes.html").read_text()
    script = (
        _js_function(src, "ingredientsToText") + "\n"
        + _js_function(src, "parseIngredientLines") + "\n"
        + f"const mixed = {json.dumps(_MIXED)};\n"
        + "const text = ingredientsToText(mixed);\n"
        + "const rt = parseIngredientLines(text);\n"
        + "console.log(JSON.stringify({text, rt}));\n"
    )
    out = _run_node(tmp_path, script)
    lines = out["text"].split("\n")

    # The two flat lines lead, under no heading; the grouped runs follow.
    assert lines[:2] == ["2 eggs", "1 cup milk"]
    assert "Meat sauce:" in lines and "Topping:" in lines
    for line in ("1 lb ground beef", "2 cups tomatoes", "1 cup breadcrumbs"):
        assert line in lines

    # Round-tripping the textarea assigns each line the same heading the
    # server-side normalizer would ("" for the implicit general section).
    from app.services.recipe_store import split_ingredient_sections
    srv_lines, srv_secs = split_ingredient_sections(_MIXED)
    server_map = {ln: (s or "") for ln, s in zip(srv_lines, srv_secs)}
    client_map = dict(zip(out["rt"]["ingredients"], out["rt"]["ingredient_sections"]))
    assert client_map == server_map
    assert sorted(out["rt"]["ingredients"]) == sorted(srv_lines)


@pytest.mark.skipif(_NODE is None, reason="node is not available")
def test_editor_textarea_unchanged_for_pure_forms(tmp_path):
    """Backward-compat pins: a purely grouped scan and a flat recipe (with or
    without a parallel sections list) render exactly as before, and a
    single-item object without "items" keeps its text."""
    src = (TEMPLATES / "recipes.html").read_text()
    script = (
        _js_function(src, "ingredientsToText") + "\n"
        + """
const grouped = {ingredients: [
  {section: 'Meat sauce', items: ['1 lb ground beef', '2 cups tomatoes']},
  {section: 'Topping', items: ['1 cup breadcrumbs']},
]};
const flat = {ingredients: ['a', 'b']};
const parallel = {ingredients: ['a', 'b', 'c'],
                  ingredient_sections: ['Sauce', 'Sauce', 'Base']};
const single = {ingredients: [{text: 'salt to taste'}]};
console.log(JSON.stringify({
  grouped: ingredientsToText(grouped),
  flat: ingredientsToText(flat),
  parallel: ingredientsToText(parallel),
  single: ingredientsToText(single),
}));
"""
    )
    out = _run_node(tmp_path, script)
    assert out["grouped"] == ("Meat sauce:\n1 lb ground beef\n2 cups tomatoes\n"
                              "Topping:\n1 cup breadcrumbs")
    assert out["flat"] == "a\nb"
    assert out["parallel"] == "Sauce:\na\nb\nBase:\nc"
    assert out["single"] == "salt to taste"


_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                        re.DOTALL | re.IGNORECASE)


def _pin_and_check_inline_js(client, url: str, needles: list[str], tmp_path):
    """Rendered-page pin: the URL serves 200 with the expected JS present, and
    every inline script on the rendered page parses (node --check)."""
    r = client.get(url)
    assert r.status_code == 200, f"{url}: {r.status_code}"
    for needle in needles:
        assert needle in r.text, f"{url} lost {needle!r}"
    if _NODE is None:
        return  # the render pin above still ran
    for i, m in enumerate(_SCRIPT_RE.finditer(r.text)):
        body = m.group(1).strip()
        if not body:
            continue
        f = tmp_path / f"inline{i}.js"
        f.write_text(body)
        proc = subprocess.run([_NODE, "--check", str(f)],
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f"{url} inline script {i}: {proc.stderr}"


def test_recipes_page_renders_with_the_mixed_form_handler(client, tmp_path):
    _pin_and_check_inline_js(client, "/ui/recipes", [
        "function ingredientsToText",
        "function parseIngredientLines",
        # The grouped detector looks at every entry, not just the first.
        "ings.some(e => typeof e === 'object'",
    ], tmp_path)


def test_on_the_line_page_renders_section_heading_rows(client, tmp_path):
    _pin_and_check_inline_js(client, "/ui/current-recipe", [
        "i.section",
        "prevSection",
    ], tmp_path)


# ── 2. Optimize keeps ingredient groups ───────────────────────────────────────

def test_optimize_prompt_asks_for_section_preservation():
    from app.providers.base import _OPTIMIZE_RECIPE_PROMPT

    assert '"section"' in _OPTIMIZE_RECIPE_PROMPT
    assert '"items"' in _OPTIMIZE_RECIPE_PROMPT
    lowered = _OPTIMIZE_RECIPE_PROMPT.lower()
    assert "heading" in lowered
    # The JSON braces stay doubled so .format only fills {recipe}.
    text = _OPTIMIZE_RECIPE_PROMPT.format(recipe="RECIPE-BODY")
    assert "RECIPE-BODY" in text
    assert "{recipe}" not in text
    assert '"ingredients"' in text


def test_format_recipe_for_prompt_shows_group_headings():
    from app.providers.base import format_recipe_for_prompt

    text = format_recipe_for_prompt({
        "name": "Lasagna",
        "ingredients": ["1 lb ground beef", "2 cups tomatoes", "1 cup breadcrumbs"],
        "ingredient_sections": ["Meat sauce", "Meat sauce", "Topping"],
        "instructions": ["Bake."],
    })
    lines = text.split("\n")
    i = lines.index("Meat sauce:")
    assert lines[i + 1:i + 3] == ["- 1 lb ground beef", "- 2 cups tomatoes"]
    j = lines.index("Topping:")
    assert lines[j + 1] == "- 1 cup breadcrumbs"

    # A recipe with no sections renders exactly as before.
    flat = format_recipe_for_prompt({"name": "X", "ingredients": ["a", "b"]})
    assert flat.split("\n") == ["Name: X", "Ingredients:", "- a", "- b",
                                "Instructions:"]


def test_optimize_reply_normalizes_every_ingredient_form(client):
    from app.routers.mealie import _optimized_with_sections

    grouped = _optimized_with_sections({"name": "X", "ingredients": [
        {"section": "Sauce", "items": ["tomato", "basil"]}, "salt"]})
    assert grouped["ingredients"] == ["tomato", "basil", "salt"]
    assert grouped["ingredient_sections"] == ["Sauce", "Sauce", ""]

    flat = _optimized_with_sections({"name": "X", "ingredients": ["a", "b"]})
    assert flat["ingredients"] == ["a", "b"]
    assert "ingredient_sections" not in flat


def test_optimize_endpoint_round_trips_sections(client, monkeypatch):
    """The whole loop: a sectioned draft goes out with its headings, the model
    answers in the grouped form the prompt asks for, and the endpoint hands the
    editor back the flat + parallel shape it round-trips."""
    from app.config import settings
    import app.dependencies as deps

    monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")

    seen: dict = {}

    class _StubProvider:
        async def optimize_recipe(self, recipe):
            seen["recipe"] = recipe
            return {
                "name": "Tidied Lasagna",
                "ingredients": [
                    {"section": "Meat sauce", "items": ["1 lb ground beef"]},
                    {"section": "Topping", "items": ["1 cup breadcrumbs"]},
                ],
                "instructions": ["Bake for 30 minutes."],
            }

    monkeypatch.setattr(deps, "get_enrich_provider", lambda: _StubProvider())
    r = client.post("/mealie/recipes/optimize", json={
        "name": "Lasagna",
        "ingredients": ["1 lb ground beef", "1 cup breadcrumbs"],
        "ingredient_sections": ["Meat sauce", "Topping"],
        "instructions": ["Bake."],
    })
    assert r.status_code == 200, r.text
    recipe = r.json()["recipe"]
    assert recipe["ingredients"] == ["1 lb ground beef", "1 cup breadcrumbs"]
    assert recipe["ingredient_sections"] == ["Meat sauce", "Topping"]
    # The provider was handed the draft's sections, so the prompt showed them.
    assert seen["recipe"]["ingredient_sections"] == ["Meat sauce", "Topping"]


# ── 3. On the Line carries section titles ─────────────────────────────────────

def test_from_mealie_detail_carries_section_titles():
    from app.services import current_recipe

    detail = {"name": "Lasagna", "recipeIngredient": [
        {"title": "Meat sauce", "note": "1 lb ground beef"},
        {"note": "2 cups tomatoes"},
        {"title": "Topping", "note": "1 cup breadcrumbs"},
    ]}
    out = current_recipe.from_mealie_detail(detail, "lasagna")
    assert [i.get("section") for i in out["ingredients"]] == \
        ["Meat sauce", "Meat sauce", "Topping"]


def test_from_mealie_detail_flat_recipe_carries_no_sections():
    from app.services import current_recipe

    out = current_recipe.from_mealie_detail(
        {"name": "Flat", "recipeIngredient": [{"note": "a"}, {"note": "b"}]},
        "flat")
    assert all("section" not in i for i in out["ingredients"])


def test_native_detail_sections_reach_on_the_line(db):
    from app.services import current_recipe, recipe_store

    saved = recipe_store.create_from_parsed(db, {
        "name": "Layered Lasagna",
        "ingredients": ["1 lb ground beef", "2 cups tomatoes", "1 cup breadcrumbs"],
        "ingredient_sections": ["Meat sauce", "Meat sauce", "Topping"],
        "instructions": ["Bake."],
    }, source="photo")
    normalized = current_recipe.from_mealie_detail(saved, saved["slug"])
    assert [i.get("section") for i in normalized["ingredients"]] == \
        ["Meat sauce", "Meat sauce", "Topping"]


def test_sections_survive_set_active_and_a_restart(tmp_path, monkeypatch):
    from app.config import settings
    from app.services import current_recipe as cr

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    cr._active = None
    cr._loaded = False
    try:
        out = cr.set_active({"title": "Lasagna", "ingredients": [
            {"name": "ground beef", "quantity": 1, "unit": "lb",
             "section": "Meat sauce"},
            {"name": "breadcrumbs", "section": "Topping"},
            {"name": "salt"},
        ]})
        assert [i["section"] for i in out["ingredients"]] == \
            ["Meat sauce", "Topping", None]
        # Simulate a restart: drop in-memory state, reload from disk.
        cr._active = None
        cr._loaded = False
        again = cr.get_active()
        assert [i["section"] for i in again["ingredients"]] == \
            ["Meat sauce", "Topping", None]
    finally:
        cr.clear_active()
        cr._active = None
        cr._loaded = False


# ── 4. PUT applies only the fields sent ───────────────────────────────────────

def test_put_omitting_times_keeps_them(client):
    """A PUT from an API caller that never mentions the time fields keeps the
    stored ones (exclude_unset, the /setup/save semantics); the edit form's
    explicit blank still clears."""
    name = _name("Timed Negroni")
    r = client.post("/mealie/recipes/create", json={
        "name": name,
        "servings": "1 drink",
        "prep_time": "3 minutes", "cook_time": "1 minute",
        "total_time": "4 minutes",
        "ingredients": ["1 oz gin"], "instructions": ["Stir."],
    })
    assert r.status_code == 200, r.text
    slug = r.json()["slug"]
    try:
        r = client.put(f"/mealie/recipes/{slug}", json={
            "name": name,
            "ingredients": ["1 oz gin", "1 oz Campari"],
            "instructions": ["Stir with ice."],
        })
        assert r.status_code == 200, r.text
        d = client.get("/mealie/recipes/detail", params={"slug": slug}).json()
        assert d["prep_time"] == "3 minutes"
        assert d["cook_time"] == "1 minute"
        assert d["total_time"] == "4 minutes"
        assert d["ingredients"] == ["1 oz gin", "1 oz Campari"]

        # An explicit blank (what the edit form posts to clear) still clears.
        r = client.put(f"/mealie/recipes/{slug}", json={
            "name": name, "cook_time": "",
            "ingredients": ["1 oz gin"], "instructions": ["Stir."],
        })
        assert r.status_code == 200, r.text
        d = client.get("/mealie/recipes/detail", params={"slug": slug}).json()
        assert d["cook_time"] == ""
        assert d["prep_time"] == "3 minutes"   # untouched fields keep their values
    finally:
        client.delete(f"/recipes/{slug}")
