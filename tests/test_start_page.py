"""On-screen Start Page (FoodAssistant): the layout resolver, the shared custom
buttons, and the /ui/start render + enable gating."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

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


def test_catalog_mirrors_the_deck_actions():
    names = {a["name"] for a in sp.catalog_for_editor()}
    # The editor catalog uses the same action names as the deck.
    assert {"inventory", "shopping", "cook", "weather", "timer_1", "brightness"} <= names
    # Each entry carries the deck face (label/icon/colour) + a group.
    inv = next(a for a in sp.catalog_for_editor() if a["name"] == "inventory")
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
    monkeypatch.setattr(settings, "start_page_keys", 6)
    monkeypatch.setattr(settings, "start_page_layout", ["inventory", "add"])
    r = client.get("/ui/start")
    assert r.status_code == 200
    # 3-column grid for 6 keys, and the two assigned keys render.
    assert "repeat(3, 1fr)" in r.text
    assert "start-key" in r.text
    assert r.text.count("start-key") >= 6  # all six cells render (some blank)


def test_inventory_key_avoids_the_ui_root_redirect():
    # /ui/ may redirect to the Start Page when it leads the nav, so the Stock key
    # must target the explicit inventory route, not "ui/", or it would loop back.
    assert sp.DECK_CATALOG["inventory"]["href"] == "ui/inventory"
