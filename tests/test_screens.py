"""Screens: the list, events sent to one screen, a screen's own key and
sign-in, showing a recipe by name, and timers that ring until dismissed."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.passwords import hash_secret
from app.config import settings  # noqa: E402
from app.services import (access, credentials, current_recipe, ha_events,  # noqa: E402
                          recipe_find, screen_pull, screens, timers)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", [], raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    credentials.reset(None, persist=True)
    screens.reset()
    ha_events.reset()
    timers.clear_all()
    timers._events.clear()
    current_recipe.clear_active()
    monkeypatch.setattr(screens, "_bandit_screens", lambda: [
        {"id": "bandit7", "label": "Counter", "kind": "bandit", "online": True,
         "last_seen": None, "targetable": True},
        {"id": "oldbandit", "label": "Pantry", "kind": "bandit", "online": True,
         "last_seen": None, "targetable": False},
    ])
    yield tmp_path
    timers.clear_all()
    screens.reset()
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
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _key(scope: str, name: str = "k") -> tuple[dict, dict]:
    rec, secret = credentials.mint(scope, name, "manual")
    return rec, {"X-API-Key": secret}


def _tablet(client, sid="ks-show8", name="Echo Show"):
    rec, headers = _key("screen", name)
    r = client.post("/api/screens/register", headers=headers, json={
        "screen_id": sid, "label": name, "kind": "kiosk_satellite",
        "width": 1280, "height": 800})
    assert r.status_code == 200, r.text
    return rec, headers


# --- the registry -------------------------------------------------------------

def test_list_has_the_server_bandits_and_registered_screens(store):
    screens.register("c_1", "ks-show8", "Echo Show", "kiosk_satellite", 1280, 800)
    by_id = {s["id"]: s for s in screens.list_screens()}
    assert by_id["server"]["kind"] == "server"
    assert by_id["bandit7"]["targetable"] and not by_id["oldbandit"]["targetable"]
    assert by_id["ks-show8"]["kind"] == "kiosk_satellite" and by_id["ks-show8"]["online"]
    # A key sees only what it registered.
    assert [s["id"] for s in screens.list_screens("c_1")] == ["ks-show8"]
    assert screens.list_screens("c_other") == []


def test_register_checks_the_id_and_the_owner(store):
    with pytest.raises(screens.ScreenError):
        screens.register("c_1", "Bad Id!")
    with pytest.raises(screens.ScreenError):
        screens.register("c_1", "server")
    with pytest.raises(screens.ScreenError):
        screens.register("c_1", "bandit7")
    screens.register("c_1", "tab", "Tablet")
    with pytest.raises(screens.ScreenError):
        screens.register("c_2", "tab", "Mine now")
    # The same key updates its own screen and keeps what it did not resend.
    again = screens.register("c_1", "tab", "Kitchen tablet")
    assert again["label"] == "Kitchen tablet" and again["kind"] == "browser"


def test_registered_screens_survive_a_restart(store):
    screens.register("c_1", "tab", "Tablet", "fully_kiosk")
    screens._writer.wait_idle()
    screens._screens = {}
    screens._mtime = None
    assert screens.owner_of("tab") == "c_1"


# --- events for one screen ----------------------------------------------------

def test_an_event_reaches_only_the_screen_it_names(store):
    screens.register("c_1", "a", "A")
    screens.register("c_1", "b", "B")
    start = screens.poll("a", 999999999)["last_id"]
    sent = screens.send_navigate("a", page="recipe")
    assert sent == {"id": "a", "label": "A", "online": True}
    got = screens.poll("a", start)["events"]
    assert [(e["type"], e["path"], e["page"]) for e in got] == [
        ("navigate", "screen/recipe", "recipe")]
    assert screens.poll("b", start)["events"] == []
    # Nothing leaks into the shared ring every browser reads.
    assert ha_events.poll(0, display=True)["events"] == []


def test_a_page_is_that_screens_own_address(store):
    assert screens.page_path("bandit", "recipe") == "ui/current-recipe"
    assert screens.page_path("server", "timers") == "ui/timers"
    assert screens.page_path("kiosk_satellite", "timers") == "screen/timers"
    assert screens.page_path("browser", "nope") == ""


def test_the_main_display_gets_its_event_and_other_browsers_do_not(store):
    assert screens.send_navigate("server", page="timers")["id"] == "server"
    assert ha_events.poll(0)["events"] == []
    shown = ha_events.poll(0, display=True)["events"]
    assert shown[0]["path"] == "ui/timers" and shown[0]["display"] is True


def test_unknown_and_older_screens_cannot_be_sent_to(store):
    assert screens.send("nope", {"type": "timers"}) is None
    assert screens.send("oldbandit", {"type": "timers"}) is None


def test_a_long_poll_answers_when_an_event_lands(store):
    screens.register("c_1", "a", "A")

    async def go():
        since = screens.poll("a", 999999999)["last_id"]
        task = asyncio.ensure_future(screens.wait_for_events("a", since, timeout=5))
        await asyncio.sleep(0.1)
        screens.send("a", {"type": "notification", "message": "hi"})
        t0 = time.monotonic()
        out = await task
        return out, time.monotonic() - t0

    out, took = asyncio.run(go())
    assert out["events"][0]["message"] == "hi" and took < 1.0


def test_a_cursor_ahead_of_the_server_is_answered_at_once(store):
    screens.register("c_1", "a", "A")
    out = asyncio.run(screens.wait_for_events("a", 500, timeout=5))
    assert out["events"] == [] and out["last_id"] < 500


def test_tickets_work_once(store):
    screens.register("c_1", "a", "A")
    ticket = screens.mint_ticket("a")
    assert screens.redeem_ticket(ticket) == "a"
    assert screens.redeem_ticket(ticket) is None
    assert screens.redeem_ticket("made-up") is None


# --- what a screen key may do -------------------------------------------------

@pytest.mark.parametrize("method,path,ok", [
    ("GET", "/api/screens", True), ("POST", "/api/screens/register", True),
    ("GET", "/api/screens/poll", True), ("POST", "/api/screens/session", True),
    ("DELETE", "/api/screens/tab", True),
    ("GET", "/timers", True), ("GET", "/timers/events", True), ("POST", "/timers", True),
    ("POST", "/timers/3/dismiss", True), ("POST", "/timers/dismiss", True),
    ("POST", "/timers/3/extend", True), ("DELETE", "/timers/3", True),
    ("GET", "/current-recipe", True), ("GET", "/current-recipe/timer-suggestions", True),
    ("POST", "/current-recipe/timers/start", True), ("GET", "/screen/recipe", True),
    ("GET", "/recipes/images/4", True),
    ("POST", "/api/screens/show-recipe", False), ("POST", "/current-recipe", False),
    ("DELETE", "/current-recipe", False), ("GET", "/inventory/stock", False),
    ("GET", "/ui/", False), ("POST", "/setup/save", False), ("GET", "/setup", False),
    ("POST", "/events/navigate", False), ("GET", "/api/credentials", False),
    ("GET", "/ha/state", False), ("POST", "/pending/scan", False),
])
def test_screen_scope(method, path, ok):
    assert access.allows_raw("screen", method, path, kiosk_is_admin=False) is ok


@pytest.mark.parametrize("method,path,ok", [
    ("GET", "/api/screens", True), ("POST", "/api/screens/show-recipe", True),
    ("POST", "/api/screens/session", True), ("POST", "/timers", True),
    ("GET", "/timers/events", True), ("POST", "/timers/dismiss", True),
    ("DELETE", "/timers/2", True), ("DELETE", "/timers", True),
    ("POST", "/events/navigate", True),
    ("POST", "/api/screens/register", False), ("GET", "/current-recipe", False),
    ("GET", "/inventory/stock", False), ("POST", "/setup/save", False),
])
def test_home_assistant_scope(method, path, ok):
    assert access.allows_raw("ha", method, path, kiosk_is_admin=False) is ok


def test_a_screen_key_reaches_only_its_own_screens(client):
    _rec, mine = _tablet(client, "ks-show8", "Echo Show")
    _rec2, theirs = _tablet(client, "ks-view", "ThinkSmart")
    assert [s["id"] for s in client.get("/api/screens", headers=mine).json()["screens"]] == [
        "ks-show8"]
    assert client.get("/api/screens/poll?screen=ks-show8", headers=mine).status_code == 200
    assert client.get("/api/screens/poll?screen=ks-view", headers=mine).status_code == 403
    assert client.get("/api/screens/poll?screen=nope", headers=mine).status_code == 404
    r = client.post("/api/screens/session", headers=mine, json={"screen": "ks-view"})
    assert r.status_code == 403
    assert client.delete("/api/screens/ks-view", headers=mine).status_code == 403
    assert client.get("/inventory/stock", headers=mine).status_code == 403
    assert client.delete("/api/screens/ks-view", headers=theirs).status_code == 200
    # Home Assistant sees them all.
    _r, ha = _key("ha", "HA")
    ids = {s["id"] for s in client.get("/api/screens", headers=ha).json()["screens"]}
    assert {"server", "bandit7", "ks-show8"} <= ids


def test_a_ticket_signs_the_web_view_in_as_that_screen(client, monkeypatch):
    rec, headers = _tablet(client)
    from fastapi.testclient import TestClient
    from app.main import app
    view = TestClient(app)
    # No sign-in: a page asks for one, and the data is refused.
    # A wall tablet is never shown a password form: it is told to reconnect.
    r = view.get("/screen/timers", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/screen/open")
    r = view.get("/screen/open", follow_redirects=False)
    assert r.status_code == 401 and r.headers["x-pr-screen"] == "reconnect"
    assert "needs to reconnect" in r.text and "<form" not in r.text
    r = view.get("/screen/open?ticket=made-up&next=screen/recipe", follow_redirects=False)
    assert r.status_code == 401 and r.headers["x-pr-screen"] == "reconnect"
    r = client.post("/api/screens/session", headers=headers,
                    json={"screen": "ks-show8", "page": "timers"})
    body = r.json()
    assert body["ok"] and body["url"].startswith("/screen/open?ticket=")
    assert r.headers["cache-control"] == "no-store"
    r = view.get(body["url"], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/screen/timers"
    page = view.get("/screen/timers")
    assert page.status_code == 200 and 'data-screen="ks-show8"' in page.text
    assert view.get("/timers").status_code == 200
    assert view.get("/api/screens/poll").json()["screen"]["id"] == "ks-show8"
    assert view.get("/setup", follow_redirects=False).status_code in (303, 403)
    assert view.get("/inventory/stock").status_code == 403
    # A password change signs every person out, but not a screen: its
    # sign-in lasts as long as its key. (A kitchen whose password had ever
    # changed used to turn every screen away at this point.)
    from app.services import auth_state
    monkeypatch.setattr(auth_state, "session_gen", lambda: 7)
    assert view.get("/timers").status_code == 200
    assert view.get("/screen/recipe").status_code == 200
    # The ticket is spent, and turning the key off ends the sign-in.
    other = TestClient(app)
    assert other.get(body["url"], follow_redirects=False).status_code == 401
    assert other.get("/timers").status_code == 401
    credentials.revoke(rec["id"])
    assert view.get("/timers").status_code == 401


def test_screen_pages_use_no_inline_script_and_old_browser_safe_code():
    import re
    js = (SERVICE / "app/static/js/screen/screen.js").read_text()
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"^\s*//.*$", "", js, flags=re.M)
    for modern in ("=>", "`", "?.", "const ", "let ", "fetch(", "async ", "..."):
        assert modern not in js, f"screen.js must stay plain ES5: found {modern!r}"
    css = (SERVICE / "app/static/css/screen.css").read_text()
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for modern in ("var(--", "clamp(", "min(", "gap:", "inset:", "display: grid"):
        assert modern not in css, f"screen.css must suit old browsers: found {modern!r}"


# --- events API ----------------------------------------------------------------

def test_events_can_name_a_screen(client):
    _rec, tablet = _tablet(client)
    _r, ha = _key("ha", "HA")
    since = client.get("/api/screens/poll?screen=ks-show8&since=999999999",
                       headers=tablet).json()["last_id"]
    r = client.post("/events/navigate", headers=ha, json={"page": "timers", "screen": "ks-show8"})
    assert r.status_code == 200 and r.json()["screen"]["label"] == "Echo Show"
    r = client.post("/events/notify", headers=ha,
                    json={"message": "Dinner", "screen": "ks-show8"})
    assert r.json()["ok"]
    events = client.get(f"/api/screens/poll?screen=ks-show8&since={since}",
                        headers=tablet).json()["events"]
    assert [e["type"] for e in events] == ["navigate", "notification"]
    assert events[0]["path"] == "screen/timers"
    assert ha_events.poll(0, display=True)["events"] == []
    assert client.post("/events/navigate", headers=ha,
                       json={"page": "timers", "screen": "nope"}).status_code == 404
    assert client.post("/events/navigate", headers=ha,
                       json={"page": "bogus", "screen": "ks-show8"}).status_code == 400
    assert client.post("/events/navigate", headers=ha,
                       json={"path": "setup/save", "screen": "ks-show8"}).status_code == 400
    r = client.post("/events/close", headers=ha, json={"screen": "ks-show8"})
    assert r.json()["ok"]
    last = screens.poll("ks-show8", since)["events"][-1]
    assert last["type"] == "close" and last["path"] == "screen/"
    client.post("/events/close", headers=ha, json={"screen": "bandit7"})
    assert screens.poll("bandit7", 0)["events"][-1]["path"] == "ui/"
    assert client.post("/events/close", headers=ha, json={}).status_code == 400
    # Without a screen it is the shared ring, as before.
    r = client.post("/events/navigate", headers=ha, json={"path": "ui/cook"})
    assert r.json()["path"] == "ui/cook" and "screen" not in r.json()
    assert ha_events.poll(0)["events"][0]["path"] == "ui/cook"


# --- recipes by name -----------------------------------------------------------

RECIPES = [
    {"slug": "lasagna", "name": "Lasagna"},
    {"slug": "lasagna-soup", "name": "Lasagna Soup"},
    {"slug": "chicken-tikka", "name": "Chicken Tikka Masala"},
    {"slug": "banana-bread", "name": "Grandma's Banana Bread"},
    {"slug": "tacos-a", "name": "Beef Tacos"},
    {"slug": "tacos-b", "name": "Fish Tacos"},
    {"slug": "creme", "name": "Crème Brûlée"},
]


@pytest.mark.parametrize("said,slug", [
    ("lasagna", "lasagna"),
    ("show me the lasagna recipe", "lasagna"),
    ("the lasagna soup", "lasagna-soup"),
    ("chicken tikka", "chicken-tikka"),
    ("banana bread", "banana-bread"),
    ("bananna bred", "banana-bread"),
    ("creme brulee", "creme"),
    ("fish taco", "tacos-b"),
])
def test_one_clear_match(said, slug):
    out = recipe_find.choose(said, RECIPES)
    assert out["result"] == "shown" and out["recipe"]["slug"] == slug


def test_several_fits_are_a_question_and_no_fit_is_not_found():
    out = recipe_find.choose("tacos", RECIPES)
    assert out["result"] == "ambiguous"
    assert {c["slug"] for c in out["candidates"]} == {"tacos-a", "tacos-b"}
    assert recipe_find.choose("pad thai", RECIPES) == {"result": "not_found"}
    assert recipe_find.choose("", RECIPES) == {"result": "not_found"}
    assert recipe_find.join_titles(["A", "B", "C"]) == "A, B and C"


def test_show_recipe_sets_it_and_moves_the_named_screen(client, monkeypatch):
    from app.routers import screens as router

    async def names(_db, _query):
        return RECIPES

    async def detail(_db, slug):
        return {"name": "Lasagna", "slug": slug, "recipeIngredient": [{"note": "noodles"}],
                "recipeInstructions": [{"text": "Bake for 45 minutes."}]}

    monkeypatch.setattr(router, "_recipe_names", names)
    monkeypatch.setattr(router, "_recipe_detail", detail)
    _rec, tablet = _tablet(client)
    _r, ha = _key("ha", "HA")
    since = screens.poll("ks-show8", 999999999)["last_id"]
    r = client.post("/api/screens/show-recipe", headers=ha,
                    json={"query": "the lasagna recipe", "screen": "ks-show8"})
    body = r.json()
    assert body["ok"] and body["result"] == "shown"
    assert body["recipe"] == {"slug": "lasagna", "title": "Lasagna"}
    assert body["speech"] == "Showing Lasagna on the Echo Show."
    assert current_recipe.get_active()["title"] == "Lasagna"
    assert [e["path"] for e in screens.poll("ks-show8", since)["events"]] == ["screen/recipe"]

    # A Bandit keeps its own current recipe, so it is handed this one too.
    since = screens.poll("bandit7", 999999999)["last_id"]
    client.post("/api/screens/show-recipe", headers=ha,
                json={"slug": "lasagna", "screen": "bandit7"})
    got = screens.poll("bandit7", since)["events"]
    assert [e["type"] for e in got] == ["recipe", "navigate"]
    assert got[0]["recipe"]["title"] == "Lasagna" and got[1]["path"] == "ui/current-recipe"

    r = client.post("/api/screens/show-recipe", headers=ha, json={"query": "tacos"})
    assert r.json()["result"] == "ambiguous" and "Which one?" in r.json()["speech"]
    r = client.post("/api/screens/show-recipe", headers=ha, json={"query": "pad thai"})
    assert r.json() == {"ok": False, "result": "not_found",
                        "speech": "I could not find a recipe called pad thai."}
    r = client.post("/api/screens/show-recipe", headers=ha,
                    json={"query": "lasagna", "screen": "oldbandit"})
    assert r.status_code == 409
    assert client.post("/api/screens/show-recipe", headers=tablet,
                       json={"query": "lasagna"}).status_code == 403


def test_a_bandit_applies_what_it_pulls(store):
    assert screen_pull.apply_event({"type": "recipe", "recipe": {"title": "Soup", "steps": ["Stir."]}})
    assert current_recipe.get_active()["title"] == "Soup"
    assert screen_pull.apply_event({"type": "navigate", "path": "ui/current-recipe", "id": 9})
    assert ha_events.poll(0)["events"] == []
    shown = ha_events.poll(0, display=True)["events"]
    assert shown[0]["path"] == "ui/current-recipe" and shown[0]["id"] != 9
    assert not screen_pull.apply_event({"type": "something-new"})


# --- timers that ring ----------------------------------------------------------

def test_a_timer_remembers_who_set_it_and_rings_until_dismissed(store, monkeypatch):
    t = timers.create_timer("Pasta", 600, origin="assist_satellite.kitchen")
    assert t["state"] == "running" and t["origin"] == "assist_satellite.kitchen"
    assert timers.dismiss_timer(t["id"]) == "running"
    real = time.time
    monkeypatch.setattr(timers.time, "time", lambda: real() + 601)
    out = timers.poll_events(0)
    assert [e["type"] for e in out["events"]] == ["started", "finished"]
    assert out["timers"][0]["state"] == "ringing" and out["timers"][0]["expired"]
    # Announced once, however often it is read.
    assert [e["type"] for e in timers.poll_events(0)["events"]] == ["started", "finished"]
    assert timers.dismiss_timer(t["id"]) == "ok"
    assert timers.dismiss_timer(t["id"]) == "missing"
    assert timers.poll_events(out["last_id"])["events"][0]["type"] == "dismissed"
    assert timers.list_timers() == []


def test_cancel_and_dismiss_are_told_apart(store, monkeypatch):
    a = timers.create_timer("A", 60, origin="one")
    b = timers.create_timer("B", 60, origin="two")
    c = timers.create_timer("C", 6000)
    timers.cancel_timer(c["id"])
    real = time.time
    monkeypatch.setattr(timers.time, "time", lambda: real() + 61)
    assert timers.dismiss_ringing("one") == 1
    assert [t["id"] for t in timers.list_timers()] == [b["id"]]
    assert timers.dismiss_ringing() == 1
    kinds = [(e["type"], e["timer"]["label"]) for e in timers.poll_events(0)["events"]]
    assert ("cancelled", "C") in kinds and ("dismissed", "A") in kinds
    assert ("dismissed", "B") in kinds and a["id"] != b["id"]


def test_the_timer_long_poll_answers_at_the_deadline(store):
    async def go():
        since = timers.poll_events(999999999)["last_id"]
        timers.create_timer("Egg", 0.4)
        since = timers.poll_events(since)["last_id"]
        t0 = time.monotonic()
        out = await timers.wait_for_events(since, timeout=5)
        return out, time.monotonic() - t0

    out, took = asyncio.run(go())
    assert [e["type"] for e in out["events"]] == ["finished"] and took < 1.5


def test_timer_routes(client):
    _rec, tablet = _tablet(client)
    _r, ha = _key("ha", "HA")
    since = screens.poll("ks-show8", 999999999)["last_id"]
    r = client.post("/timers", headers=ha, json={
        "label": "Pasta", "seconds": 600, "origin": "assist_satellite.kitchen",
        "screen": "ks-show8"})
    body = r.json()
    assert body["timer"]["origin"] == "assist_satellite.kitchen"
    assert body["screen"]["id"] == "ks-show8"
    kinds = [e["type"] for e in screens.poll("ks-show8", since)["events"]]
    assert "navigate" in kinds and "timers" in kinds
    tid = body["timer"]["id"]
    assert client.post(f"/timers/{tid}/dismiss", headers=tablet).status_code == 409
    assert client.post("/timers/999/dismiss", headers=tablet).status_code == 404
    feed = client.get("/timers/events?since=0", headers=ha).json()
    assert feed["events"][-1]["type"] == "started" and feed["timers"][0]["id"] == tid
    assert client.post("/timers/dismiss", headers=tablet, json={}).json() == {
        "ok": True, "dismissed": 0}
    assert client.delete(f"/timers/{tid}", headers=tablet).status_code == 200
    # An older caller that sends only a label and seconds still works.
    assert client.post("/timers", headers=ha,
                       json={"label": "Tea", "seconds": 60}).json()["timer"]["origin"] == ""
