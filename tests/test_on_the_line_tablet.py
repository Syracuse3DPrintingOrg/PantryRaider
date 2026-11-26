"""On the Line on a tablet in Pantry Raider mode (FoodAssistant-l8in0).

A tablet switched into the whole interface cooks a recipe of its own, the
one its cook mode and voice follow. On the Line there shows that recipe (the
kitchen's while the tablet has none of its own), and its buttons act on it:
the timers, Start cooking, the servings, Cooked and Clear. Any other browser,
and the kitchen display, still shows the kitchen's recipes, courses and all.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.passwords import hash_secret  # noqa: E402
from app.config import settings  # noqa: E402
from app.services import (access, credentials, current_recipe, ha_events,  # noqa: E402
                          screen_recipes, screens, timers)
from page_scripts import template_source  # noqa: E402

PIE = {"title": "Best-Ever Key Lime Pie", "id": "klp", "servings": 8,
       "ingredients": [{"name": "graham cracker crumbs", "quantity": 1.5, "unit": "cups"},
                       {"name": "egg yolks", "quantity": 4}],
       "steps": ["Heat the oven to 350 F.",
                 "Press the crumbs into a dish and bake the crust for 10 minutes.",
                 "Whisk the filling.",
                 "Pour the filling in and bake for 15 minutes."]}
LASAGNA = {"title": "Lasagna", "id": "lasagna", "servings": 6,
           "ingredients": [{"name": "lasagna noodles", "quantity": 12}],
           "steps": ["Boil the noodles for 8 minutes.", "Layer everything in a baking dish.",
                     "Bake for 45 minutes."]}
_NODE = shutil.which("node")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", [], raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    credentials.reset(None, persist=True)
    screens.reset()
    screen_recipes.reset()
    ha_events.reset()
    timers.clear_all()
    current_recipe.clear_active()
    monkeypatch.setattr(screens, "_bandit_screens", lambda: [])
    yield tmp_path
    timers.clear_all()
    screens.reset()
    screen_recipes.reset()
    ha_events.reset()
    current_recipe.clear_active()
    credentials.reset(persist=False)


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"), raising=False)
    from app.routers import current_recipe as cr

    async def consume(_recipe):
        return ["Limes"]

    monkeypatch.setattr(cr, "_consume_active_recipe", consume)
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _tablet(client, sid="ks-echo", name="Echo Show"):
    """A tablet's own key, and a web view of it signed in to the whole
    interface, the way its kiosk app opens Pantry Raider mode."""
    _rec, secret = credentials.mint("screen", name, "manual")
    key = {"X-API-Key": secret}
    r = client.post("/api/screens/register", headers=key, json={
        "screen_id": sid, "label": name, "kind": "kiosk_satellite"})
    assert r.status_code == 200, r.text
    url = client.post("/api/screens/session", headers=key,
                      json={"screen": sid, "page": "app"}).json()["url"]
    from fastapi.testclient import TestClient
    from app.main import app
    view = TestClient(app)
    assert view.get(url, follow_redirects=False).status_code == 303
    return key, view


def _browser():
    from fastapi.testclient import TestClient
    from app.main import app
    admin = TestClient(app)
    assert admin.post("/ui/login", data={"password": "hunter2"},
                      follow_redirects=False).status_code in (302, 303)
    return admin


def _page_data(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="current-recipe-data">(.*?)</script>',
                  html, re.S)
    assert m, "the page data block is missing"
    return json.loads(m.group(1))


# --- which recipe the page shows ----------------------------------------------

def test_a_tablet_sees_its_own_recipe_and_a_browser_the_kitchens(client):
    """The kitchen is making the pie; the Echo Show opened lasagna. On the
    Line on the Echo Show shows lasagna, the same as its cook mode; a browser
    still shows the pie."""
    _key, echo = _tablet(client)
    current_recipe.set_active(PIE)
    screen_recipes.set_recipe("ks-echo", LASAGNA)
    screen_recipes.set_step("ks-echo", screen_recipes.get_recipe("ks-echo"), 1)

    page = echo.get("/ui/current-recipe")
    assert page.status_code == 200 and _page_data(page.text)["tablet"] is True
    # What the page reads in that mode: the tablet's own, at its step, with
    # its timers, its equipment and how often it was made.
    d = echo.get("/api/screens/recipe?full=1&extras=1").json()
    assert d["ok"] and d["source"] == "screen" and d["recipe"]["title"] == "Lasagna"
    assert d["step"] == 2
    assert [s["label"] for s in d["suggestions"]] and all(
        s["step_index"] in (0, 2) for s in d["suggestions"])
    assert isinstance(d["equipment"], list) and "missing_appliances" in d
    assert d["recipe"]["cook_count"] == 0
    # Cook mode on that page reads the very same recipe.
    assert echo.get("/api/screens/recipe?full=1").json()["recipe"]["title"] == "Lasagna"

    admin = _browser()
    page = admin.get("/ui/current-recipe")
    assert page.status_code == 200 and _page_data(page.text)["tablet"] is False
    recipes = admin.get("/current-recipe/all").json()["recipes"]
    assert [r["title"] for r in recipes] == [PIE["title"]]
    assert admin.get("/api/screens/recipe?full=1").json()["recipe"]["title"] == PIE["title"]


def test_a_tablet_with_no_recipe_of_its_own_shows_the_kitchens(client):
    _key, echo = _tablet(client)
    current_recipe.set_active(PIE)
    d = echo.get("/api/screens/recipe?full=1&extras=1").json()
    assert d["ok"] and d["source"] == "kitchen" and d["recipe"]["title"] == PIE["title"]
    # Nothing is open anywhere: the page says so.
    current_recipe.clear_active()
    d = echo.get("/api/screens/recipe?full=1&extras=1").json()
    assert d["ok"] is False and d["result"] == "none" and "equipment" not in d


def test_a_screen_page_session_is_not_a_tablet_on_the_line(client):
    """Only the whole interface opens On the Line; a ticket for a screen's
    own pages cannot reach it at all."""
    _rec, secret = credentials.mint("screen", "ThinkSmart", "manual")
    key = {"X-API-Key": secret}
    client.post("/api/screens/register", headers=key,
                json={"screen_id": "ks-view", "label": "ThinkSmart", "kind": "kiosk_satellite"})
    url = client.post("/api/screens/session", headers=key,
                      json={"screen": "ks-view", "page": "recipe"}).json()["url"]
    from fastapi.testclient import TestClient
    from app.main import app
    narrow = TestClient(app)
    narrow.get(url, follow_redirects=False)
    assert narrow.get("/ui/current-recipe", follow_redirects=False).status_code in (303, 403)
    assert screens.app_session_screen({"role": "screen", "screen_id": "ks-view",
                                       "screen_app": False}) == ""
    assert screens.app_session_screen({"role": "screen", "screen_id": "ks-view",
                                       "screen_app": True}) == "ks-view"
    assert screens.app_session_screen({}) == ""


# --- what the buttons act on --------------------------------------------------

def test_servings_on_a_tablet_change_only_the_tablets_copy(client):
    _key, echo = _tablet(client)
    current_recipe.set_active(PIE)
    # Showing the kitchen's pie, the tablet takes it as its own first.
    r = echo.post("/api/screens/recipe/scale", json={"factor": 2})
    assert r.status_code == 200, r.text
    assert r.json()["recipe"]["scaled_servings"] == 16
    assert screen_recipes.get_recipe("ks-echo")["servings_scale"] == 2
    assert current_recipe.get_active()["servings_scale"] == 1
    # The step it is on stays through a change of servings.
    screen_recipes.set_step("ks-echo", screen_recipes.get_recipe("ks-echo"), 2)
    r = echo.post("/api/screens/recipe/scale", json={"factor": 0.5}).json()
    assert r["recipe"]["scaled_servings"] == 4
    assert screen_recipes.current_step("ks-echo", screen_recipes.get_recipe("ks-echo")) == 2
    # A factor that is not a positive number changes nothing.
    for bad in (0, -1):
        r = echo.post("/api/screens/recipe/scale", json={"factor": bad}).json()
        assert r["recipe"]["servings_scale"] == 0.5
    # Nothing open anywhere: nothing to scale.
    current_recipe.clear_active()
    screen_recipes.clear("ks-echo")
    assert echo.post("/api/screens/recipe/scale", json={"factor": 2}).status_code == 404


def test_cooked_on_a_tablet_puts_the_same_dish_away_in_the_kitchen(client):
    """The tablet took up the kitchen's pie and cooked it. The kitchen's pie
    goes too, so the tablet does not fall back to the dish just finished and
    nobody can use its ingredients up a second time."""
    _key, echo = _tablet(client)
    current_recipe.set_active(PIE)
    echo.post("/api/screens/recipe/step", json={"step": "first", "adopt": True})
    assert screen_recipes.get_recipe("ks-echo")["title"] == PIE["title"]
    r = echo.post("/api/screens/recipe/cooked", json={})
    assert r.status_code == 200, r.text
    assert r.json()["consumed"] == ["Limes"]
    assert screen_recipes.get_recipe("ks-echo") is None
    assert current_recipe.get_active() is None
    assert echo.get("/api/screens/recipe?full=1").json()["ok"] is False
    # A different dish in the kitchen stays where it is.
    current_recipe.set_active(LASAGNA)
    screen_recipes.set_recipe("ks-echo", PIE)
    assert echo.post("/api/screens/recipe/cooked", json={}).status_code == 200
    assert current_recipe.get_active()["title"] == "Lasagna"
    assert echo.get("/api/screens/recipe?full=1").json()["source"] == "kitchen"


def test_clear_on_a_tablet(client):
    _key, echo = _tablet(client)
    # The tablet's own and the kitchen's are the same dish: both go.
    current_recipe.set_active(LASAGNA)
    screen_recipes.set_recipe("ks-echo", LASAGNA)
    r = echo.post("/api/screens/recipe/clear", json={})
    assert r.status_code == 200 and r.json()["result"] == "cleared"
    assert screen_recipes.get_recipe("ks-echo") is None and current_recipe.get_active() is None
    # A different dish in the kitchen stays, and the page shows it next.
    current_recipe.set_active(PIE)
    screen_recipes.set_recipe("ks-echo", LASAGNA)
    echo.post("/api/screens/recipe/clear", json={})
    assert current_recipe.get_active()["title"] == PIE["title"]
    assert echo.get("/api/screens/recipe?full=1").json()["source"] == "kitchen"
    # Showing the kitchen's, Clear puts the kitchen's away, as it always has.
    screen_recipes.set_step("", current_recipe.get_active(), 2)
    echo.post("/api/screens/recipe/clear", json={})
    assert current_recipe.get_active() is None
    current_recipe.set_active(PIE)
    assert screen_recipes.current_step("", current_recipe.get_active()) is None
    current_recipe.clear_active()
    assert echo.post("/api/screens/recipe/clear", json={}).json()["result"] == "none"


def test_a_recipe_picked_on_a_tablet_becomes_its_own(client, monkeypatch):
    """Cook on the tablet's Recipes or What Can I Cook? page sets the
    kitchen's current recipe, as it always has, and the tablet's own too, so
    On the Line there opens on the dish just picked."""
    _key, echo = _tablet(client)
    screen_recipes.set_recipe("ks-echo", LASAGNA)
    r = echo.post("/current-recipe", json={"title": "Soup", "id": "soup", "steps": ["Stir."]})
    assert r.status_code == 200, r.text
    assert current_recipe.get_active()["title"] == "Soup"
    assert screen_recipes.get_recipe("ks-echo")["title"] == "Soup"
    assert echo.get("/api/screens/recipe?full=1").json()["recipe"]["title"] == "Soup"

    from app.services import recipe_source, recipe_store
    monkeypatch.setattr(recipe_source, "active_backend", lambda: recipe_source.BACKEND_NATIVE)
    monkeypatch.setattr(recipe_store, "detail", lambda _db, slug: {
        "name": "Key Lime Pie", "slug": slug,
        "recipeInstructions": [{"text": s} for s in PIE["steps"]]})
    r = echo.post("/current-recipe/from-mealie", json={"slug": "klp"})
    assert r.status_code == 200, r.text
    assert screen_recipes.get_recipe("ks-echo")["title"] == "Key Lime Pie"
    # A course is added to the kitchen's meal and leaves the tablet's alone.
    echo.post("/current-recipe/from-mealie", json={"slug": "bars", "as_course": True})
    assert screen_recipes.get_recipe("ks-echo")["id"] == "klp"

    # Picked in a browser, the kitchen's changes and the tablet keeps its own.
    admin = _browser()
    admin.post("/current-recipe", json={"title": "Stew", "id": "stew", "steps": ["Simmer."]})
    assert current_recipe.get_active()["title"] == "Stew"
    assert screen_recipes.get_recipe("ks-echo")["title"] == "Key Lime Pie"


def test_who_may_scale_or_clear_a_screens_recipe():
    for path in ("/api/screens/recipe/scale", "/api/screens/recipe/clear"):
        assert access.route_family("POST", path) == "kitchen"
        for scope, ok in (("screen_app", True), ("viewer", True), ("screen", False),
                          ("ha", False), ("ha_rest", False), ("display", False),
                          ("reader", False)):
            assert access.allows_raw(scope, "POST", path, kiosk_is_admin=False) is ok, (scope, path)


def test_courses_stay_with_the_kitchen():
    """A tablet shows one recipe, so Add course is left off its pages."""
    cook = (SERVICE / "app/static/js/pages/cook.js").read_text()
    buttons = re.findall(r'<button class="([^"]*)"[^>]*>(?:(?!</button>).)*Add course</button>',
                         cook, re.S)
    assert len(buttons) == 2 and all("pr-tablet-hide" in c for c in buttons)
    assert ".pr-tablet-hide{display:none!important}" in (
        SERVICE / "app/static/js/screen-app.js").read_text()


# --- the page script ------------------------------------------------------------

def _extract(src: str, name: str) -> str:
    spec = importlib.util.spec_from_file_location(
        "_recipe_output_escaping", Path(__file__).with_name("test_recipe_output_escaping.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.extract_function(src, name)


@pytest.mark.skipif(_NODE is None, reason="node is not available")
@pytest.mark.parametrize("tablet,screen_status,asked,shown,tablet_after", [
    # A browser reads the kitchen's recipes, exactly as before.
    (False, 200, ["current-recipe/all"], "Best-Ever Key Lime Pie", False),
    # A tablet reads its own, and nothing of the kitchen's.
    (True, 200, ["api/screens/recipe?full=1&extras=1"], "Lasagna", True),
    # A tablet this kitchen no longer knows shows the kitchen's.
    (True, 404, ["api/screens/recipe?full=1&extras=1", "current-recipe/all"],
     "Best-Ever Key Lime Pie", False),
])
def test_the_page_loads_the_tablets_recipe_in_node(tablet, screen_status, asked, shown,
                                                   tablet_after):
    src = template_source("current-recipe.html")
    functions = "\n".join(_extract(src, n) for n in ("loadCurrentRecipe", "loadTabletRecipe"))
    script = """
