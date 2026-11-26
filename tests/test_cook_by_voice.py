"""Cooking by voice and in cook mode: a recipe and a current step per
screen, answering "which one?", the recipe the language model reads, the
step timers a recipe offers, and the routes and scopes behind them."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.passwords import hash_secret  # noqa: E402
from app.config import settings  # noqa: E402
from app.services import (access, credentials, current_recipe, ha_events,  # noqa: E402
                          recipe_find, recipe_timers, screen_pull, screen_recipes, screens,
                          timers)

PIE = {"title": "Best-Ever Key Lime Pie", "id": "klp", "servings": 8,
       "ingredients": [{"name": "graham cracker crumbs", "quantity": 1.5, "unit": "cups",
                        "section": "Crust"},
                       {"name": "egg yolks", "quantity": 4}],
       "steps": ["Heat the oven to 350 F.",
                 "Press the crumbs into a dish and bake the crust for 10 minutes.",
                 "Whisk the filling.",
                 "Pour the filling in and bake for 15 minutes.",
                 "Cool for 30 minutes, then chill for 3 hours."]}
LIBRARY = [{"slug": "klp", "name": "Best-Ever Key Lime Pie"},
           {"slug": "bars", "name": "Key Lime Pie Bars"},
           {"slug": "lasagna", "name": "Lasagna"},
           {"slug": "lasagna-soup", "name": "Lasagna Soup"}]


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
    monkeypatch.setattr(screens, "_bandit_screens", lambda: [
        {"id": "bandit7", "label": "Counter", "kind": "bandit", "online": True,
         "last_seen": None, "targetable": True}])
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
    from app.routers import screens as router

    async def names(_db, _query):
        return LIBRARY

    async def detail(_db, slug):
        if slug == "klp":
            return {"name": PIE["title"], "slug": "klp",
                    "recipeIngredient": [{"note": "1 1/2 cups graham cracker crumbs"}],
                    "recipeInstructions": [{"text": s} for s in PIE["steps"]]}
        return {"name": slug.title(), "slug": slug,
                "recipeInstructions": [{"text": "Bake for 45 minutes."}]}

    monkeypatch.setattr(router, "_recipe_names", names)
    monkeypatch.setattr(router, "_recipe_detail", detail)
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _key(scope: str, name: str = "k") -> dict:
    _rec, secret = credentials.mint(scope, name, "manual")
    return {"X-API-Key": secret}


def _tablet(client, sid="ks-echo", name="Echo Show"):
    headers = _key("screen", name)
    r = client.post("/api/screens/register", headers=headers, json={
        "screen_id": sid, "label": name, "kind": "kiosk_satellite"})
    assert r.status_code == 200, r.text
    return headers


# --- answering "which one?" ---------------------------------------------------

OFFERED = [{"slug": "klp", "title": "Best-Ever Key Lime Pie"},
           {"slug": "bars", "title": "Key Lime Pie Bars"},
           {"slug": "plain", "title": "Key Lime Pie"}]


@pytest.mark.parametrize("said,slug", [
    ("the second one", "bars"), ("number two", "bars"), ("2", "bars"),
    ("the first one", "klp"), ("the last one", "plain"), ("the third", "plain"),
    ("the best-ever one", "klp"), ("best ever", "klp"), ("the bars", "bars"),
    ("just the key lime pie", "plain"), ("key lime pie", "plain"),
])
def test_an_answer_picks_one_of_the_offered(said, slug):
    assert recipe_find.resolve_choice(said, OFFERED)["slug"] == slug


@pytest.mark.parametrize("said", ["the lasagna", "the sixth one", "", "something else"])
def test_an_answer_that_picks_none_is_none(said):
    assert recipe_find.resolve_choice(said, OFFERED) is None


def test_lets_cook_finds_the_dish():
    for said in ("let's cook a key lime pie", "I want to make key lime pie"):
        out = recipe_find.choose(said, LIBRARY)
        assert out["result"] == "ambiguous", said
        assert {c["slug"] for c in out["candidates"]} == {"klp", "bars"}


# --- the shape a model answers from -------------------------------------------

def test_describe_gives_lines_numbered_steps_and_timers():
    recipe = current_recipe._serialize(current_recipe._normalize(PIE))
    sug = recipe_timers.suggestions_for_recipe(recipe)
    running = [{"label": "Bake crust", "total_seconds": 600, "remaining_seconds": 300,
                "expired": False, "state": "running", "id": 4}]
    out = screen_recipes.describe(recipe, sug, running, {"id": "ks", "label": "Echo Show"},
                                  "screen", 1)
    assert out["ok"] and out["title"] == PIE["title"] and out["servings"] == 8
    assert out["ingredients"] == ["Crust: 1 1/2 cups graham cracker crumbs", "4 egg yolks"]
    assert [s["number"] for s in out["steps"]] == [1, 2, 3, 4, 5]
    assert out["step"] == 2 and out["current"]["number"] == 2
    assert out["steps"][1]["timers"] == [{"label": "Bake crust", "seconds": 600,
                                          "duration": "10 minutes", "running": True}]
    assert out["timers"][0] == {"label": "Bake crust", "seconds": 600,
                                "duration": "10 minutes", "step": 2, "running": True,
                                "remaining_seconds": 300, "timer_id": 4}
    assert out["speech"] == ("Best-Ever Key Lime Pie is open on the Echo Show. "
                             "It has 5 steps. You are on step 2.")
    none = screen_recipes.describe(None, screen={"id": "ks", "label": "Echo Show"})
    assert none == {"ok": False, "result": "none", "screen": {"id": "ks", "label": "Echo Show"},
                    "speech": "No recipe is open on the Echo Show. What are you cooking?"}


def test_reading_steps():
    recipe = current_recipe._serialize(current_recipe._normalize(PIE))
    d = screen_recipes.describe(recipe, recipe_timers.suggestions_for_recipe(recipe))
    out, now = screen_recipes.step_answer(d, "next", None)
    assert now == 0 and out["speech"] == "Step 1 of 5: Heat the oven to 350 F."
    out, now = screen_recipes.step_answer(d, "next", 0)
    assert now == 1 and out["speech"].endswith(
        "This step has a 10 minute timer: say start the timer.")
    assert screen_recipes.step_answer(d, "back", 1)[1] == 0
    assert screen_recipes.step_answer(d, "repeat", 3)[1] == 3
    assert screen_recipes.step_answer(d, "4", None)[1] == 3
    out, now = screen_recipes.step_answer(d, "next", 4)
    assert out["result"] == "last_step" and now == 4
    assert screen_recipes.step_answer(d, "9", None)[0]["result"] == "no_such_step"
    assert "last step" in screen_recipes.step_answer(d, "last", None)[0]["speech"]


def test_amounts_and_lengths_are_said_plainly():
    assert screen_recipes.amount_text(1.5) == "1 1/2"
    assert screen_recipes.amount_text(0.333) == "1/3"
    assert screen_recipes.amount_text(2.0) == "2"
    assert screen_recipes.spoken(45) == "45 seconds"
    assert screen_recipes.spoken(5400) == "1 hour and 30 minutes"
    assert recipe_timers.next_note(PIE["steps"], 1) == "Next: Whisk the filling."
    assert recipe_timers.next_note(PIE["steps"], 4) == ""


# --- per screen recipe and step -------------------------------------------------

def test_each_screen_keeps_its_recipe_and_step(store):
    screen_recipes.set_recipe("ks-a", PIE)
    screen_recipes.set_recipe("ks-b", {"title": "Soup", "steps": ["Stir.", "Serve."]})
    a, b = screen_recipes.get_recipe("ks-a"), screen_recipes.get_recipe("ks-b")
    assert a["title"] == PIE["title"] and b["title"] == "Soup"
    assert screen_recipes.set_step("ks-a", a, 3) == 3
    assert screen_recipes.set_step("ks-b", b, 9) == 1          # clamped
    assert screen_recipes.current_step("ks-a", a) == 3
    # A step belongs to the recipe it was set on.
    assert screen_recipes.current_step("ks-a", b) is None
    # It survives a restart (the state file), and a new recipe clears it.
    screen_recipes._writer.wait_idle()
    screen_recipes._rows, screen_recipes._steps, screen_recipes._mtime = {}, {}, None
    assert screen_recipes.current_step("ks-a", screen_recipes.get_recipe("ks-a")) == 3
    screen_recipes.set_recipe("ks-a", PIE)
    assert screen_recipes.current_step("ks-a", screen_recipes.get_recipe("ks-a")) is None
    # A tablet with no recipe of its own falls back to the kitchen's; a
    # Bandit does not (it keeps its own on the device).
    current_recipe.set_active({"title": "Kitchen", "steps": ["x"]})
    assert screen_recipes.recipe_for({"id": "ks-c", "kind": "kiosk_satellite"})[1] == "kitchen"
    # Asked about that screen alone, it has none.
    assert screen_recipes.recipe_for({"id": "ks-c", "kind": "kiosk_satellite"}, own=True) == (
        None, "screen")
    assert screen_recipes.recipe_for({"id": "bandit7", "kind": "bandit"}) == (None, "screen")
    assert screen_recipes.recipe_for(None)[0]["title"] == "Kitchen"


def test_a_bandit_follows_a_step_it_is_sent(store):
    current_recipe.set_active(PIE)
    assert screen_pull.apply_event({"type": "step", "step": 3})
    assert screen_recipes.current_step("", current_recipe.get_active()) == 2


# --- routes -------------------------------------------------------------------

def test_lets_cook_two_turns_then_steps_and_timers(client):
    tablet = _tablet(client)
    ha = _key("ha", "HA")
    since = screens.poll("ks-echo", 999999999)["last_id"]
    r = client.post("/api/screens/show-recipe", headers=ha,
                    json={"query": "let's cook key lime pie", "screen": "ks-echo",
                          "page": "cook"}).json()
    assert r["result"] == "ambiguous" and r["speech"].endswith("Which one?")
    # The answer: no page given, the question asked for cook mode.
    r = client.post("/api/screens/show-recipe", headers=ha,
                    json={"choice": "the best-ever one", "screen": "ks-echo"}).json()
    assert r["result"] == "shown" and r["page"] == "cook" and r["step"] == 1
    assert r["speech"] == ("Let's cook Best-Ever Key Lime Pie on the Echo Show. "
                           "Step 1 of 5: Heat the oven to 350 F.")
    assert [e["path"] for e in screens.poll("ks-echo", since)["events"]] == ["screen/cook"]
    # The tablet's recipe is its own; the kitchen's current one follows too.
    assert current_recipe.get_active()["title"] == PIE["title"]
    current_recipe.set_active({"title": "Something else", "steps": ["x"]})

    got = client.get("/api/screens/recipe?screen=ks-echo", headers=ha).json()
    assert got["ok"] and got["recipe"]["title"] == PIE["title"] and got["step"] == 1
    assert got["source"] == "screen"
    # The tablet reads its own recipe with its own key.
    assert client.get("/api/screens/recipe?screen=ks-echo", headers=tablet).json()["step"] == 1

    r = client.post("/api/screens/recipe/step", headers=ha,
                    json={"screen": "ks-echo", "step": "next"}).json()
    assert r["step"] == 2 and r["current"]["number"] == 2
    assert screens.poll("ks-echo", since)["events"][-1] == {
        **screens.poll("ks-echo", since)["events"][-1], "type": "step", "step": 2}

    # "Start the timer": the current step's.
    r = client.post("/api/screens/recipe/timer", headers=ha,
                    json={"screen": "ks-echo", "origin": "assist_satellite.k"}).json()
    assert r["result"] == "started" and r["timer"]["label"] == "Bake crust"
    assert r["recipe"] == {"title": PIE["title"]}
    assert r["timer"]["note"] == "Next: Whisk the filling."
    assert r["timer"]["origin"] == "assist_satellite.k"
    # Never twice.
    r = client.post("/api/screens/recipe/timer", headers=ha,
                    json={"screen": "ks-echo", "step": 2}).json()
    assert r["result"] == "running" and "already running" in r["speech"]
    # Words: the bake that is not already running.
    r = client.post("/api/screens/recipe/timer", headers=ha,
                    json={"screen": "ks-echo", "query": "baking it"}).json()
    assert r["result"] == "started" and r["step"] == 4
    # A step with two timers asks, with only that step's.
    r = client.post("/api/screens/recipe/timer", headers=ha,
                    json={"screen": "ks-echo", "step": 5}).json()
    assert r["result"] == "ambiguous"
    assert r["choices"] == [
        {"step": 5, "label": "Cool", "seconds": 1800, "speech": "Cool, 30 minutes"},
        {"step": 5, "label": "Chill", "seconds": 10800, "speech": "Chill, 3 hours"}]
    # The current step with no timer: the timers not already running, to
    # choose from (the crust and the pie are in the oven).
    client.post("/api/screens/recipe/step", headers=ha, json={"screen": "ks-echo", "step": 3})
    r = client.post("/api/screens/recipe/timer", headers=ha, json={"screen": "ks-echo"}).json()
    assert r["result"] == "ambiguous"
    assert [c["label"] for c in r["choices"]] == ["Cool", "Chill"]
    assert r["speech"] == ("Step 3 has no timer. Which timer: cool for 30 minutes in step 5 "
                           "or chill for 3 hours in step 5?")
    r = client.post("/api/screens/recipe/timer", headers=ha,
                    json={"screen": "ks-echo", "query": "frying"}).json()
    assert r["result"] == "not_found"
    r = client.post("/api/screens/recipe/timer", headers=ha,
                    json={"screen": "ks-echo", "step": 9}).json()
    assert r["result"] == "not_found" and r["reason"] == "no_such_step"


def test_nothing_open_and_ownership(client):
    tablet = _tablet(client)
    other = _tablet(client, "ks-view", "ThinkSmart")
    ha = _key("ha", "HA")
    r = client.get("/api/screens/recipe?screen=ks-echo", headers=ha).json()
    assert r["ok"] is False and r["result"] == "none"
    r = client.post("/api/screens/recipe/timer", headers=ha, json={"screen": "ks-echo"}).json()
    assert r["result"] == "no_recipe"
    assert client.get("/api/screens/recipe?screen=ks-view", headers=tablet).status_code == 403
    assert client.post("/api/screens/recipe/step", headers=other,
                       json={"screen": "ks-echo", "step": "next"}).status_code == 403
    assert client.get("/api/screens/recipe?screen=nope", headers=ha).status_code == 404


def test_cooked_from_cook_mode(client, monkeypatch):
    from app.routers import current_recipe as cr

    async def consume(_recipe):
        return ["Limes"]

    monkeypatch.setattr(cr, "_consume_active_recipe", consume)
    tablet = _tablet(client)
    screen_recipes.set_recipe("ks-echo", PIE)
    r = client.post("/api/screens/recipe/cooked", headers=tablet, json={"screen": "ks-echo"})
    assert r.status_code == 200, r.text
    assert r.json()["consumed"] == ["Limes"] and "1 item used up" in r.json()["speech"]
    assert screen_recipes.get_recipe("ks-echo") is None
    # Home Assistant's key may not use up stock.
    assert client.post("/api/screens/recipe/cooked", headers=_key("ha", "HA"),
                       json={"screen": "ks-echo"}).status_code == 403


def test_cook_page_and_ticket(client):
    tablet = _tablet(client)
    body = client.post("/api/screens/session", headers=tablet,
                       json={"screen": "ks-echo", "page": "cook"}).json()
    assert body["url"].endswith("&next=screen/cook") and body["exit"] == "page"
    from fastapi.testclient import TestClient
    from app.main import app
    view = TestClient(app)
    r = view.get(body["url"], follow_redirects=False)
    assert r.headers["location"] == "/screen/cook"
    page = view.get("/screen/cook")
    assert page.status_code == 200 and "cook-mode.js" in page.text and 'id="sc-cook"' in page.text
    assert screens.page_path("bandit", "cook") == "ui/current-recipe?cook=1"
    assert screens.page_path("kiosk_satellite", "cook") == "screen/cook"
    ha = _key("ha", "HA")
    r = client.post("/events/navigate", headers=ha, json={"page": "cook", "screen": "ks-echo"})
    assert r.status_code == 200


@pytest.mark.parametrize("scope,method,path,ok", [
    ("screen", "GET", "/api/screens/recipe", True),
    ("screen", "POST", "/api/screens/recipe/step", True),
    ("screen", "POST", "/api/screens/recipe/timer", True),
    ("screen", "POST", "/api/screens/recipe/cooked", True),
    ("screen", "GET", "/screen/cook", True),
    ("ha", "GET", "/api/screens/recipe", True),
    ("ha", "POST", "/api/screens/recipe/step", True),
    ("ha", "POST", "/api/screens/recipe/timer", True),
    ("ha", "POST", "/api/screens/recipe/cooked", False),
    ("ha_rest", "POST", "/api/screens/recipe/timer", True),
    ("ha_rest", "POST", "/api/screens/recipe/step", True),
    ("ha_rest", "GET", "/api/screens/recipe", True),
    ("display", "GET", "/api/screens/recipe", False),
    ("viewer", "POST", "/api/screens/recipe/cooked", True),
    ("ha_rest", "POST", "/api/screens/recipe/cooked", False),
    ("reader", "POST", "/api/screens/recipe/cooked", False),
])
def test_scopes(scope, method, path, ok):
    assert access.allows_raw(scope, method, path, kiosk_is_admin=False) is ok


def test_cook_mode_stays_old_browser_safe():
    import re
    js = (SERVICE / "app/static/js/cook-mode.js").read_text()
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"^\s*//.*$", "", js, flags=re.M)
    for modern in ("=>", "`", "?.", "const ", "let ", "fetch(", "async ", "..."):
        assert modern not in js, f"cook-mode.js must stay plain ES5: found {modern!r}"
    css = re.sub(r"/\*.*?\*/", "", (SERVICE / "app/static/css/cook-mode.css").read_text(),
                 flags=re.S)
    for modern in ("var(--", "clamp(", "min(", "gap:", "inset:", "display: grid"):
        assert modern not in css, f"cook-mode.css must suit old browsers: found {modern!r}"


# --- a screen key reaches only its own screens ----------------------------------

def test_a_screen_key_never_acts_for_the_kitchen_or_another_screen(client, monkeypatch):
    from app.routers import current_recipe as cr
    used = []

    async def consume(recipe):
        used.append(recipe.get("title"))
        return ["Limes"]

    monkeypatch.setattr(cr, "_consume_active_recipe", consume)
    tablet = _tablet(client)
    other = _tablet(client, "ks-view", "ThinkSmart")
    current_recipe.set_active(PIE)
    screen_recipes.set_step("", current_recipe.get_active(), 2)
    screen_recipes.set_recipe("ks-view", {"title": "Soup", "id": "soup", "steps": ["Stir."]})
    # Naming no screen is not the kitchen's own recipe for a screen key.
    assert client.get("/api/screens/recipe", headers=tablet).status_code == 400
    for path, body in (("/api/screens/recipe/step", {"step": "next"}),
                       ("/api/screens/recipe/timer", {"step": 2}),
                       ("/api/screens/recipe/cooked", {})):
        r = client.post(path, headers=tablet, json=body)
        assert r.status_code == 400, (path, r.text)
    # Nor another key's screen, and never the main display by name.
    assert client.post("/api/screens/recipe/cooked", headers=tablet,
                       json={"screen": "ks-view"}).status_code == 403
    assert client.post("/api/screens/recipe/cooked", headers=tablet,
                       json={"screen": "server"}).status_code == 403
    assert used == [] and timers.list_timers() == []
    assert current_recipe.get_active()["title"] == PIE["title"]
    assert screen_recipes.current_step("", current_recipe.get_active()) == 2
    assert screen_recipes.get_recipe("ks-view")["title"] == "Soup"
    # Its own screen it may cook from.
    r = client.post("/api/screens/recipe/cooked", headers=other, json={"screen": "ks-view"})
    assert r.status_code == 200 and used == ["Soup"]


def test_a_screen_key_on_a_bandit_is_sent_to_the_main_server(client, monkeypatch):
    tablet = _tablet(client)
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    r = client.post("/api/screens/recipe/step", headers=tablet,
                    json={"screen": "ks-echo", "step": "next"})
    assert r.status_code == 409


def test_cooked_puts_the_step_away_with_the_recipe(client, monkeypatch):
    from app.routers import current_recipe as cr

    async def consume(_recipe):
        return []

    monkeypatch.setattr(cr, "_consume_active_recipe", consume)
    tablet = _tablet(client)
    current_recipe.set_active(PIE)
    kitchen = current_recipe.get_active()
    # The tablet has no recipe of its own, so it cooks the kitchen's.
    screen_recipes.set_step("ks-echo", kitchen, 3)
    screen_recipes.set_step("", kitchen, 1)
    r = client.post("/api/screens/recipe/cooked", headers=tablet, json={"screen": "ks-echo"})
    assert r.status_code == 200, r.text
    assert current_recipe.get_active() is None
    current_recipe.set_active(PIE)
    again = current_recipe.get_active()
    assert screen_recipes.current_step("ks-echo", again) is None
    assert screen_recipes.current_step("", again) is None


def test_cooked_on_the_recipe_page_starts_the_next_cook_at_step_1(client, monkeypatch):
    from app.routers import current_recipe as cr

    async def consume(_recipe):
        return []

    monkeypatch.setattr(cr, "_consume_active_recipe", consume)
    current_recipe.set_active(PIE)
    screen_recipes.set_step("", current_recipe.get_active(), 4)
    from fastapi.testclient import TestClient
    from app.main import app
    admin = TestClient(app)
    assert admin.post("/ui/login", data={"password": "hunter2"},
                      follow_redirects=False).status_code in (302, 303)
    assert admin.post("/current-recipe/cooked").status_code == 200
    current_recipe.set_active(PIE)
    assert screen_recipes.current_step("", current_recipe.get_active()) is None


# --- the state file -------------------------------------------------------------

@pytest.mark.parametrize("blob", ["", "{not json", "[]", "null", "42", '"x"',
                                  '{"screens": [], "steps": "x"}'])
def test_a_damaged_screen_recipes_file_reads_as_nothing(store, blob):
    screen_recipes.reset()
    (store / "screen_recipes.json").write_text(blob)
    assert screen_recipes.get_recipe("ks-a") is None
    assert screen_recipes.current_step("ks-a", PIE) is None
    assert screen_recipes.recipe_for({"id": "ks-a", "kind": "kiosk_satellite"})[1] == "kitchen"
    # The next change writes a good file over it.
    screen_recipes.set_recipe("ks-a", PIE)
    screen_recipes._writer.wait_idle()
    screen_recipes._rows, screen_recipes._steps, screen_recipes._mtime = {}, {}, None
    assert screen_recipes.get_recipe("ks-a")["title"] == PIE["title"]


def test_a_damaged_row_is_dropped_and_the_rest_load(store):
    import json
    import time
    now = time.time()
    screen_recipes.reset()
    (store / "screen_recipes.json").write_text(json.dumps({
        "screens": {
            "ks-a": {"recipe": {"title": "Soup", "servings": "lots", "steps": ["Stir."]},
                     "set": now},
            "ks-b": {"recipe": {"title": "Pie", "id": "pie", "steps": ["Bake.", "Cool."]},
                     "set": now},
            "ks-c": {"recipe": {"title": "Odd", "ingredients": [{"name": "x",
                                                                 "quantity": "a lot"}]},
                     "set": now},
            "ks-d": {"recipe": {"title": "Late"}, "set": "yesterday"},
        },
        "steps": {"ks-b": {"step": 1, "of": "pie", "set": now},
                  "ks-a": {"step": "two", "of": "x", "set": now}},
    }))
    assert screen_recipes.get_recipe("ks-a") is None
    assert screen_recipes.get_recipe("ks-c") is None
    assert screen_recipes.get_recipe("ks-d") is None
    pie = screen_recipes.get_recipe("ks-b")
    assert pie["title"] == "Pie" and screen_recipes.current_step("ks-b", pie) == 1


def test_a_timer_file_from_before_notes_still_loads(store):
    import json
    import time
    now = time.time()
    timers.clear_all()
    timers._writer.wait_idle()
    timers._mtime = None
    (store / "timers.json").write_text(json.dumps({"next": 3, "timers": [
        {"id": 3, "label": "Bread", "total_seconds": 600, "deadline_epoch": now + 500,
         "created_epoch": now - 100, "origin": "", "rang": False}]}))
    got = timers.list_timers()
    assert [t["label"] for t in got] == ["Bread"] and got[0]["note"] == ""


def test_a_bandit_keeps_the_screen_recipe_routes_closed_without_a_password():
    from app import main
    snap = access.AccessSnapshot(is_satellite=True, upstream_linked=True)
    for path in ("/api/screens/recipe", "/api/screens/recipe/step",
                 "/api/screens/recipe/timer", "/api/screens/recipe/cooked"):
        assert main._gate_applies(path, "POST", snap) is True, path
    assert main._gate_applies("/api/screens/poll", "GET", snap) is False
    alone = access.AccessSnapshot(is_satellite=True, upstream_linked=False)
    assert main._gate_applies("/api/screens/recipe/timer", "POST", alone) is False
    server = access.AccessSnapshot(is_satellite=False, upstream_linked=True)
    assert main._gate_applies("/api/screens/recipe/timer", "POST", server) is False


# --- a screen answers only for its own recipe -----------------------------------

def test_a_screen_answers_only_for_its_own_recipe(client):
    """The Starview is cooking lasagna; the Echo Show never opened a recipe.
    Asked what to do first, the Echo Show says nothing is open there instead
    of reading the Starview's. Its pages still show the kitchen's recipe and
    say whose it is, and a tap there makes it the Echo Show's own."""
    echo = _tablet(client)
    _tablet(client, "ks-view", "Starview")
    ha = _key("ha", "HA")
    r = client.post("/api/screens/show-recipe", headers=ha,
                    json={"query": "lasagna", "screen": "ks-view", "page": "cook"}).json()
    assert r["result"] == "shown" and r["recipe"]["title"] == "Lasagna"
    assert current_recipe.get_active()["title"] == "Lasagna"   # the kitchen's, too

    nothing = "No recipe is open on the Echo Show. What are you cooking?"
    r = client.post("/api/screens/recipe/step", headers=ha,
                    json={"screen": "ks-echo", "step": "first"}).json()
    assert r["ok"] is False and r["result"] == "none" and r["speech"] == nothing
    r = client.get("/api/screens/recipe?screen=ks-echo&own=1", headers=ha).json()
    assert r["result"] == "none" and r["speech"] == nothing
    r = client.post("/api/screens/recipe/timer", headers=ha, json={"screen": "ks-echo"}).json()
    assert r["result"] == "no_recipe" and r["speech"] == nothing
    assert timers.list_timers() == []
    # The Starview answers for its own.
    r = client.get("/api/screens/recipe?screen=ks-view&own=1", headers=ha).json()
    assert r["ok"] and r["source"] == "screen" and r["step"] == 1
    # The Echo Show's pages still show the kitchen's recipe, and say so.
    r = client.get("/api/screens/recipe?screen=ks-echo&full=1", headers=echo).json()
    assert r["ok"] and r["source"] == "kitchen" and r["recipe"]["title"] == "Lasagna"
    assert r["step"] is None
    # A request that names no screen is about the kitchen's recipe, as before.
    r = client.get("/api/screens/recipe", headers=ha).json()
    assert r["ok"] and r["source"] == "kitchen" and r["title"] == "Lasagna"
    r = client.post("/api/screens/recipe/step", headers=ha, json={"step": "first"}).json()
    assert r["ok"] and r["step"] == 1
    # A tap on the Echo Show's own page (adopt) makes the recipe it shows its own,
    # so voice there now answers for it.
    r = client.post("/api/screens/recipe/step", headers=echo,
                    json={"screen": "ks-echo", "step": "first", "adopt": True}).json()
    assert r["ok"] and r["step"] == 1 and r["source"] == "screen"
    r = client.post("/api/screens/recipe/step", headers=ha,
                    json={"screen": "ks-echo", "step": "next"}).json()
    assert r["ok"] and r["result"] == "last_step"          # lasagna has one step
    assert screen_recipes.get_recipe("ks-echo")["title"] == "Lasagna"
    # The timer route takes up the shown recipe the same way.
    _tablet(client, "ks-den", "Den tablet")
    den = client.post("/api/screens/recipe/timer", headers=_key("screen", "den"),
                      json={"screen": "ks-den", "adopt": True})
    assert den.status_code == 403                            # not that key's screen
    third = _tablet(client, "ks-hall", "Hall tablet")
    r = client.post("/api/screens/recipe/timer", headers=third,
                    json={"screen": "ks-hall", "adopt": True}).json()
    assert r["result"] == "started" and r["timer"]["label"] == "Bake"
    assert screen_recipes.get_recipe("ks-hall")["title"] == "Lasagna"


