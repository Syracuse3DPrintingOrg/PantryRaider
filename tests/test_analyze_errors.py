"""Analyze endpoint + Manage Pantry page answer failures honestly
(FoodAssistant-3w02).

The field bug: a food photo on /ui/add came back with the red banner
"Analysis failed: SyntaxError: The string did not match the expected pattern".
That is WebKit's JSON.parse failure, so the page had parsed a NON-JSON body.
Two defects lined up to produce it:

* the analyze router only caught NotImplementedError, so any other provider
  failure (an unreachable model, a reply that is not valid JSON, a field the
  parser cannot coerce) escaped as Starlette's default 500, whose body is the
  plain text "Internal Server Error"; and
* the page called `(await r.json()).detail` on the error response without
  checking the content type, so a plain-text 500 (or an HTML 413/502/504 from a
  proxy in front of the tunnelled server) threw a raw SyntaxError that landed on
  the banner verbatim.

These tests pin both contracts: the endpoint now answers JSON on every failure,
and the page ships the defensive reader that turns a non-JSON reply into a human
message.
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402
from app.providers.base import VisionProvider  # noqa: E402


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (180, 160, 140)).save(buf, format="PNG")
    return buf.getvalue()


class _RaisingProvider(VisionProvider):
    """A provider whose analyze calls raise a chosen exception, so the endpoint
    error handling is what's under test (no network, no real model)."""

    def __init__(self, exc: Exception):
        self._exc = exc

    async def analyze_food(self, image_data, mime_type):
        raise self._exc

    async def analyze_receipt(self, image_data, mime_type):
        raise self._exc

    async def health_check(self):
        return True


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    # A configured install so the setup-redirect middleware serves the routes.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test")
    monkeypatch.setattr(settings, "grocy_api_key", "k")
    monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    # The token-budget gate must never trip in these tests.
    from app.services import usage
    monkeypatch.setattr(usage, "over_budget", lambda *a, **k: False)
    from app.main import app
    try:
        yield app, TestClient(app)
    finally:
        app.dependency_overrides.clear()
        os.chdir(cwd)


def _use_provider(app, provider):
    from app.dependencies import get_vision_provider
    app.dependency_overrides[get_vision_provider] = lambda: provider


def _assert_json(resp):
    """The response body is real JSON, not a plain-text 500 (the whole point)."""
    assert "application/json" in resp.headers.get("content-type", "")
    return json.loads(resp.content)   # raises if the body is not JSON


# --- server contract ---------------------------------------------------------

def test_food_provider_value_error_answers_json_502(client):
    # A model reply the parser cannot coerce (e.g. float("a few")) used to
    # escape as a plain-text 500; now it is a clean JSON 502.
    app, c = client
    _use_provider(app, _RaisingProvider(ValueError("could not convert")))
    r = c.post("analyze/food", files={"file": ("f.png", _png_bytes(), "image/png")})
    assert r.status_code == 502
    body = _assert_json(r)
    assert isinstance(body["detail"], str) and body["detail"]


def test_food_non_json_model_reply_answers_json_502(client):
    # parse_json_response raises json.JSONDecodeError on a non-JSON model reply.
    app, c = client
    _use_provider(app, _RaisingProvider(json.JSONDecodeError("x", "not json", 0)))
    r = c.post("analyze/food", files={"file": ("f.png", _png_bytes(), "image/png")})
    assert r.status_code == 502
    _assert_json(r)


def test_receipt_provider_failure_answers_json_502(client):
    app, c = client
    _use_provider(app, _RaisingProvider(RuntimeError("boom")))
    r = c.post("analyze/receipt", files={"file": ("r.png", _png_bytes(), "image/png")})
    assert r.status_code == 502
    _assert_json(r)


def test_provider_httpexception_passes_through(client):
    # A provider that already mapped its failure to a user-facing JSON error
    # (the cloud's quota 429, unreachable 502) keeps its own status and detail.
    app, c = client
    _use_provider(app, _RaisingProvider(HTTPException(429, detail="Quota reached")))
    r = c.post("analyze/food", files={"file": ("f.png", _png_bytes(), "image/png")})
    assert r.status_code == 429
    assert _assert_json(r)["detail"] == "Quota reached"


def test_no_provider_answers_json_503(client):
    # No AI provider configured surfaces as a JSON 503 the page can read.
    app, c = client
    _use_provider(app, _RaisingProvider(NotImplementedError()))
    r = c.post("analyze/food", files={"file": ("f.png", _png_bytes(), "image/png")})
    assert r.status_code == 503
    _assert_json(r)


def test_unsupported_mime_is_json_400(client):
    app, c = client
    _use_provider(app, _RaisingProvider(RuntimeError("unused")))
    r = c.post("analyze/food", files={"file": ("f.gif", b"GIF89a", "image/gif")})
    assert r.status_code == 400
    _assert_json(r)


# --- client contract (the Manage Pantry page JS) -----------------------------
# The page's client logic lives in static/js/manage-pantry.js, not inline in
# add.html (kiosk caching, FoodAssistant-3c7k), so the contract is checked
# there; the rendered page must still link the script.

_PAGE_JS = (_SERVICE / "app" / "static" / "js" / "manage-pantry.js").read_text()


def _page(c):
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = c.get("/ui/add")
    assert r.status_code == 200
    return r.text


def test_page_ships_the_defensive_json_reader(client):
    _app, c = client
    html = _page(c)
    assert "static/js/manage-pantry.js" in html
    # The reader and its helpers are present, and the analyze path uses it.
    assert "function readJson(" in _PAGE_JS
    assert "function friendlyHttpError(" in _PAGE_JS
    assert "await readJson(r)" in _PAGE_JS
    # The old fragile pattern that threw a raw SyntaxError on a non-JSON body
    # must be gone.
    assert "(await r.json()).detail" not in _PAGE_JS


def test_page_has_human_messages_for_non_json_replies(client):
    assert "session has expired" in _PAGE_JS      # 401 over the tunnel
    assert "too large" in _PAGE_JS                 # 413 from a proxy
    assert "did not respond in time" in _PAGE_JS   # 502/503/504
