"""On-screen Start Page (Pantry Raider): the layout resolver, the shared custom
buttons, and the /ui/start render + enable gating."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402
from app.services import start_page as sp  # noqa: E402


def test_grid_shapes_cover_the_three_deck_sizes():
    assert set(sp.GRID_SHAPES) == {6, 15, 32}
    assert sp.GRID_SHAPES[15] == (5, 3)
    # cols*rows always equals the key count.
    for n, (c, r) in sp.GRID_SHAPES.items():
        assert c * r == n


def test_normalize_key_count():
    assert sp.normalize_key_count(6) == 6
    assert sp.normalize_key_count(99) == 15   # invalid -> default
    assert sp.normalize_key_count("x") == 15


def test_fallback_catalog_mirrors_the_deck_actions():
    # The off-Pi fallback catalog uses the same action names/faces as the deck's
    # JS fallback, so the editor and /ui/start show the same keys.
    names = {a["name"] for a in sp.FALLBACK_CATALOG}
    assert {"inventory", "shopping", "cook", "weather", "timer_1", "brightness"} <= names
    inv = next(a for a in sp.FALLBACK_CATALOG if a["name"] == "inventory")
    assert inv["label"] and inv["icon"] and inv["color"] and inv["group"]


def test_custom_buttons_come_from_streamdeck_overrides(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [
        {"id": "k1", "type": "timer", "minutes": 10},
        {"id": "k2", "type": "shopping_add", "item": "Milk", "label": "Milk"},
        {"not": "an id"},  # ignored
    ])
    cbs = sp.custom_buttons()
    by = {c["id"]: c for c in cbs}
    assert set(by) == {"k1", "k2"}
    assert by["k1"]["label"] == "10 min"     # derived label
    assert by["k2"]["label"] == "Milk"
    assert by["k1"]["color"] and by["k1"]["icon"]


def test_resolve_layout_uses_deck_tokens(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_key_overrides",
                        [{"id": "k1", "type": "timer", "minutes": 5}])
    # Tokens are the deck model: action name, custom id, or blank.
    resolved = sp.resolve_layout(["inventory", "k1", "brightness", "bogus"], 6)
    assert len(resolved) == 6
    assert resolved[0]["kind"] == "builtin" and resolved[0]["href"] == "ui/inventory"
    assert resolved[1]["kind"] == "custom" and resolved[1]["id"] == "k1"
    assert resolved[2]["kind"] == "deckonly"   # a deck-only action (no on-screen page)
    assert resolved[3]["kind"] == "blank"      # unknown token
    assert resolved[5]["kind"] == "blank"      # padded


# -- Glance as a Start Page preset (FoodAssistant-7598) ----------------------

def _pages(n=None):
    """A few nav-shaped pages (what navigation.glance_pages() returns)."""
    all_pages = [
        {"key": "inventory", "label": "Inventory", "icon": "bi-grid", "href": "ui/inventory"},
        {"key": "cook", "label": "Cook", "icon": "bi-fire", "href": "ui/cook"},
        # A heading landing on its first child's page: no nav key in the deck
        # catalog, but ui/timers maps back to the timers_view action.
        {"key": "timetemp", "label": "Time & Temp", "icon": "bi-thermometer-half", "href": "ui/timers"},
        # A page with no deck equivalent at all.
        {"key": "journal", "label": "Journal", "icon": "bi-journal", "href": "ui/journal"},
    ]
    return all_pages if n is None else (all_pages * ((n // 4) + 1))[:n]


def test_glance_key_count_picks_the_smallest_grid_that_fits():
    assert sp.glance_key_count(4) == 6
    assert sp.glance_key_count(6) == 6
    assert sp.glance_key_count(8) == 15
    assert sp.glance_key_count(20) == 32
    # More pages than the biggest grid: cap at the biggest (extras are cut).
    assert sp.glance_key_count(40) == 32


def test_glance_layout_maps_pages_to_navigating_builtin_keys():
    out = sp.glance_layout(_pages())
    assert len(out) == 6                      # 4 pages -> the 3x2 grid
    inv, cook, tt, journal, b1, b2 = out
    for k in (inv, cook, tt, journal):
        assert k["kind"] == "builtin" and k["href"]
    assert inv["href"] == "ui/inventory" and inv["label"] == "Inventory"
    assert inv["icon"] == "bi-grid"
    assert b1["kind"] == "blank" and b2["kind"] == "blank"   # padded


def test_glance_layout_reuses_deck_colors_with_a_rotation_fallback():
    out = sp.glance_layout(_pages(), catalog=sp.FALLBACK_CATALOG)
    inv, cook, tt, journal = out[:4]
    cat = {a["name"]: a for a in sp.FALLBACK_CATALOG}
    # A page that is also a deck action borrows that action's key colour.
    assert inv["color"] == cat["inventory"]["color"]
    assert cook["color"] == cat["cook"]["color"]
    # A page with no deck equivalent still gets a colour from the rotation.
    assert journal["color"] in sp._GLANCE_COLOR_ROTATION


def test_glance_layout_never_seeds_a_timer_fire_key():
    # ui/timers reverse-maps to a navigating action (timers_view), never one of
    # the timer_1.. keys, whose on-screen press starts a timer instead of
    # navigating.
    toks = sp.glance_seed_tokens(_pages())
    assert "timers_view" in toks
    assert not any(t.startswith("timer_") and t != "timers_view" for t in toks)


def test_glance_seed_tokens_blank_when_no_deck_equivalent():
    toks = sp.glance_seed_tokens(_pages())
    assert len(toks) == 6
    assert toks[0] == "inventory" and toks[1] == "cook"
    assert toks[3] == ""                       # journal has no deck action
    assert toks[4] == "" and toks[5] == ""     # padded


def test_glance_layout_respects_an_explicit_key_count():
    out = sp.glance_layout(_pages(20), keys=6)
    assert len(out) == 6
    assert all(k["kind"] == "builtin" for k in out)   # truncated, no padding


# -- route ------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app
    cwd = os.getcwd(); os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "grocy_base_url", "http://g")
    monkeypatch.setattr(settings, "grocy_api_key", "k")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_start_page_disabled_shows_notice(client, monkeypatch):
    monkeypatch.setattr(settings, "start_page_enabled", False)
    r = client.get("/ui/start")
    assert r.status_code == 200
    assert "Start Page is turned off" in r.text


def test_start_save_merges_custom_keys_into_shared_store(client, monkeypatch):
    # A custom key built on the Start Page is merged into the shared deck store.
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [])
    client.post("/setup/save", json={
        "start_page_enabled": True, "start_page_keys": 6,
        "start_page_layout": ["custom:c1"],
        "start_custom_defs": [{"id": "c1", "type": "timer", "label": "Tea", "minutes": 3}],
        "start_loaded_ids": [],
    })
    ov = settings.streamdeck_key_overrides
    assert len(ov) == 1 and ov[0]["id"] == "c1" and ov[0]["slot"] == -1


def test_start_save_preserves_deck_keys_and_slots(client, monkeypatch):
    # A key placed on the deck (slot 4) that the Start Page editor did not load
    # is preserved; a key the editor loaded but dropped is removed.
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [
        {"id": "c1", "type": "timer", "minutes": 3, "slot": -1},
        {"id": "c2", "type": "weather", "slot": 4},
    ])
    client.post("/setup/save", json={
        "start_page_enabled": True, "start_page_keys": 6, "start_page_layout": [],
        "start_custom_defs": [],          # user removed c1 on the Start Page
        "start_loaded_ids": ["c1"],       # ...and only c1 was loaded here
    })
    ov = {o["id"]: o for o in settings.streamdeck_key_overrides}
    assert "c1" not in ov                      # removed on the Start Page
    assert ov["c2"]["slot"] == 4               # deck key + slot preserved


def test_start_page_enabled_renders_grid(client, monkeypatch):
    monkeypatch.setattr(settings, "start_page_enabled", True)
    # Custom mode renders the arranged launcher grid; Glance (the default) builds
    # itself from the nav instead (FoodAssistant-gg33).
    monkeypatch.setattr(settings, "start_page_mode", "custom")
    monkeypatch.setattr(settings, "start_page_keys", 6)
    monkeypatch.setattr(settings, "start_page_layout", ["inventory", "add"])
    r = client.get("/ui/start")
    assert r.status_code == 200
    # 3-column grid for 6 keys, and the two assigned keys render.
    assert "repeat(3, 1fr)" in r.text
    assert "start-key" in r.text
    assert r.text.count("start-key") >= 6  # all six cells render (some blank)


def test_start_page_glance_renders_through_the_grid_path(client, monkeypatch):
    # Glance is a preset of the normal grid (FoodAssistant-7598): the top-level
    # nav pages render as clickable builtin keys plus the notification pills.
    monkeypatch.setattr(settings, "start_page_enabled", True)
    monkeypatch.setattr(settings, "start_page_mode", "glance")
    r = client.get("/ui/start")
    assert r.status_code == 200
    assert "start-grid" in r.text and "glance-grid" not in r.text
    # A top-level page renders as a navigating builtin key.
    assert 'data-href="ui/inventory"' in r.text
    # The pills row stays, with its live count endpoints.
    assert "glance-pills" in r.text
    assert "action-items/count" in r.text and "expiring/count?days=7" in r.text


def test_start_page_custom_with_no_layout_seeds_from_glance(client, monkeypatch):
    # Switching to Custom before arranging anything starts from the Glance
    # preset instead of an all-blank grid (FoodAssistant-7598)...
    monkeypatch.setattr(settings, "start_page_enabled", True)
    monkeypatch.setattr(settings, "start_page_mode", "custom")
    monkeypatch.setattr(settings, "start_page_layout", [])
    r = client.get("/ui/start")
    assert r.status_code == 200
    assert 'data-href="ui/inventory"' in r.text
    # ...but Custom never shows the Glance pills row (the class name always
    # appears in the stylesheet, so check for the rendered markup).
    assert '<div class="glance-pills">' not in r.text


def test_glance_seed_endpoint_shape(client):
    r = client.get("/ui/start/glance-seed")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["keys"] in sp.VALID_KEY_COUNTS
    assert isinstance(body["layout"], list) and len(body["layout"]) == body["keys"]
    assert "inventory" in body["layout"]


def test_inventory_key_avoids_the_ui_root_redirect():
    # /ui/ may redirect to the Start Page when it leads the nav, so the Stock key
    # must target the explicit inventory route, not "ui/", or it would loop back.
    assert sp.ACTION_HREF["inventory"] == "ui/inventory"


# -- live timer key faces (FoodAssistant-uzra) -------------------------------

def test_custom_timer_buttons_carry_the_registry_label():
    from app.services.start_page import custom_buttons
    out = custom_buttons([
        {"id": "a", "type": "timer", "label": "Tea", "minutes": 3},
        {"id": "b", "type": "timer", "minutes": 5},
        {"id": "c", "type": "shopping_add", "item": "Milk"},
    ])
    by_id = {c["id"]: c for c in out}
    assert by_id["a"]["timer_label"] == "Tea"
    assert by_id["b"]["timer_label"] == "Timer"   # same rule the fire path uses
    assert "timer_label" not in by_id["c"]


def test_resolve_layout_passes_timer_label_through():
    from app.services.start_page import resolve_layout
    ovs = [{"id": "a", "type": "timer", "label": "Tea", "minutes": 3}]
    keys = resolve_layout(["a"], 6, overrides=ovs)
    assert keys[0]["kind"] == "custom"
    assert keys[0]["timer_label"] == "Tea"


# -- live built-in key faces (FoodAssistant-x2u7) ----------------------------

def test_expiring_soon_count_mirrors_the_deck_formula():
    from app.services.start_page import expiring_soon_count
    summary = {"expired": 2, "today": 1, "within_3_days": 3, "within_7_days": 4}
    # A week-wide soon window counts the 7-day bucket too, matching the deck.
    assert expiring_soon_count(summary, 7) == 10
    # A shorter window drops the 7-day bucket.
    assert expiring_soon_count(summary, 3) == 6
    # Tolerant of missing/malformed input so a count face never crashes.
    assert expiring_soon_count(None, 7) == 0
    assert expiring_soon_count({}, 7) == 0
    assert expiring_soon_count({"expired": "x"}, 7) == 0


def test_resolve_layout_marks_live_builtins():
    from app.services.start_page import resolve_layout, LIVE_BUILTINS
    keys = resolve_layout(
        ["weather", "forecast", "expiring", "camera", "inventory"], 6)
    live = {k.get("key"): k.get("live") for k in keys if k.get("kind") == "builtin"}
    for name in ("weather", "forecast", "expiring", "camera"):
        assert name in LIVE_BUILTINS
        assert live[name] == name
    # A plain navigation key keeps its static icon face (no live hook).
    assert live["inventory"] is None


def test_expiring_count_endpoint_shape(client, monkeypatch):
    from app.services.grocy import GrocyClient

    async def _fake(self, days=7):
        # A mixed 30-day window: expired, today, 3-day, 7-day, and beyond.
        return [
            {"days_remaining": -1}, {"days_remaining": 0},
            {"days_remaining": 2}, {"days_remaining": 5},
            {"days_remaining": 20},
        ]

    monkeypatch.setattr(GrocyClient, "get_expiring", _fake)
    r = client.get("/expiring/count")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # expired+today+3-day+7-day within the default week window; the 20-day item
    # falls outside it.
    assert body["count"] == 4


def test_start_page_marks_dynamic_keys(client, monkeypatch):
    monkeypatch.setattr(settings, "start_page_enabled", True)
    # The live-face keys are a custom-layout feature; Glance has its own pills.
    monkeypatch.setattr(settings, "start_page_mode", "custom")
    monkeypatch.setattr(settings, "start_page_keys", 15)
    monkeypatch.setattr(settings, "start_page_layout",
                        ["weather", "forecast", "expiring", "camera"])
    r = client.get("/ui/start")
    assert r.status_code == 200
    # The dynamic keys carry the data-live hook the poll loop wires up.
    for name in ("weather", "forecast", "expiring", "camera"):
        assert f'data-live="{name}"' in r.text
    # ...and the page ships the live-face poll endpoints.
    assert "ui/weather/data" in r.text
    assert "expiring/count" in r.text
    assert "/snapshot" in r.text