# --- choosing a timer -------------------------------------------------------------

def test_timer_choices_leave_out_the_ones_running(client):
    ha = _key("ha", "HA")
    _tablet(client)
    screen_recipes.set_recipe("ks-echo", PIE)
    screen_recipes.set_step("ks-echo", screen_recipes.get_recipe("ks-echo"), 2)  # step 3

    def start(**body):
        return client.post("/api/screens/recipe/timer", headers=ha,
                           json={"screen": "ks-echo", **body}).json()

    assert start(step=2)["result"] == "started"              # Bake crust
    assert start(query="cool")["result"] == "started"        # Cool, step 5
    # Step 3 has no timer: the two not running are offered.
    r = start()
    assert r["result"] == "ambiguous" and [c["label"] for c in r["choices"]] == ["Bake", "Chill"]
    assert r["speech"].startswith("Step 3 has no timer. Which timer: bake for 15 minutes")
    # Step 5's two timers, one running: still a question, with one choice.
    r = start(step=5)
    assert r["result"] == "ambiguous" and [c["label"] for c in r["choices"]] == ["Chill"]
    assert start(step=4)["result"] == "started"
    r = start()
    assert r["result"] == "ambiguous" and [c["label"] for c in r["choices"]] == ["Chill"]
    assert start(step=5, query="chill")["result"] == "started"
    # Every one running: nothing to ask.
    r = start()
    assert r["ok"] and r["result"] == "running" and r["speech"] == "Those timers are already running."
    assert {t["label"] for t in r["timers"]} == {"Bake crust", "Bake", "Cool", "Chill"}
    r = start(step=5)
    assert r["result"] == "running" and r["speech"] == "Those timers are already running."
    assert len(timers.list_timers()) == 4


