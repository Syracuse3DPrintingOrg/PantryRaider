"""URL-import fixes (FoodAssistant-btd7).

Two concerns, both without touching the network:
  * friendly_fetch_error maps an httpx failure to a short, actionable message
    (404/410, 401/403, timeout/DNS) instead of leaking the raw exception.
  * the LLM-fallback fetch presents as an ordinary browser (a real User-Agent
    plus Accept headers) so bot-protected recipe sites answer it.
"""
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.routers.mealie import _BROWSER_HEADERS, friendly_fetch_error

_SERVICE_DIR = Path(__file__).parent.parent / "service"


# ── friendly_fetch_error ───────────────────────────────────────────────────────

def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://example.com/recipe")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


@pytest.mark.parametrize("code", [404, 410])
def test_not_found_message(code):
    msg = friendly_fetch_error(_status_error(code))
    assert "could not be found" in msg
    assert "single recipe" in msg


@pytest.mark.parametrize("code", [401, 403])
def test_blocked_message(code):
    msg = friendly_fetch_error(_status_error(code))
    assert "blocked" in msg
    assert "copying the recipe text" in msg


def test_other_status_message():
    msg = friendly_fetch_error(_status_error(500))
    assert "error" in msg.lower()


def test_timeout_message():
    msg = friendly_fetch_error(httpx.ConnectTimeout("slow"))
    assert "Could not reach that site" in msg


def test_dns_connect_message():
    msg = friendly_fetch_error(httpx.ConnectError("name resolution failed"))
    assert "Could not reach that site" in msg


def test_no_raw_exception_leaks():
    # A generic failure still returns friendly copy, never the exception text.
    msg = friendly_fetch_error(RuntimeError("Client error '404 Not Found' for url ..."))
    assert "404" not in msg
    assert "Could not fetch that page" in msg


def test_browser_headers_look_like_a_browser():
    ua = _BROWSER_HEADERS["User-Agent"]
    assert "Mozilla/5.0" in ua and "Chrome/" in ua
    assert "Pantry Raider" not in ua
    assert "Accept" in _BROWSER_HEADERS and "Accept-Language" in _BROWSER_HEADERS


# ── The fallback fetch sends the browser headers ───────────────────────────────

class _FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class _FakeClient:
    """Records the headers the endpoint's fallback fetch sends."""
    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None):
        _FakeClient.captured = {"url": url, "headers": headers}
        return _FakeResponse("<html><body>" + ("recipe words " * 80) + "</body></html>")


class _FakeProvider:
    async def extract_recipe(self, page_text=None, **kwargs):
        return {"name": "Mock Recipe", "ingredients": ["a"], "instructions": ["b"]}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    cwd = os.getcwd()
    os.chdir(_SERVICE_DIR)
    try:
        from app.config import settings
        settings.data_dir = str(tmp_path_factory.mktemp("data"))
        from app.main import app
        settings.grocy_base_url = "http://grocy.test"
        settings.grocy_api_key = "test-grocy-key"
        settings.vision_provider = "gemini"
        settings.gemini_api_key = "test-gemini-key"
        settings.mealie_base_url = "http://mealie.test"
        settings.mealie_api_key = "test-mealie-key"
        settings.auth_required = False
        settings.auth_password = ""
        with TestClient(app) as c:
            yield c
    finally:
        os.chdir(cwd)


def test_fallback_fetch_uses_browser_ua(client, monkeypatch):
    import app.dependencies as deps
    from app.services.mealie import MealieClient
    from app.routers import mealie as mealie_router

    async def _scraper_fails(self, url):
        raise RuntimeError("Mealie scraper can't read this site")

    monkeypatch.setattr(MealieClient, "create_recipe_from_url", _scraper_fails)
    monkeypatch.setattr(mealie_router.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(deps, "get_enrich_provider", lambda: _FakeProvider())

    r = client.post("/mealie/recipes/import-url",
                    json={"url": "https://example.com/best-lasagna"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["saved"] is False
    assert body["recipe"]["name"] == "Mock Recipe"

    sent = _FakeClient.captured["headers"]
    assert "Mozilla/5.0" in sent["User-Agent"]
    assert "Pantry Raider" not in sent["User-Agent"]
    assert sent["Accept-Language"]


def test_fallback_fetch_403_is_friendly(client, monkeypatch):
    import app.dependencies as deps
    from app.services.mealie import MealieClient
    from app.routers import mealie as mealie_router

    async def _scraper_fails(self, url):
        raise RuntimeError("scraper failed")

    class _BlockingClient(_FakeClient):
        async def get(self, url, headers=None):
            req = httpx.Request("GET", url)
            resp = httpx.Response(403, request=req)
            raise httpx.HTTPStatusError("blocked", request=req, response=resp)

    monkeypatch.setattr(MealieClient, "create_recipe_from_url", _scraper_fails)
    monkeypatch.setattr(mealie_router.httpx, "AsyncClient", _BlockingClient)
    monkeypatch.setattr(deps, "get_enrich_provider", lambda: _FakeProvider())

    r = client.post("/mealie/recipes/import-url",
                    json={"url": "https://example.com/blocked"})
    assert r.status_code == 502
    assert "blocked" in r.json()["detail"]
