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


# -- on-screen nav chrome visibility (FoodAssistant-vbfp follow-up) ---------

def test_nav_chrome_auto_hides_only_on_streamdeck_large_kiosk():
    from app.config import nav_chrome_hidden
    # Auto: hide only when a deck is connected AND the scale is large/xlarge.
    assert nav_chrome_hidden("auto", True, "large") is True
    assert nav_chrome_hidden("auto", True, "xlarge") is True
    assert nav_chrome_hidden("auto", True, "normal") is False   # small scale keeps nav
    assert nav_chrome_hidden("auto", False, "large") is False   # no deck keeps nav
    assert nav_chrome_hidden("auto", False, "normal") is False


def test_nav_chrome_explicit_overrides_win():
    from app.config import nav_chrome_hidden
    # "hidden" always hides; "shown" always shows, regardless of deck/scale.
    assert nav_chrome_hidden("hidden", False, "normal") is True
    assert nav_chrome_hidden("shown", True, "xlarge") is False


# -- custom tab normalization (FoodAssistant-9gdz) --------------------------

def test_normalize_custom_tabs_drops_invalid_and_assigns_keys():
    raw = [
        {"label": "Home Assistant", "url": "https://ha.local", "icon": "bi-house"},
        {"label": "", "url": "https://x"},          # no label, dropped
        {"label": "No URL"},                          # no url -> a heading (kept)
        "not a dict",                                 # dropped
        {"label": "Docs", "url": "ui/about"},         # default icon
    ]
    out = navigation.normalize_custom_tabs(raw)
    # A label-only entry (no url) is kept as a heading/folder, not dropped.
    assert [t["label"] for t in out] == ["Home Assistant", "No URL", "Docs"]
    assert out[0]["key"].startswith(navigation.CUSTOM_PREFIX)
    assert out[0]["icon"] == "bi-house" and out[0]["custom"] is True
    assert out[0]["heading"] is False
    assert out[1]["heading"] is True and out[1]["href"] == ""
    assert out[2]["icon"] == navigation._DEFAULT_CUSTOM_ICON


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
                "convert", "nutrition", "camera", "weather", "shop"):
        assert key not in top, f"{key} should be nested, not top-level"

    # Children follow the flat NAV_TABS registration order within each parent.
    assert children["inventory"] == ["expiring", "audit"]
    assert children["recipes"] == ["cook", "current_recipe", "mealplan"]
    assert sorted(children["guide"]) == sorted(["convert", "nutrition", "camera", "weather", "shop"])


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


# -- headings / folders (FoodAssistant-81yi) --------------------------------

def test_heading_with_child_renders_as_label_dropdown_without_own_page():
    # A heading (no href) holding a child renders as a dropdown; it must NOT emit
    # a leading "open the parent's page" item, because it has no page.
    tabs = [
        {"key": "custom_tools", "label": "Tools", "icon": "bi-folder",
         "href": "", "parent": "", "custom": True, "heading": True},
        {"key": "convert", "label": "Convert", "icon": "bi-rulers",
         "href": "ui/convert"},
    ]
    tree = navigation.build_nav_tree(tabs, parents={"convert": "custom_tools"})
    assert [n["key"] for n in tree] == ["custom_tools"]
    folder = tree[0]
    assert folder["heading"] is True and folder["href"] == ""
    assert [c["key"] for c in folder["children"]] == ["convert"]


def test_empty_heading_is_dropped_from_tree():
    # A heading with no children is a dead end and is not rendered.
    tabs = [
        {"key": "custom_empty", "label": "Empty", "icon": "bi-folder",
         "href": "", "parent": "", "custom": True, "heading": True},
        {"key": "convert", "label": "Convert", "icon": "bi-rulers",
         "href": "ui/convert"},
    ]
    tree = navigation.build_nav_tree(tabs, parents={})
    assert [n["key"] for n in tree] == ["convert"]


def test_normalize_explicit_heading_flag_kept_even_with_url():
    # An explicit heading flag wins: the entry is a folder and its url is cleared.
    out = navigation.normalize_custom_tabs(
        [{"label": "Group", "url": "ui/about", "heading": True}])
    assert out[0]["heading"] is True and out[0]["href"] == ""