# --- filler in what was said ------------------------------------------------------

@pytest.mark.parametrize("said", ["just lasagna", "Just lasagna", "only the lasagna please",
                                  "actually, lasagna", "the lasagna one", "lasagna, thanks",
                                  "um, just the one lasagna"])
def test_filler_around_a_name_is_not_part_of_it(said):
    assert recipe_find.choose(said, LIBRARY) == {
        "result": "shown", "recipe": {"slug": "lasagna", "title": "Lasagna"}}


def test_just_lasagna_from_a_model_opens_it(client):
    ha = _key("ha", "HA")
    r = client.post("/api/screens/show-recipe", headers=ha,
                    json={"query": "Just lasagna", "page": "cook"}).json()
    assert r["result"] == "shown" and r["recipe"]["title"] == "Lasagna"


def test_removing_a_screen_in_settings_puts_its_recipe_away(client):
    _tablet(client)
    screen_recipes.set_recipe("ks-echo", PIE)
    from fastapi.testclient import TestClient
    from app.main import app
    admin = TestClient(app)
    assert admin.post("/ui/login", data={"password": "hunter2"},
                      follow_redirects=False).status_code in (302, 303)
    assert admin.delete("/setup/screens/ks-echo").json()["ok"]
    assert screen_recipes.get_recipe("ks-echo") is None


