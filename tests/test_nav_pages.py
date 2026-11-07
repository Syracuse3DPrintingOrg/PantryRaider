"""Remote "go to this page" requests open only real app pages (FoodAssistant-ye112).

POST /events/navigate used to accept any same-origin path. A bare name sent by
a soak test ("expiring", "review") became /expiring/ (a JSON API) or a 404 JSON
reply, and the kiosk sat on that bare page with no app script to poll for the
next event or go home when idle. These tests pin the allowlist to the HTML
routes the app really registers, and the route's 400 for anything else.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services.url_safety import NAV_PAGES, nav_page_path  # noqa: E402

# HTML pages that are deliberately not navigation targets: the sign-in and PIN
# screens and the first-run wait page are reached by redirect, and the touch
# calibration page is a setup tool, not somewhere to send the screen.
NOT_NAV_TARGETS = {"ui/login", "ui/pin", "ui/getting-ready", "setup/calibrate/touch/page"}


def _html_get_routes() -> set[str]:
    from fastapi.responses import HTMLResponse
    from fastapi.routing import APIRoute
    from app.main import app

    def walk(routes):
        for r in routes:
            if isinstance(r, APIRoute):
                yield r
            inner = getattr(r, "original_router", None)
            if inner is not None:
                yield from walk(inner.routes)

    out = set()
    for r in walk(app.routes):
        if "GET" in r.methods and r.response_class is HTMLResponse and "{" not in r.path:
            out.add(r.path.lstrip("/"))
    return out


def test_every_nav_page_is_a_registered_html_page():
    pages = _html_get_routes()
    assert pages, "route walk found no HTML pages; the FastAPI internals changed"
    missing = sorted(p for p in NAV_PAGES if p not in pages)
    assert not missing, f"not HTML page routes: {missing}"


def test_every_html_page_is_listed_or_excluded():
    # A new page has to be added to NAV_PAGES (or to the exclusions above).
    unlisted = sorted(_html_get_routes() - NAV_PAGES - NOT_NAV_TARGETS)
    assert not unlisted, f"add these to url_safety.NAV_PAGES: {unlisted}"


@pytest.mark.parametrize("value,expected", [
    ("ui/cook", "ui/cook"), ("/ui/cook", "ui/cook"), ("ui/cook/", "ui/cook"),
    ("ui", "ui/"), ("ui/", "ui/"), ("/ui/?kiosk=1", "ui/?kiosk=1"),
    ("ui/camera?cam=1", "ui/camera?cam=1"),
    ("ui/timers?view=timers", "ui/timers?view=timers"),
    ("setup#pane-network", "setup#pane-network"),
    # Plain names: page names, nav keys and nav labels.
    ("expiring", "ui/expiring"), ("Expiring", "ui/expiring"),
    ("review", "ui/pending"), ("pending", "ui/pending"),
    ("glance", "ui/start"), ("home", "ui/"), ("on-the-line", "ui/current-recipe"),
    ("kitchen-guide", "ui/kitchen-guide"), ("guide", "ui/kitchen-guide"),
    ("weather?x=1", "ui/weather?x=1"), ("timers", "ui/timers"),
])
def test_nav_page_path_keeps_pages_and_maps_names(value, expected):
    assert nav_page_path(value) == expected


@pytest.mark.parametrize("value", [
    "expiring/", "ui/login", "ui/weather/data", "ui/camera/0/snapshot",
    "health", "current-recipe/", "timers/abc", "events/poll", "static/js/app.js",
    "nope", "ui/nope", "javascript:alert(1)", "//evil.example", "", "/",
])
def test_nav_page_path_refuses_what_is_not_a_page(value):
    assert nav_page_path(value) == ""


@pytest.fixture
def client(monkeypatch, tmp_path):
    import os
    from app.config import settings
    from app.services import ha_events
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://g", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    ha_events.reset()
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        ha_events.reset()
        os.chdir(cwd)


def test_navigate_route_refuses_api_paths_with_400(client):
    for bad in ("expiring/", "ui/weather/data", "health", "nope"):
        r = client.post("/events/navigate", json={"path": bad})
        assert r.status_code == 400, bad
        body = r.json()
        assert body["ok"] is False and "page" in body["error"]
    assert client.get("/events/poll?since=0").json()["events"] == []


def test_navigate_route_maps_a_bare_name_to_its_page(client):
    r = client.post("/events/navigate", json={"path": "expiring"})
    assert r.status_code == 200 and r.json()["path"] == "ui/expiring"
    ev = client.get("/events/poll?since=0").json()["events"][-1]
    assert ev["type"] == "navigate" and ev["path"] == "ui/expiring"