def test_navbar_renders_heading_dropdown_label_only(monkeypatch, tmp_path):
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
        # A custom heading "Tools" holding the built-in Convert tab.
        monkeypatch.setattr(settings, "nav_parents",
                            {"convert": "custom_tools"}, raising=False)
        monkeypatch.setattr(settings, "custom_nav_tabs",
                            [{"id": "custom_tools", "label": "Tools",
                              "icon": "bi-folder", "url": "", "parent": "",
                              "heading": True}], raising=False)
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        html = client.get("/ui/about").text
        # The heading renders as a dropdown toggle...
        assert 'id="navSub_custom_tools"' in html
        # ...whose menu does NOT link to a page for the heading itself (no href
        # pointing at the heading; the toggle stays href="#").
        assert 'href="ui/convert"' in html
        assert 'href="custom_tools"' not in html
    finally:
        os.chdir(cwd)


def test_first_visible_href_default_is_inventory(monkeypatch):
    monkeypatch.setattr(settings, "nav_order", "", raising=False)
    monkeypatch.setattr(settings, "nav_hidden", "", raising=False)
    monkeypatch.setattr(settings, "nav_parents", {}, raising=False)
    monkeypatch.setattr(settings, "custom_nav_tabs", [], raising=False)
    monkeypatch.setattr(settings, "start_page_enabled", False, raising=False)
    assert navigation.first_visible_href() == "ui/"


def test_start_page_defaults_to_top_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "nav_order", "", raising=False)
    monkeypatch.setattr(settings, "nav_hidden", "", raising=False)
    monkeypatch.setattr(settings, "nav_parents", {}, raising=False)
    monkeypatch.setattr(settings, "custom_nav_tabs", [], raising=False)
    monkeypatch.setattr(settings, "start_page_enabled", True, raising=False)
    # Start is first in the visible order and leads the nav, so /ui lands there.
    assert navigation.visible_tabs()[0]["key"] == "start"
    assert navigation.first_visible_href() == "ui/start"


def test_saved_nav_order_overrides_start_first(monkeypatch):
    # Once the user sets their own order, the Start-first default no-ops.
    monkeypatch.setattr(settings, "nav_order", "inventory,start", raising=False)
    monkeypatch.setattr(settings, "nav_hidden", "", raising=False)
    monkeypatch.setattr(settings, "nav_parents", {}, raising=False)
    monkeypatch.setattr(settings, "custom_nav_tabs", [], raising=False)
    monkeypatch.setattr(settings, "start_page_enabled", True, raising=False)
    assert navigation.visible_tabs()[0]["key"] == "inventory"


# -- Timers tab (FoodAssistant-xlb3) -----------------------------------------

def test_timers_tab_registered_with_no_requirement():
    tab = next(t for t in navigation.NAV_TABS if t["key"] == "timers")
    assert tab["href"] == "ui/timers"
    assert tab["icon"] == "bi-stopwatch"
    assert tab["label"] == "Timers"
    # No backing service: the tab shows on every install shape.
    assert "requires" not in tab


def test_timers_tab_visible_by_default():
    keys = [t["key"] for t in navigation.visible_tabs()]
    assert "timers" in keys


def test_timers_tab_appended_to_saved_nav_order(monkeypatch):
    # A nav_order saved before the Timers tab existed must still show it:
    # unknown keys are appended after the saved order in registration order.
    monkeypatch.setattr(settings, "nav_order",
                        "inventory,shopping,weather,about", raising=False)
    keys = [t["key"] for t in navigation.visible_tabs()]
    assert keys[:4] == ["inventory", "shopping", "weather", "about"]
    assert "timers" in keys
    assert keys.index("timers") > keys.index("about")


def test_timers_tab_survives_saved_nesting_and_hidden(monkeypatch):
    # Saved customizations from an older install (nesting map without the new
    # key, some tabs hidden) never swallow the tab.
    monkeypatch.setattr(settings, "nav_order", "inventory,expiring", raising=False)
    monkeypatch.setattr(settings, "nav_hidden", "convert,about", raising=False)
    monkeypatch.setattr(settings, "nav_parents", {"expiring": "inventory"}, raising=False)
    tree = navigation.build_nav_tree()
    top_keys = {n["key"] for n in tree}
    assert "timers" in top_keys  # no saved parent, so it lands top-level


@pytest.mark.parametrize("mode", ["", "server", "pi_hosted", "pi_remote"])
def test_timers_page_renders_active_nav_tab(monkeypatch, mode, tmp_path):
    # The nav shows the Timers tab and highlights it on its own page, on every
    # deployment shape (the tab has no requirement to gate it).
    import os
    from unittest.mock import patch
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "deployment_mode", mode, raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        with patch.object(type(settings), "is_configured", lambda self: True):
            with TestClient(app) as client:
                r = client.get("/ui/timers")
                assert r.status_code == 200
                # The tab renders and carries the active highlight here.
                assert '<a class="nav-link active" href="ui/timers">' in r.text
    finally:
        os.chdir(cwd)