def test_the_landscape_sizes_are_the_echo_shows():
    """Measured on the kitchen tablets: an Echo Show 8 lays out 963 x 594 CSS
    px (at 1.33), a ThinkSmart View 492 x 788 (at 1.625). The landscape
    layout starts at 900 px so the Echo gets it, with its own numbers."""
    import re
    css = (SERVICE / "app/static/css/cook-mode.css").read_text()
    wide = css[css.index("@media (min-width: 900px)"):]
    for rule in (".cm-text { font-size: 33px; }", ".cm-text.cm-long { font-size: 27px; }",
                 "min-height: 72px", ".cm-back { width: 165px; height: 83px;",
                 ".cm-running .cm-run-left { font-size: 42px; }"):
        assert rule in wide, rule
    js = (SERVICE / "app/static/js/cook-mode.js").read_text()
    assert "linesOf(text) > 6" in js and "length > 200" not in js
    screen = (SERVICE / "app/static/css/screen.css").read_text()
    assert "@media (min-width: 900px) and (orientation: landscape)" in screen
    assert "min-width: 1000px" not in screen
    bar = (SERVICE / "app/static/js/timer-bar.js").read_text()
    assert re.search(r"HEIGHT = TABLET \? 38 : 28", bar)


# --- the recipe being cooked, asked for again -----------------------------------

