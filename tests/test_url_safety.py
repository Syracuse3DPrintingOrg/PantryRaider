"""The one URL policy for saved links (service/app/services/url_safety.py).

A custom nav tab's address is rendered into an href on every page, so a
javascript: (or data:, vbscript:) address must never get through, including
the spellings a browser quietly repairs ("java<TAB>script:", a leading control
character). Relative app pages and ordinary web addresses keep working.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import url_safety  # noqa: E402
from app.services.url_safety import (  # noqa: E402
    clean_custom_tabs, is_external, safe_href, safe_nav_path)


# -- safe_href ----------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "ui/x", "/grocy/", "#", "setup#pane", "//host/p", "ui/about?x=1",
    "http://ha.local:8123/lovelace", "https://example.com", "HTTPS://Example.com/",
])
def test_safe_href_allows_web_and_app_addresses(value):
    assert safe_href(value) == value


@pytest.mark.parametrize("value", [
    "javascript:alert(1)", "JavaScript:alert(1)", "java\tscript:alert(1)",
    "java\nscript:alert(1)", "java\rscript:alert(1)", " javascript:alert(1)",
    "\x01javascript:alert(1)", "\x00\x1fjavascript:alert(1)",
    "data:text/html,<script>alert(1)</script>", "vbscript:msgbox(1)",
    "file:///etc/passwd", "http:foo", "https:", "http://",
    "ui/\x01x",
])
def test_safe_href_refuses_code_and_hostless_schemes(value):
    assert safe_href(value) is None


def test_safe_href_trims_and_drops_tabs_newlines():
    assert safe_href("  https://ha.local/\n") == "https://ha.local/"
    assert safe_href("ui/\tabout") == "ui/about"


def test_safe_href_empty_is_none():
    assert safe_href("") is None
    assert safe_href(None) is None
    assert safe_href(" \x00 ") is None


# -- is_external --------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("https://example.com", True), ("HTTP://example.com", True),
    ("//host/p", True), ("\\\\host/p", True), ("/\\host", True),
    ("ui/about", False), ("/grocy/", False), ("#", False), ("", False),
    ("setup#pane", False), ("javascript:x", False),
])
def test_is_external(value, expected):
    assert is_external(value) is expected


# -- safe_nav_path moved here; events.py re-exports the same function ----------

def test_safe_nav_path_is_reexported_from_events():
    from app.routers import events
    assert events.safe_nav_path is safe_nav_path
    assert safe_nav_path("/ui/cook") == "ui/cook"
    assert safe_nav_path("javascript:x") == ""
    assert safe_nav_path("//evil") == ""
    assert safe_nav_path("https://x/y") == ""


@pytest.mark.parametrize("value", [
    # A leading slash used to be stripped after the checks, leaving a space or
    # control in front of a scheme that the browser trims and then runs.
    "/ javascript:alert(1)", "/\tjavascript:alert(1)", "/\x01javascript:alert(1)",
    "/javascript:alert(1)", "/data:text/html,x",
    # A backslash is a slash to the browser, so these would leave the app.
    "/\\/evil.example", "\\\\evil.example", "ui\\..\\x",
    # A tab or newline inside is dropped by the browser, joining "//".
    "/\t//evil.example", "/\n//evil.example",
    "/", "  ",
])
def test_safe_nav_path_refuses_what_the_browser_would_run_or_leave_for(value):
    assert safe_nav_path(value) == ""


@pytest.mark.parametrize("value,expected", [
    ("ui/cook", "ui/cook"), ("/ui/cook", "ui/cook"), ("//ui", ""),
    ("ui/timers?view=timers", "ui/timers?view=timers"), (" ui/cook\n", "ui/cook"),
    ("setup#pane-network", "setup#pane-network"),
])
def test_safe_nav_path_keeps_app_pages(value, expected):
    # What Home Assistant and the Stream Deck already send keeps working.
    assert safe_nav_path(value) == expected


# -- clean_custom_tabs --------------------------------------------------------

def test_clean_custom_tabs_accepts_good_rows_in_stored_shape():
    clean, rejected = clean_custom_tabs([
        {"id": "custom_ha", "label": "HA", "icon": "bi-house", "url": "https://ha.local"},
        {"id": "custom_tools", "label": "Tools", "icon": "bi-folder", "url": "", "heading": True},
        {"id": "custom_about", "label": "About me", "url": "ui/about", "parent": "custom_tools"},
    ])
    assert rejected == []
    assert clean == [
        {"id": "custom_ha", "label": "HA", "icon": "bi-house", "url": "https://ha.local",
         "parent": "", "heading": False},
        {"id": "custom_tools", "label": "Tools", "icon": "bi-folder", "url": "",
         "parent": "", "heading": True},
        {"id": "custom_about", "label": "About me", "icon": "bi-link-45deg",
         "url": "ui/about", "parent": "custom_tools", "heading": False},
    ]


def test_clean_custom_tabs_rejects_unsafe_address_with_message():
    clean, rejected = clean_custom_tabs([
        {"id": "custom_ok", "label": "OK", "url": "https://ok.example"},
        {"id": "custom_bad", "label": "Bad tab", "url": "javascript:alert(document.cookie)"},
    ])
    assert [t["id"] for t in clean] == ["custom_ok"]
    assert rejected == [{
        "id": "custom_bad", "label": "Bad tab",
        "message": ("Bad tab can't be saved: a tab's address has to start with "
                    "http:// or https://, or be a page in this app."),
    }]


def test_clean_custom_tabs_pairs_rejections_after_dropped_rows():
    # A label-less row and a non-dict are dropped silently, as before, and do
    # not throw off which posted id a rejection names.
    clean, rejected = clean_custom_tabs([
        "junk", {"id": "custom_x", "label": "", "url": "https://x"},
        {"id": "custom_evil", "label": "Evil", "url": " data:text/html,x"},
    ])
    assert clean == []
    assert [r["id"] for r in rejected] == ["custom_evil"]


def test_clean_custom_tabs_not_a_list():
    assert clean_custom_tabs(None) == ([], [])
    assert clean_custom_tabs({"a": 1}) == ([], [])


# -- report_blocked_tabs ------------------------------------------------------

class _FakeSettings(SimpleNamespace):
    def save(self, data):
        self.saved.append(dict(data))
        for k, v in data.items():
            setattr(self, k, v)


def _fake_settings(tabs, notified=None, save_error=None):
    fs = _FakeSettings(custom_nav_tabs=tabs, nav_blocked_notified=notified or [],
                       saved=[])
    if save_error is not None:
        def _boom(data):
            raise save_error
        fs.save = _boom
    return fs


@pytest.fixture
def created(monkeypatch):
    calls = []

    def fake_create(db, kind, title, **kw):
        calls.append({"kind": kind, "title": title, **kw})
        return {"id": len(calls)}

    from app.services import action_items
    monkeypatch.setattr(action_items, "create", fake_create)
    return calls


_BAD = {"id": "custom_bad", "label": "Bad", "url": "javascript:alert(1)"}
_GOOD = {"id": "custom_good", "label": "Good", "url": "https://ok.example"}


def test_report_blocked_tabs_raises_one_item_per_tab_once(monkeypatch, created):
    import app.config as config_mod
    fs = _fake_settings([_BAD, _GOOD])
    monkeypatch.setattr(config_mod, "settings", fs)
    assert url_safety.report_blocked_tabs(db=None) == 1
    assert len(created) == 1
    item = created[0]
    assert item["kind"] == "nav_tab_blocked"
    assert item["title"] == "A custom tab was hidden"
    assert '"Bad"' in item["body"] and "could run code" in item["body"]
    assert "Settings, Navigation" in item["body"]
    assert item["dedupe_key"] == "nav_tab_blocked:custom_bad"
    assert fs.nav_blocked_notified == ["custom_bad"]
    # A restart reports nothing new.
    assert url_safety.report_blocked_tabs(db=None) == 0
    assert len(created) == 1


def test_report_blocked_tabs_forgets_a_fixed_tab(monkeypatch, created):
    import app.config as config_mod
    fs = _fake_settings([_GOOD], notified=["custom_bad"])
    monkeypatch.setattr(config_mod, "settings", fs)
    assert url_safety.report_blocked_tabs(db=None) == 0
    assert fs.nav_blocked_notified == []


@pytest.mark.parametrize("tabs", [None, [], "not a list"])
def test_report_blocked_tabs_empty_or_missing_config(monkeypatch, created, tabs):
    import app.config as config_mod
    fs = _fake_settings(tabs)
    monkeypatch.setattr(config_mod, "settings", fs)
    assert url_safety.report_blocked_tabs(db=None) == 0
    assert created == [] and fs.saved == []


def test_report_blocked_tabs_survives_read_only_data_dir(monkeypatch, created):
    import app.config as config_mod
    fs = _fake_settings([_BAD], save_error=OSError("read-only file system"))
    monkeypatch.setattr(config_mod, "settings", fs)
    assert url_safety.report_blocked_tabs(db=None) == 1
    # Remembered in memory for this run, so a second call does not repeat it.
    assert fs.nav_blocked_notified == ["custom_bad"]


def test_report_blocked_tabs_never_raises(monkeypatch):
    import app.config as config_mod
    from app.services import action_items

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(action_items, "create", boom)
    monkeypatch.setattr(config_mod, "settings", _fake_settings([_BAD]))
    assert url_safety.report_blocked_tabs(db=None) == 0


def test_report_blocked_tabs_writes_a_real_inbox_item(monkeypatch):
    import app.config as config_mod
    from app.database import SessionLocal
    from app.models.db_models import ActionItem
    from app.services import action_items

    monkeypatch.setattr(config_mod, "settings", _fake_settings([_BAD]))
    db = SessionLocal()
    try:
        db.query(ActionItem).filter(ActionItem.kind == "nav_tab_blocked").delete()
        db.commit()
        assert url_safety.report_blocked_tabs(db) == 1
        items = [i for i in action_items.list_active(db) if i["kind"] == "nav_tab_blocked"]
        assert len(items) == 1 and items[0]["level"] == "warning"
    finally:
        db.query(ActionItem).filter(ActionItem.kind == "nav_tab_blocked").delete()
        db.commit()
        db.close()
