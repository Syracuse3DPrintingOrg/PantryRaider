"""Convert a kitchen between Forager-connected and self-hosted, both
directions, and keep the local core working with no internet
(FoodAssistant-nipb).

Covers:
  - the pure cloud_convert_updates helper (what changes on a switch);
  - the /setup/cloud/unlink convert flow: it forgets the Forager link and,
    when Forager was the scanner, stops the app claiming AI is configured
    against a service it just left, even when the cloud is unreachable;
  - the Forager pane shows the convert action, the kept-vs-changed
    explanation, and the offline note in both the linked and unlinked states;
  - a local-core page (the inventory dashboard) still renders while linked to
    Forager with the cloud unreachable.
All HTTP is mocked: no network.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402
from app.routers.setup import cloud_convert_updates  # noqa: E402


# --- the pure helper --------------------------------------------------------

def test_convert_always_forgets_the_link():
    up = cloud_convert_updates("gemini", "", "", False)
    assert up["cloud_instance_token"] == ""


def test_convert_off_forager_scanner_stops_claiming_ai():
    # Forager was the scanner and enricher: both stop pointing at the cloud so
    # the app does not offer AI actions that can no longer work.
    up = cloud_convert_updates("cloud", "cloud", "", False)
    assert up["vision_provider"] == "gemini"
    assert up["vision_provider"] != "cloud"
    assert up["enrich_provider"] == ""


def test_convert_keeps_a_self_hosted_provider_untouched():
    # A kitchen already on its own key is left alone; only the link is dropped.
    up = cloud_convert_updates("gemini", "gemini", "", False)
    assert "vision_provider" not in up
    assert "enrich_provider" not in up
    assert up == {"cloud_instance_token": ""}


def test_convert_turns_off_forager_remote_access():
    up = cloud_convert_updates("cloud", "cloud", "forager", True)
    assert up["tunnel_mode"] == ""
    assert up["tunnel_enabled"] is False
    # A legacy "subscription" mode reads as Forager and is cleared too.
    up2 = cloud_convert_updates("cloud", "cloud", "subscription", True)
    assert up2["tunnel_mode"] == ""


def test_convert_leaves_a_cloudflare_tunnel_alone():
    # Cloudflare remote access is self-hosted, so switching off Forager must not
    # disturb it.
    up = cloud_convert_updates("cloud", "cloud", "cloudflare", False)
    assert "tunnel_mode" not in up
    assert "tunnel_enabled" not in up


# --- the convert flow (endpoint) --------------------------------------------

class _FakeAsyncClient:
    """httpx.AsyncClient stand-in serving one canned reply (or raising)."""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def delete(self, url, **kwargs):
        if self._error:
            raise self._error
        return self._response

    async def post(self, url, **kwargs):
        if self._error:
            raise self._error
        return self._response

    async def get(self, url, **kwargs):
        if self._error:
            raise self._error
        return self._response


def _resp(status, body):
    return httpx.Response(status, json=body,
                          request=httpx.Request("DELETE", "https://cloud.test"))


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "cloud_base_url", "https://cloud.test")
    try:
        with patch.object(type(settings), "is_configured", lambda self: True):
            yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_convert_endpoint_drops_ai_gate(client, monkeypatch):
    # Linked, with Forager as the scanner: after switching to self-hosted the
    # app must not still claim AI is configured against the (now gone) cloud.
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_old")
    monkeypatch.setattr(settings, "vision_provider", "cloud")
    monkeypatch.setattr(settings, "enrich_provider", "cloud")
    monkeypatch.setattr(settings, "tunnel_enabled", False)
    assert settings.ai_configured()  # true while linked
    fake = _FakeAsyncClient(_resp(200, {"ok": True}))
    from app.routers import setup as setup_router
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        r = client.post("/setup/cloud/unlink")
    assert r.json() == {"ok": True}
    assert settings.cloud_instance_token == ""
    assert settings.vision_provider != "cloud"
    assert not settings.cloud_linked()
    assert not settings.ai_configured()


def test_convert_endpoint_survives_unreachable_cloud(client, monkeypatch):
    # The switch is local first: an unreachable Forager must never block it, and
    # the AI gate still flips off so scanning does not silently point at nothing.
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_old")
    monkeypatch.setattr(settings, "vision_provider", "cloud")
    monkeypatch.setattr(settings, "enrich_provider", "cloud")
    monkeypatch.setattr(settings, "tunnel_enabled", False)
    fake = _FakeAsyncClient(error=httpx.ConnectError("down"))
    from app.routers import setup as setup_router
    with patch.object(setup_router.httpx, "AsyncClient", fake):
        r = client.post("/setup/cloud/unlink")
    assert r.json() == {"ok": True}
    assert settings.cloud_instance_token == ""
    assert settings.vision_provider != "cloud"
    assert not settings.ai_configured()


# --- the pane copy (both states) --------------------------------------------

def _render_forager_pane(client, monkeypatch, *, linked: bool) -> str:
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "cloud_instance_token",
                        "prc_tok" if linked else "")
    with patch.object(type(settings), "is_configured", lambda self: True), \
         patch("app.routers.setup.is_raspberry_pi", return_value=False), \
         patch("app.templating.is_raspberry_pi", return_value=False):
        r = client.get("/setup")
    assert r.status_code == 200
    return r.text


def test_pane_shows_convert_kept_changed_and_offline_note_both_states(client, monkeypatch):
    for linked in (True, False):
        html = _render_forager_pane(client, monkeypatch, linked=linked)
        # The convert action / self-hosted choice is present.
        assert "Self-hosted or connected to Forager" in html
        # Kept-vs-changed explanation.
        assert "What you keep" in html
        assert "What a Forager account adds" in html
        assert "inventory and recipes" in html
        # Offline note.
        assert "keeps working on your own network even if Forager or the internet is down" in html
    # The active switch button appears only while linked; the self-hosted-now
    # framing appears only while unlinked.
    linked_html = _render_forager_pane(client, monkeypatch, linked=True)
    assert "Switch to self-hosted" in linked_html
    unlinked_html = _render_forager_pane(client, monkeypatch, linked=False)
    assert "You are running self-hosted right now" in unlinked_html


# --- offline core -----------------------------------------------------------

class _RaiseAsyncClient:
    def __init__(self, *a, **k):
        raise httpx.ConnectError("no internet")


def test_inventory_page_renders_while_linked_and_cloud_down(client, monkeypatch):
    # A Forager-connected kitchen must render its local core with the cloud (and
    # the internet) unreachable. Patch httpx so any outbound call would fail;
    # the page render must not depend on one.
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_tok")
    monkeypatch.setattr(settings, "vision_provider", "cloud")
    with patch.object(httpx, "AsyncClient", _RaiseAsyncClient):
        r = client.get("/ui/inventory")
    assert r.status_code == 200
    assert "inventory" in r.text.lower()