def test_the_recipe_in_cook_mode_stays_there_when_asked_for_again(client):
    """"Just lasagna" while the lasagna is in cook mode on that tablet: with
    no page asked for, cook mode stays at its step. The whole recipe opens
    only when asked for, or for any other recipe."""
    view = _tablet(client, "ks-view", "Starview")
    ha = _key("ha", "HA")

    def show(**body):
        return client.post("/api/screens/show-recipe", headers=ha,
                           json={"screen": "ks-view", **body}).json()

    def on_page(page):
        assert client.get(f"/api/screens/poll?screen=ks-view&since=999999999&page={page}",
                          headers=view).status_code == 200

    assert show(query="best-ever key lime pie", page="cook")["step"] == 1
    client.post("/api/screens/recipe/step", headers=ha, json={"screen": "ks-view", "step": 3})
    on_page("cook")
    since = screens.poll("ks-view", 999999999)["last_id"]

    r = show(query="just the best-ever key lime pie")
    assert r["ok"] and r["page"] == "cook" and r["step"] == 3
    assert r["speech"] == ("Best-Ever Key Lime Pie is already open on the Starview. "
                           "You are on step 3.")
    assert screens.poll("ks-view", since)["events"] == []          # the screen stays put
    mine = screen_recipes.get_recipe("ks-view")
    assert screen_recipes.current_step("ks-view", mine) == 2

    # Asked for the whole recipe: the overview, keeping the place.
    r = show(query="best-ever key lime pie", page="recipe")
    assert r["page"] == "recipe"
    assert [e["path"] for e in screens.poll("ks-view", since)["events"]] == ["screen/recipe"]
    assert screen_recipes.current_step("ks-view", screen_recipes.get_recipe("ks-view")) == 2
    # On the overview, a page not asked for stays the overview.
    on_page("recipe")
    assert show(query="best-ever key lime pie")["page"] == "recipe"
    # Another recipe, with cook mode up: the whole recipe, from the top.
    on_page("cook")
    r = show(query="lasagna")
    assert r["page"] == "recipe" and r["recipe"]["title"] == "Lasagna"
    assert screen_recipes.current_step("ks-view", screen_recipes.get_recipe("ks-view")) is None
    # "Let's cook" still starts at step 1.
    on_page("cook")
    r = show(query="best-ever key lime pie", page="cook")
    assert r["page"] == "cook" and r["step"] == 1
    # A page the screen reported long ago does not count.
    screens._pages["ks-view"] = (screens._pages["ks-view"][0] - 3600, "cook")
    assert show(query="best-ever key lime pie")["page"] == "recipe"


