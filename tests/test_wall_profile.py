"""The wall tablet profile: a per-screen switch that gives a tablet showing
the whole interface bigger type, a short menu and fewer buttons. It moves and
trims things on the page and takes nothing away the session may do."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402
from app.services import access, credentials, ha_events, screens, start_page  # noqa: E402

STATIC = SERVICE / "app" / "static"
TEMPLATES = SERVICE / "app" / "templates"


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
    monkeypatch.setattr(screens, "_bandit_screens", lambda: [
        {"id": "bandit7", "label": "Counter", "kind": "bandit", "online": True,
         "last_seen": None, "targetable": True},
    ])
    yield tmp_path
    screens.reset()
    ha_events.reset()
    credentials.reset(persist=False)


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True, raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"), raising=False)
    monkeypatch.setattr(settings, "start_page_enabled", True, raising=False)
    monkeypatch.setattr(settings, "start_page_mode", "glance", raising=False)
    monkeypatch.setattr(settings, "kiosk_split_enabled", False, raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _key(scope: str, name: str = "k") -> dict:
    _rec, secret = credentials.mint(scope, name, "manual")
    return {"X-API-Key": secret}


def _tablet(client, sid="ks-show8", name="Echo Show", kind="kiosk_satellite"):
    headers = _key("screen", name)
    r = client.post("/api/screens/register", headers=headers, json={
        "screen_id": sid, "label": name, "kind": kind, "width": 1280, "height": 800})
    assert r.status_code == 200, r.text
    return headers


def _app_view(client, headers, sid="ks-show8"):
    """A web view signed in as that screen, showing the whole interface."""
    from fastapi.testclient import TestClient
    from app.main import app
    view = TestClient(app)
    url = client.post("/api/screens/session", headers=headers,
                      json={"screen": sid, "page": "app"}).json()["url"]
    assert view.get(url, follow_redirects=False).status_code == 303
    return view


# --- The switch, per screen ----------------------------------------------------

def test_a_kiosk_app_gets_the_profile_and_other_screens_do_not(store):
    screens.register("c_1", "ks-show8", "Echo Show", "kiosk_satellite")
    screens.register("c_1", "fully", "Hall tablet", "fully_kiosk")
    screens.register("c_1", "phone", "Old phone")
    assert screens.wall_profile("ks-show8") is True
    assert screens.wall_profile("fully") is False
    assert screens.wall_profile("phone") is False
    # Only a registered screen has a profile at all.
    assert screens.wall_profile("server") is False
    assert screens.wall_profile("bandit7") is False
    assert screens.wall_profile("nope") is False
    assert screens.wall_default("kiosk_satellite") and not screens.wall_default("browser")


def test_a_choice_wins_over_the_default_and_survives_a_restart(store):
    screens.register("c_1", "ks-show8", "Echo Show", "kiosk_satellite")
    screens.register("c_1", "phone", "Old phone")
    assert screens.set_wall_profile("ks-show8", False)
    assert screens.set_wall_profile("phone", True)
    assert not screens.set_wall_profile("nope", True)
    # A kiosk app signs up again every time it starts: that keeps the choice.
    screens.register("c_1", "ks-show8", "Echo Show", "kiosk_satellite", 1280, 800)
    screens._writer.wait_idle()
    screens._screens = {}
    screens._mtime = None
    assert screens.wall_profile("ks-show8") is False
    assert screens.wall_profile("phone") is True
    # None goes back to what the kind gets.
    assert screens.set_wall_profile("ks-show8", None)
    assert screens.wall_profile("ks-show8") is True
    # Forgotten and signed up again, it starts from the default.
    screens.forget("phone")
    screens.register("c_1", "phone", "Old phone")
    assert screens.wall_profile("phone") is False


def test_the_list_others_read_keeps_its_shape(store):
    screens.register("c_1", "ks-show8", "Echo Show", "kiosk_satellite")
    screens.set_wall_profile("ks-show8", False)
    row = next(s for s in screens.list_screens() if s["id"] == "ks-show8")
    assert "wall" not in row and row["kind"] == "kiosk_satellite"


# --- Glance in the profile -------------------------------------------------------

def _layout():
    pages = [{"key": k, "label": label, "icon": "bi-grid", "href": href} for k, label, href in (
        ("inventory", "Inventory", "ui/inventory"), ("add", "Manage", "ui/add"),
        ("pending", "Review", "ui/pending"), ("cook", "Cook", "ui/cook"),
        ("shopping", "Shopping", "ui/shopping"), ("guide", "Kitchen Guide", "ui/kitchen-guide"),
        ("timetemp", "Time & Temp", "ui/timers"), ("homehub", "Home Hub", "ui/weather"),
        ("custom_blog", "Our blog", "https://example.com/blog"),
    )]
    return start_page.glance_layout(pages)


def test_the_four_tiles_lead_and_the_rest_wait_behind_more():
    lead, rest = start_page.wall_tiles(_layout())
    assert [k["key"] for k in lead] == ["cook", "timetemp", "shopping", "inventory"]
    assert [k["key"] for k in rest] == ["add", "pending", "guide", "homehub"]
    # A page outside the app would leave the tablet with no way back.
    assert all(k["key"] != "custom_blog" for k in lead + rest)


def test_a_hidden_page_just_drops_out_of_the_lead():
    layout = [k for k in _layout() if k.get("key") != "shopping"]
    lead, rest = start_page.wall_tiles(layout)
    assert [k["key"] for k in lead] == ["cook", "timetemp", "inventory"]
    assert start_page.wall_tiles(None) == ([], [])


# --- Settings ----------------------------------------------------------------------

def test_settings_lists_renames_switches_and_removes_screens(client):
    _tablet(client, "ks-show8", "Echo Show")
    _tablet(client, "phone", "Old phone", kind="browser")
    admin = _key("admin", "owner")
    rows = client.get("/setup/screens", headers=admin).json()["screens"]
    by_id = {r["id"]: r for r in rows}
    # Only the screens kept here: not the main display, not a Bandit.
    assert set(by_id) == {"ks-show8", "phone"}
    echo = by_id["ks-show8"]
    assert echo["kind_label"] == "Kiosk Satellite" and echo["label"] == "Echo Show"
    assert echo["wall"] is True and echo["wall_default"] is True and echo["online"] is True
    assert echo["last_seen"] and echo["width"] == 1280
    assert by_id["phone"]["wall"] is False and by_id["phone"]["kind_label"] == "Browser"
    r = client.post("/setup/screens/ks-show8", headers=admin,
                    json={"label": "  Kitchen   wall ", "wall": False})
    assert r.status_code == 200 and r.json()["screen"]["label"] == "Kitchen wall"
    assert r.json()["screen"]["wall"] is False and screens.wall_profile("ks-show8") is False
    # Only what is sent changes.
    r = client.post("/setup/screens/ks-show8", headers=admin, json={"label": "Kitchen"})
    assert r.json()["screen"]["wall"] is False
    r = client.post("/setup/screens/ks-show8", headers=admin, json={"wall": None})
    assert r.json()["screen"]["wall"] is True
    assert client.post("/setup/screens/nope", headers=admin, json={"wall": True}).status_code == 404
    assert client.post("/setup/screens/server", headers=admin, json={"wall": True}).status_code == 404
    assert client.delete("/setup/screens/phone", headers=admin).json()["ok"]
    assert client.delete("/setup/screens/phone", headers=admin).status_code == 404
    assert [r["id"] for r in client.get("/setup/screens", headers=admin).json()["screens"]] == [
        "ks-show8"]


def test_the_settings_routes_are_for_the_owner_only(client):
    tablet = _tablet(client)
    view = _app_view(client, tablet)
    # The screen's own key, its web view, and anyone signed out are refused.
    for who in (dict(headers=tablet), dict(headers={})):
        assert client.get("/setup/screens", **who).status_code in (401, 403)
        assert client.post("/setup/screens/ks-show8", json={"wall": False},
                           **who).status_code in (401, 403)
        assert client.delete("/setup/screens/ks-show8", **who).status_code in (401, 403)
    assert view.get("/setup/screens").status_code == 403
    assert view.post("/setup/screens/ks-show8", json={"wall": False}).status_code == 403
    assert view.delete("/setup/screens/ks-show8").status_code == 403
    assert screens.wall_profile("ks-show8") is True
    for method in ("GET", "POST", "DELETE"):
        path = "/setup/screens" if method == "GET" else "/setup/screens/ks-show8"
        assert access.route_family(method, path) == "admin"
        assert not access.allows_raw("screen_app", method, path, kiosk_is_admin=True)
        assert not access.allows_raw("screen", method, path, kiosk_is_admin=False)


# --- The pages ---------------------------------------------------------------------

def test_the_profile_trims_glance_for_a_kiosk_app(client):
    view = _app_view(client, _tablet(client))
    page = view.get("/ui/start?kiosk=1").text
    assert 'data-profile="wall"' in page and "static/css/wall.css" in page
    assert "static/js/wall.js" in page and 'id="wall-continue"' in page
    assert 'href="ui/current-recipe?cook=1"' in page
    # One line of hero, not the welcome.
    assert "wall-hero" in page and "Welcome to" not in page
    lead = page.split('class="glance-cards wall-lead"', 1)[1].split("</div>", 1)[0]
    assert re.findall(r'data-tile="([a-z_]+)"', lead) == ["cook", "timetemp", "shopping", "inventory"]
    assert 'id="wall-more"' in page and 'id="wall-rest" hidden' in page
    rest = page.split('id="wall-rest"', 1)[1]
    assert 'data-tile="add"' in rest and 'data-tile="pending"' in rest


def test_the_profile_trims_the_menu_and_the_inventory_rows(client):
    view = _app_view(client, _tablet(client))
    page = view.get("/ui/inventory").text
    assert 'data-profile="wall"' in page and "static/css/wall.css" in page
    # No wide bar with inline links at any width, and the logo goes home.
    assert "navbar-expand-lg" not in page
    assert 'class="navbar-brand d-flex align-items-center" href="ui/start"' in page
    assert "hamburger-nav d-lg-none" not in page and "pr-nav-utility" in page
    # The on-screen bar and the timer chips step aside for the timer bar.
    assert re.search(r'id="floatNav"[^>]*data-nav-hidden="1"', page, re.S)
    assert 'data-chips-hidden="1"' in page
    assert 'id="rowMoreModal"' in page and "pr-page-title" in page


def test_the_profile_follows_the_switch_and_the_kind(client):
    tablet = _tablet(client)
    view = _app_view(client, tablet)
    admin = _key("admin", "owner")
    client.post("/setup/screens/ks-show8", headers=admin, json={"wall": False})
    for path in ("/ui/start?kiosk=1", "/ui/inventory"):
        page = view.get(path).text
        assert 'data-profile="wall"' not in page and "wall.css" not in page, path
    assert "Welcome to" in view.get("/ui/start?kiosk=1").text
    assert 'id="rowMoreModal"' not in view.get("/ui/inventory").text
    # A plain browser screen starts without it, and gets it when switched on.
    phone = _tablet(client, "phone", "Old phone", kind="browser")
    other = _app_view(client, phone, "phone")
    assert 'data-profile="wall"' not in other.get("/ui/inventory").text
    client.post("/setup/screens/phone", headers=admin, json={"wall": True})
    assert 'data-profile="wall"' in other.get("/ui/inventory").text


def test_a_screen_page_ticket_and_a_person_never_get_the_profile(client):
    tablet = _tablet(client)
    from fastapi.testclient import TestClient
    from app.main import app
    narrow = TestClient(app)
    url = client.post("/api/screens/session", headers=tablet,
                      json={"screen": "ks-show8", "page": "home"}).json()["url"]
    narrow.get(url, follow_redirects=False)
    assert 'data-profile="wall"' not in narrow.get("/screen/").text
    person = TestClient(app)
    assert person.post("/ui/login", data={"password": "hunter2"},
                       follow_redirects=False).status_code == 303
    for path in ("/ui/start", "/ui/inventory"):
        assert 'data-profile="wall"' not in person.get(path).text, path


def test_a_wall_tablet_always_gets_glance(client, monkeypatch):
    view = _app_view(client, _tablet(client))
    monkeypatch.setattr(settings, "start_page_mode", "custom", raising=False)
    assert 'id="wall-more"' in view.get("/ui/start?kiosk=1").text
    monkeypatch.setattr(settings, "start_page_enabled", False, raising=False)
    page = view.get("/ui/start?kiosk=1").text
    assert "The Start Page is turned off" not in page and 'id="wall-more"' in page
    # The split view belongs to this install's own panel, never a tablet.
    monkeypatch.setattr(settings, "start_page_enabled", True, raising=False)
    monkeypatch.setattr(settings, "kiosk_split_enabled", True, raising=False)
    assert view.get("/ui/start?kiosk=1", follow_redirects=False).status_code == 200


def test_what_the_profile_session_may_do_is_unchanged(client):
    view = _app_view(client, _tablet(client))
    assert view.get("/ui/inventory").status_code == 200
    assert view.get("/current-recipe").status_code == 200
    r = view.get("/setup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/ui/start?kiosk=1")
    assert view.post("/setup/save", json={}).status_code == 403


# --- The scripts and the stylesheet --------------------------------------------------

def _code(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


def test_the_tablet_script_is_plain_es5():
    js = _code(STATIC / "js" / "wall.js")
    for modern in ("=>", "`", "?.", "??", "const ", "let ", "fetch(", "async ", "..."):
        assert modern not in js, f"wall.js must stay plain ES5: found {modern!r}"


def test_the_stylesheet_needs_nothing_newer_than_the_echo_web_view():
    """The Echo Show 8 runs a Chromium 118 web view."""
    css = _code(STATIC / "css" / "wall.css")
    for newer in ("light-dark(", "field-sizing", "anchor-name", "position-anchor",
                  "position-area", "interpolate-size", "text-box", "@starting-style",
                  "@position-try", "calc-size(", "&"):
        assert newer not in css, f"wall.css uses {newer!r}, which Chromium 118 lacks"
    # Every rule is scoped to the profile, so it can never reach another page.
    for selector in re.findall(r"(^|})\s*([^{}@]+)\{", css):
        for part in _top_level(selector[1]):
            part = part.strip()
            if part and not part.startswith('html[data-profile="wall"]'):
                pytest.fail(f"unscoped rule in wall.css: {part}")


def _top_level(selector: str) -> list[str]:
    """A selector list split at its own commas, not the ones inside :is()."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(selector):
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append(selector[start:i])
            start = i + 1
    parts.append(selector[start:])
    return parts


def test_the_keyboard_and_the_touch_sizes_come_with_the_profile():
    osk = (STATIC / "js" / "osk.js").read_text(encoding="utf-8")
    boot = (STATIC / "js" / "head-boot.js").read_text(encoding="utf-8")
    assert "data-profile') === 'wall'" in osk
    assert "data-profile') === 'wall'" in boot


def test_inventory_rows_name_their_actions_on_a_wall_tablet():
    js = (STATIC / "js" / "pages" / "inventory.js").read_text(encoding="utf-8")
    body = js.split("function wallRowActions", 1)[1].split("\n}\n", 1)[0]
    labels = re.findall(r'aria-hidden="true"></i>([^<]+)</button>', body)
    assert labels == ["Used it", "Edit", "More"]
    assert "printLabel" not in body


def test_copy_has_no_em_dashes():
    for path in (STATIC / "css" / "wall.css", STATIC / "js" / "wall.js",
                 STATIC / "js" / "setup" / "screens.js",
                 SERVICE / "app" / "routers" / "setup" / "screens.py",
                 TEMPLATES / "setup" / "_pane_devices.html"):
        assert chr(0x2014) not in path.read_text(encoding="utf-8"), path