const asked = [];
const answers = {
  'api/screens/recipe?full=1&extras=1': [%d, {ok: true, source: 'screen', step: 2,
    recipe: {title: 'Lasagna', steps: ['Boil.', 'Layer.', 'Bake for 45 minutes.']},
    suggestions: [{label: 'Bake', seconds: 2700, step_index: 2}], equipment: []}],
  'current-recipe/all': [200, {recipes: [{slot: 0, title: 'Best-Ever Key Lime Pie'}]}],
};
globalThis.fetch = async (url) => { asked.push(url); const [status, body] = answers[url];
  return {ok: status === 200, status, statusText: 'x', json: async () => body}; };
globalThis.document = {getElementById: () => ({querySelector: () => ({})})};
var crTablet = %s, crAll = [], crFocus = 0, crRecipe = null, crSuggestions = [], crStep = null;
const calls = [];
function show() {} function hide() {}
function renderCourseStrip() {} function renderRecipe() { calls.push('render'); }
function renderEquipment() {} function fillTimerButtons() { calls.push('timers'); }
function paintSteps() {} function startTimerPolling() {}
async function loadEquipment() {} async function loadTimerSuggestions() { calls.push('kitchen-timers'); }
%s
(async () => {
  await loadCurrentRecipe();
  console.log(JSON.stringify({asked, title: crRecipe && crRecipe.title, crTablet, crStep,
    timers: crSuggestions.length, calls}));
})();
""" % (screen_status, "true" if tablet else "false", functions)
    out = subprocess.run([_NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["asked"] == asked
    assert res["title"] == shown
    assert res["crTablet"] is tablet_after
    if tablet_after:
        assert res["crStep"] == 2 and res["timers"] == 1
        assert res["calls"] == ["render", "timers"]
    else:
        assert "kitchen-timers" in res["calls"]
