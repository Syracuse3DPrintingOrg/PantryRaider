"""Tests for the navigation tab registry (service/app/navigation.py).

Covers the Camera tab gating: it appears only when at least one camera is
configured, and an unconfigured Camera tab does NOT raise a service "unlock"
hint (cameras are set in Interface, not a backend service).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app import navigation  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_nav(monkeypatch):
    # A predictable nav: nothing hidden, default order, no cameras, Mealie off.
    monkeypatch.setattr(settings, "nav_order", "", raising=False)
    monkeypatch.setattr(settings, "nav_hidden", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_cameras", [], raising=False)
    yield


def test_camera_tab_hidden_without_cameras():
    keys = [t["key"] for t in navigation.visible_tabs()]
    assert "camera" not in keys


def test_camera_tab_shown_with_cameras(monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_cameras",
                        [{"name": "Door", "snapshot_url": "http://x/s.jpg"}], raising=False)
    keys = [t["key"] for t in navigation.visible_tabs()]
    assert "camera" in keys


def test_unconfigured_camera_raises_no_unlock_hint():
    # Mealie is unconfigured here, so it should be the only unlock group; cameras
    # must never produce a lock badge even though their requirement is unmet.
    services = {g["service"] for g in navigation.auto_hidden_groups()}
    assert "cameras" not in services


def test_camera_tab_appears_in_all_tabs_editor(monkeypatch):
    # The Settings tab editor lists every registered tab regardless of state.
    monkeypatch.setattr(settings, "streamdeck_cameras", [], raising=False)
    keys = [t["key"] for t in navigation.all_tabs()]
    assert "camera" in keys
    cam = next(t for t in navigation.all_tabs() if t["key"] == "camera")
    assert cam["shown"] is False and cam["available"] is False


# -- custom tab normalization (FoodAssistant-9gdz) --------------------------

def test_normalize_custom_tabs_drops_invalid_and_assigns_keys():
    raw = [
        {"label": "Home Assistant", "url": "https://ha.local", "icon": "bi-house"},
        {"label": "", "url": "https://x"},          # no label, dropped
        {"label": "No URL"},                          # no url, dropped
        "not a dict",                                 # dropped
        {"label": "Docs", "url": "ui/about"},         # default icon
    ]
    out = navigation.normalize_custom_tabs(raw)
    assert [t["label"] for t in out] == ["Home Assistant", "Docs"]
    assert out[0]["key"].startswith(navigation.CUSTOM_PREFIX)
    assert out[0]["icon"] == "bi-house" and out[0]["custom"] is True
    assert out[1]["icon"] == navigation._DEFAULT_CUSTOM_ICON


def test_normalize_custom_tabs_dedupes_keys():
    raw = [
        {"id": "media", "label": "Media", "url": "a"},
        {"id": "media", "label": "Media Two", "url": "b"},
    ]
    out = navigation.normalize_custom_tabs(raw)
    assert out[0]["key"] != out[1]["key"]


def test_custom_tab_shows_in_visible_and_all_tabs(monkeypatch):
    monkeypatch.setattr(settings, "custom_nav_tabs",
                        [{"label": "Wiki", "url": "https://wiki.local", "icon": "bi-book"}],
                        raising=False)
    monkeypatch.setattr(settings, "nav_parents", {}, raising=False)
    vkeys = [t["key"] for t in navigation.visible_tabs()]
    custom = [k for k in vkeys if k.startswith(navigation.CUSTOM_PREFIX)]
    assert custom, "custom tab should be visible"
    editor = {t["key"]: t for t in navigation.all_tabs()}
    assert editor[custom[0]]["custom"] is True
    assert editor[custom[0]]["label"] == "Wiki"


# -- nav tree building ------------------------------------------------------

def _tab(key, parent="", custom=False):
    t = {"key": key, "label": key.title(), "icon": "bi-x", "href": key}
    if custom:
        t["custom"] = True
        t["parent"] = parent
    return t


def test_build_nav_tree_flat_when_no_parents():
    tabs = [_tab("a"), _tab("b"), _tab("c")]
    tree = navigation.build_nav_tree(tabs, parents={})
    assert [n["key"] for n in tree] == ["a", "b", "c"]
    assert all(n["children"] == [] for n in tree)


def test_build_nav_tree_nests_builtin_child_under_parent():
    tabs = [_tab("parent"), _tab("child"), _tab("other")]
    tree = navigation.build_nav_tree(tabs, parents={"child": "parent"})
    keys = [n["key"] for n in tree]
    assert "child" not in keys           # nested, not top-level
    parent = next(n for n in tree if n["key"] == "parent")
    assert [c["key"] for c in parent["children"]] == ["child"]


def test_build_nav_tree_nests_custom_child_via_inline_parent():
    tabs = [_tab("parent"), _tab("custom_x", parent="parent", custom=True)]
    tree = navigation.build_nav_tree(tabs, parents={})
    parent = next(n for n in tree if n["key"] == "parent")
    assert [c["key"] for c in parent["children"]] == ["custom_x"]


def test_build_nav_tree_orphan_parent_falls_back_to_top_level():
    # Parent reference points at a tab that is not present (hidden) -> top level.
    tabs = [_tab("child")]
    tree = navigation.build_nav_tree(tabs, parents={"child": "missing"})
    assert [n["key"] for n in tree] == ["child"]


def test_build_nav_tree_only_one_level_deep():
    # A child of a child should not nest two levels; it stays top-level.
    tabs = [_tab("a"), _tab("b"), _tab("c")]
    tree = navigation.build_nav_tree(tabs, parents={"b": "a", "c": "b"})
    a = next(n for n in tree if n["key"] == "a")
    assert [ch["key"] for ch in a["children"]] == ["b"]
    # c's parent (b) is itself nested, so c falls back to top level.
    assert "c" in [n["key"] for n in tree]


# -- default grouping (FoodAssistant-dprh) ----------------------------------

def test_effective_nav_parents_uses_default_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "nav_parents", {}, raising=False)
    assert navigation.effective_nav_parents() == navigation.DEFAULT_NAV_PARENTS


def test_effective_nav_parents_user_override_wins(monkeypatch):
    monkeypatch.setattr(settings, "nav_parents", {"audit": "shopping"}, raising=False)
    eff = navigation.effective_nav_parents()
    assert eff == {"audit": "shopping"}
    # The default baseline must not leak through once the user has saved a map.
    assert "expiring" not in eff


def test_default_tree_groups_secondary_tabs_under_parents(monkeypatch):
    # Fresh install: nothing hidden, no saved nesting, Mealie + a camera on so the
    # recipe and camera parents/children are all present.
    monkeypatch.setattr(settings, "nav_order", "", raising=False)
    monkeypatch.setattr(settings, "nav_hidden", "", raising=False)
    monkeypatch.setattr(settings, "nav_parents", {}, raising=False)
    monkeypatch.setattr(settings, "custom_nav_tabs", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_cameras",
                        [{"name": "Door", "snapshot_url": "http://x/s.jpg"}], raising=False)
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test", raising=False)
    monkeypatch.setattr(settings, "mealie_api_key", "k", raising=False)

    tree = navigation.build_nav_tree()
    top = {n["key"]: n for n in tree}
    children = {k: [c["key"] for c in n["children"]] for k, n in top.items()}

    # Primary daily-use tabs stay at the top level.
    for key in ("inventory", "add", "pending", "shopping", "recipes"):
        assert key in top, f"{key} should be a top-level tab"

    # Secondary tabs are grouped, not top-level.
    for key in ("expiring", "audit", "cook", "current_recipe", "mealplan",
                "convert", "nutrition", "camera"):
        assert key not in top, f"{key} should be nested, not top-level"

    # Children follow the flat NAV_TABS registration order within each parent.
    assert children["inventory"] == ["expiring", "audit"]
    assert children["recipes"] == ["cook", "current_recipe", "mealplan"]
    assert sorted(children["guide"]) == sorted(["convert", "nutrition", "camera"])


def test_default_grouping_keeps_every_tab_reachable_flat(monkeypatch):
    # The flat list (floating nav / overflow menu) must still expose every tab,
    # including the ones nested by the default grouping.
    monkeypatch.setattr(settings, "nav_order", "", raising=False)
    monkeypatch.setattr(settings, "nav_hidden", "", raising=False)
    monkeypatch.setattr(settings, "nav_parents", {}, raising=False)
    monkeypatch.setattr(settings, "custom_nav_tabs", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_cameras",
                        [{"name": "Door", "snapshot_url": "http://x/s.jpg"}], raising=False)
    monkeypatch.setattr(settings, "mealie_base_url", "http://mealie.test", raising=False)
    monkeypatch.setattr(settings, "mealie_api_key", "k", raising=False)
    flat = {t["key"] for t in navigation.visible_tabs()}
    for key in ("expiring", "audit", "cook", "current_recipe", "mealplan",
                "convert", "nutrition", "camera"):
        assert key in flat


def test_user_override_wins_over_default_tree(monkeypatch):
    # A saved nav_parents map replaces the default grouping wholesale: only the
    # user's nesting applies, and default-nested tabs return to the top level.
    monkeypatch.setattr(settings, "nav_order", "", raising=False)
    monkeypatch.setattr(settings, "nav_hidden", "", raising=False)
    monkeypatch.setattr(settings, "nav_parents", {"shopping": "inventory"}, raising=False)
    monkeypatch.setattr(settings, "custom_nav_tabs", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_cameras", [], raising=False)
    monkeypatch.setattr(settings, "mealie_base_url", "", raising=False)
    monkeypatch.setattr(settings, "mealie_api_key", "", raising=False)

    tree = navigation.build_nav_tree()
    top = {n["key"]: n for n in tree}
    # The user's single nesting applies.
    assert "shopping" not in top
    inv = top["inventory"]
    assert [c["key"] for c in inv["children"]] == ["shopping"]
    # Default-nested tabs are no longer grouped; expiring is back at top level.
    assert "expiring" in top
    assert inv != {} and "expiring" not in [c["key"] for c in inv["children"]]


# -- render smoke test: a parent with children produces a dropdown ----------

def test_navbar_renders_dropdown_for_parent_with_children(monkeypatch, tmp_path):
    import os
    cwd = os.getcwd()
    os.chdir(SERVICE)
    try:
        monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
        monkeypatch.setattr(settings, "auth_required", False, raising=False)
        monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
        monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
        monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
        monkeypatch.setattr(settings, "nav_order", "", raising=False)
        monkeypatch.setattr(settings, "nav_hidden", "", raising=False)
        # Nest the Expiring tab under Inventory so Inventory becomes a dropdown.
        monkeypatch.setattr(settings, "nav_parents", {"expiring": "inventory"}, raising=False)
        monkeypatch.setattr(settings, "custom_nav_tabs", [], raising=False)
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        html = client.get("/ui/about").text
        assert 'id="navSub_inventory"' in html
        assert "dropdown-toggle" in html
    finally:
        os.chdir(cwd)
