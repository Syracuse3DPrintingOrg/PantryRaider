"""Blocked custom tabs in the nav (service/app/navigation.py, base.html,
start.html, and the page scripts that follow a link).

A saved custom tab whose address could run code (javascript:, data:) is never
rendered: not in the top row, the hamburger, the More menu, the sub-pills, the
on-screen bar, the Glance grid, or as the kiosk home. The Settings editor still
lists it, flagged, with the stored value so it can be fixed. Tabs saved before
this check existed keep working when their address is fine.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app import navigation  # noqa: E402

_EVIL = "javascript:alert(document.cookie)"
_TABS = [
    {"id": "custom_evil", "label": "Evil", "icon": "bi-bug", "url": _EVIL},
    {"id": "custom_ha", "label": "Home Assistant", "icon": "bi-house", "url": "https://ha.example"},
    {"id": "custom_page", "label": "About page", "icon": "bi-info", "url": "ui/about"},
]


@pytest.fixture(autouse=True)
def _nav(monkeypatch):
    monkeypatch.setattr(settings, "nav_order", "", raising=False)
    monkeypatch.setattr(settings, "nav_hidden", "", raising=False)
    monkeypatch.setattr(settings, "nav_parents", {}, raising=False)
    monkeypatch.setattr(settings, "streamdeck_cameras", [], raising=False)
    monkeypatch.setattr(settings, "custom_nav_tabs", list(_TABS), raising=False)
    yield


# -- normalize_custom_tabs ----------------------------------------------------

def test_unsafe_url_gives_a_blocked_tab():
    tabs = {t["key"]: t for t in navigation.normalize_custom_tabs(_TABS)}
    evil = tabs["custom_evil"]
    assert evil["blocked"] is True and evil["heading"] is False
    assert evil["href"] == ""
    assert evil["blocked_url"] == _EVIL
    assert tabs["custom_ha"]["blocked"] is False
    assert tabs["custom_ha"]["href"] == "https://ha.example"


def test_tab_saved_before_the_check_still_works():
    # An older settings.json entry (no heading key, no icon) with a normal
    # address renders exactly as before.
    [tab] = navigation.normalize_custom_tabs([{"id": "custom_x", "label": "X", "url": "ui/x"}])
    assert tab["href"] == "ui/x" and not tab["blocked"] and not tab["heading"]
    assert tab["icon"] == "bi-link-45deg"


def test_heading_is_never_blocked():
    [tab] = navigation.normalize_custom_tabs([{"id": "custom_h", "label": "H", "heading": True}])
    assert tab["heading"] is True and tab["blocked"] is False


@pytest.mark.parametrize("icon,expected", [
    ("bi-house", "bi-house"), ("bi-grid-3x3-gap", "bi-grid-3x3-gap"),
    ('bi-x" onmouseover="alert(1)', "bi-link-45deg"), ("fa-house", "bi-link-45deg"),
    ("BI-HOUSE", "bi-link-45deg"), ("", "bi-link-45deg"),
])
def test_icon_must_be_a_bootstrap_icon_name(icon, expected):
    [tab] = navigation.normalize_custom_tabs([{"label": "T", "url": "ui/x", "icon": icon}])
    assert tab["icon"] == expected


# -- every rendered list skips it ---------------------------------------------

def test_blocked_tab_left_out_of_every_rendered_list():
    assert "custom_evil" not in {t["key"] for t in navigation.visible_tabs()}
    tree = navigation.build_nav_tree()
    keys = {n["key"] for n in tree} | {c["key"] for n in tree for c in n["children"]}
    assert "custom_evil" not in keys
    assert "custom_evil" not in {p["key"] for p in navigation.glance_pages()}
    assert "custom_evil" not in {p["key"] for p in navigation.float_nav_pages()}
    assert "custom_ha" in {p["key"] for p in navigation.float_nav_pages()}


def test_build_nav_tree_drops_blocked_from_a_passed_list():
    tabs = navigation.normalize_custom_tabs(_TABS)
    assert "custom_evil" not in {n["key"] for n in navigation.build_nav_tree(tabs, {})}


def test_child_of_blocked_tab_falls_back_to_top_level(monkeypatch):
    monkeypatch.setattr(settings, "custom_nav_tabs", [
        {"id": "custom_evil", "label": "Evil", "url": _EVIL},
        {"id": "custom_kid", "label": "Kid", "url": "ui/about", "parent": "custom_evil"},
    ], raising=False)
    top = {n["key"] for n in navigation.build_nav_tree()}
    assert "custom_kid" in top and "custom_evil" not in top


def test_blocked_lead_tab_is_never_the_home(monkeypatch):
    monkeypatch.setattr(settings, "nav_order", "custom_evil,inventory", raising=False)
    monkeypatch.setattr(settings, "start_page_enabled", False, raising=False)
    assert navigation.first_visible_href() == "ui/inventory"


def test_all_tabs_keeps_blocked_tab_for_the_editor():
    rows = {r["key"]: r for r in navigation.all_tabs()}
    evil = rows["custom_evil"]
    assert evil["blocked"] is True and evil["shown"] is False
    assert evil["blocked_url"] == _EVIL
    assert rows["inventory"]["blocked"] is False
    assert rows["custom_ha"]["blocked"] is False


# -- the rendered pages -------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "start_page_enabled", True, raising=False)
    monkeypatch.setattr(settings, "start_page_mode", "glance", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_base_page_never_renders_the_blocked_address(client):
    html = client.get("/ui/about").text
    assert "javascript:" not in html.lower()
    assert "Evil" not in html
    # The external tab opens in a new tab; the in-app one does not.
    assert re.search(r'href="https://ha\.example"\s+target="_blank" rel="noopener"', html)
    assert not re.search(r'href="ui/about"\s+target="_blank"', html)


def test_glance_page_never_renders_the_blocked_address(client):
    html = client.get("/ui/start").text
    assert "javascript:alert" not in html
    assert "Evil" not in html
    # A Glance card still opens its page in place, as it always has (a kiosk
    # has no second window to come back from).
    assert re.search(r'class="glance-card" href="https://ha\.example" style=', html)


def test_kiosk_home_target_is_never_blocked(client, monkeypatch):
    monkeypatch.setattr(settings, "nav_order", "custom_evil", raising=False)
    html = client.get("/ui/about").text
    m = re.search(r'data-home="([^"]*)"', html)
    assert m and not m.group(1).lower().startswith("javascript")


def test_camera_context_carries_no_credentials(client, monkeypatch):
    # Every page gets the cameras view model, not the raw saved entries.
    monkeypatch.setattr(settings, "streamdeck_cameras", [
        {"name": "Porch", "snapshot_url": "http://admin:hunter2@192.0.2.9/snap.jpg"},
        {"name": "Shed", "snapshot_url": "http://192.0.2.10/snap.jpg?user=admin&password=hunter2"},
    ], raising=False)
    from app.templating import theme_context

    class _Req:
        class state:
            pass
    ctx = theme_context(_Req())
    blob = repr(ctx["cameras"])
    assert "hunter2" not in blob and "admin" not in blob
    assert ctx["cameras"][0]["snapshot_src"] == "ui/camera/0/snapshot"


# -- page scripts follow only http(s) -----------------------------------------

_JS = SERVICE / "app" / "static" / "js"


def test_base_page_defines_prsafehref():
    src = (_JS / "base-page.js").read_text()
    assert "window.PRSafeHref = function" in src
    assert "document.baseURI" in src
    assert "url.protocol === 'http:' || url.protocol === 'https:'" in src


def test_keyboard_nav_checks_before_navigating():
    src = (_JS / "keyboard-nav.js").read_text()
    assert "window.PRSafeHref(raw)" in src
    assert "window.location.href = target" in src


def test_kiosk_home_falls_back_to_app_home():
    src = (_JS / "kiosk-home.js").read_text()
    assert "if (!safeHref(home)) home = 'ui/';" in src
    assert "window.location.href = home;" not in src


def test_start_page_go_checks_before_navigating():
    src = (SERVICE / "app" / "templates" / "start.html").read_text()
    assert "function go(path) { const u = safeHref(path); if (u) location.href = u; }" in src


def test_templates_use_is_external_not_a_substring_test():
    for name in ("base.html", "start.html"):
        src = (SERVICE / "app" / "templates" / name).read_text()
        assert "'://' in" not in src, name


def test_nav_editor_marks_refused_rows_and_escapes():
    src = (_JS / "setup" / "nav-editor.js").read_text()
    assert "function navApplySaveErrors(reply)" in src
    assert "Address not allowed, edit it" in src
    assert "t.blocked ? t.blocked_url : t.href" in src
    assert "${navEsc(t.label)}" in src
    assert "if (!navUrlAllowed(url))" in src