# --- timer lengths written for people -------------------------------------------

LENGTHS = {30: "30 sec", 45: "45 sec", 60: "1 min", 90: "1 min 30 sec", 125: "2 min 5 sec",
           599: "9 min 59 sec", 600: "10 min", 630: "11 min", 3600: "1 hr",
           5400: "1 hr 30 min", 7200: "2 hr"}


def _js_function(src: str, name: str) -> str:
    """The source of ``function name(...) {...}`` in a script."""
    i = src.index("function " + name + "(")
    depth = 0
    for j in range(src.index("{", i), len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError(name)


@pytest.mark.parametrize("path,name", [("app/static/js/cook-mode.js", "spoken"),
                                       ("app/static/js/screen/screen.js", "spoken"),
                                       ("app/static/js/pages/current-recipe.js", "fmtLength")])
def test_timer_buttons_say_seconds_under_a_minute(path, name):
    """Lasagna's "Boil for 30 seconds" is a 30 second timer, and its button
    says so: "Start 30 sec · Boil", never "1 min"."""
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    src = _js_function((SERVICE / path).read_text(), name)
    script = src + "\nconsole.log(JSON.stringify(" + json.dumps(list(LENGTHS)) + ".map(" + name + ")));"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == list(LENGTHS.values())


def test_spoken_lengths_keep_their_seconds():
    assert screen_recipes.spoken(30) == "30 seconds"
    assert screen_recipes.spoken(90) == "1 minute and 30 seconds"
    assert screen_recipes.spoken(600) == "10 minutes"
    assert screen_recipes.spoken(5400) == "1 hour and 30 minutes"


def test_a_page_opened_while_a_timer_rings_takes_over_at_once():
    """A kiosk app closes the page around every voice turn and opens it
    again: a timer the server already calls done rings straight away,
    whatever the tablet's clock says."""
    screen = (SERVICE / "app/static/js/screen/screen.js").read_text()
    assert "function isRinging(t) { return t.expired === true || t.state === 'ringing'" in screen
    cook = (SERVICE / "app/static/js/cook-mode.js").read_text()
    assert "function rings(t) { return t.expired === true || t.state === 'ringing'" in cook
    assert "deadline_epoch - serverNow() <= 0" not in cook.replace(
        "function rings(t) { return t.expired === true || t.state === 'ringing' || "
        "t.deadline_epoch - serverNow() <= 0; }", "")
    app = (SERVICE / "app/static/js/screen-app.js").read_text()
    assert "timers[i].expired" in app and "'timers/events?wait=' + wait" in app
