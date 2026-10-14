"""Signing in with a password: Home Assistant's Ingress counts as home, and a
refusal from outside says why and offers the setup code (routers/ui.py).

An add-on user who sets a password signs in through Ingress with the password
alone. A bare password from outside is still refused while the device has no
two-factor: through a proxy or tunnel (a reverse proxy on this same machine
included, with or without forwarding headers) the copy says so, from a public
address it says "outside your home network", and the admin refusal links to
the setup code. The kitchen screen on this device still signs in as before.
"""
from __future__ import annotations

import html
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402
from app.routers import ui as ui_router  # noqa: E402

_PW = "kitchen-secret"
_VIEWER_PW = "viewer-secret"
_LINK = 'href="ui/setup-code"'

# The header set Home Assistant's Supervisor sends on every Ingress request.
_INGRESS_PEER = ("172.30.32.2", 50000)
_INGRESS_HEADERS = {
    "X-Ingress-Path": "/api/hassio_ingress/abc123",
    "X-Hass-Source": "core.ingress",
    "X-Forwarded-For": "192.168.1.20, 172.30.32.1",
    "X-Forwarded-Host": "homeassistant.local:8123",
    "X-Forwarded-Proto": "http",
}


@pytest.fixture
def app(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "gk", raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret(_PW), raising=False)
    monkeypatch.setattr(settings, "viewer_password", hash_secret(_VIEWER_PW), raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", False, raising=False)
    monkeypatch.setattr(settings, "local_totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "local_totp_recovery", [], raising=False)
    monkeypatch.setattr(settings, "tunnel_enabled", False, raising=False)
    monkeypatch.setattr(settings, "qr_public_url", "", raising=False)
    monkeypatch.setattr(settings, "cloud_instance_token", "", raising=False)
    monkeypatch.delenv("PANTRY_RAIDER_ADDON", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    from app.main import app as fastapi_app
    try:
        yield fastapi_app
    finally:
        os.chdir(cwd)


def _http10(asgi_app):
    """The app as an HTTP/1.0 client reaches it (nginx's default upstream)."""
    async def shim(scope, receive, send):
        await asgi_app(dict(scope, http_version="1.0") if scope["type"] == "http" else scope,
                       receive, send)
    return shim


def _login(client, password=_PW, **kw):
    return client.post("/ui/login", data={"mode": "local", "password": password},
                       follow_redirects=False, **kw)


def _signed_in(r) -> bool:
    return r.status_code in (302, 303) and r.headers["location"].endswith("/ui/")


def _refused_with(r, message: str, *, link: bool) -> None:
    assert r.status_code == 401
    text = html.unescape(r.text)
    assert message in text
    assert "second factor" in text
    assert (_LINK in r.text) is link


# -- Ingress ------------------------------------------------------------------

def test_ingress_password_signs_in_without_two_factor(app, monkeypatch):
    monkeypatch.setenv("PANTRY_RAIDER_ADDON", "1")
    client = TestClient(app, client=_INGRESS_PEER, headers=dict(_INGRESS_HEADERS))
    assert _signed_in(_login(client))


def test_ingress_shape_without_the_addon_runtime_is_a_proxy(app):
    client = TestClient(app, client=_INGRESS_PEER, headers=dict(_INGRESS_HEADERS))
    _refused_with(_login(client), ui_router._PROXY_2FA_MSG, link=True)


# -- proxies and tunnels ------------------------------------------------------

def test_plain_proxy_gets_the_proxy_copy_and_the_setup_code(app):
    client = TestClient(app, client=("192.168.1.5", 50000),
                        headers={"X-Forwarded-For": "203.0.113.9"})
    _refused_with(_login(client), ui_router._PROXY_2FA_MSG, link=True)


def test_public_peer_gets_the_outside_copy_and_the_setup_code(app):
    client = TestClient(app, client=("203.0.113.9", 50000))
    _refused_with(_login(client), ui_router._LOCAL_INTERNET_MSG, link=True)


def test_same_host_proxy_with_forwarding_headers(app):
    client = TestClient(app, client=("127.0.0.1", 50000), base_url="http://pantry.example.com",
                        headers={"X-Forwarded-For": "192.168.1.30"})
    _refused_with(_login(client), ui_router._PROXY_2FA_MSG, link=True)


def test_same_host_proxy_without_forwarding_headers_by_referer(app):
    # nginx on its defaults: Host rewritten to the upstream address and no
    # forwarding header, but the browser's Referer names the public site.
    client = TestClient(app, client=("127.0.0.1", 50000), base_url="http://127.0.0.1:9284",
                        headers={"Sec-Fetch-Site": "same-origin",
                                 "Referer": "https://pantry.example.com/ui/login"})
    _refused_with(_login(client), ui_router._PROXY_2FA_MSG, link=True)


def test_same_host_proxy_without_forwarding_headers_over_http10(app):
    client = TestClient(_http10(app), client=("127.0.0.1", 50000),
                        base_url="http://127.0.0.1:9284")
    _refused_with(_login(client), ui_router._PROXY_2FA_MSG, link=True)


def test_kitchen_screen_still_signs_in_with_the_password(app):
    client = TestClient(app, client=("127.0.0.1", 50000), base_url="http://localhost:9284")
    assert _signed_in(_login(client))


def test_lan_still_signs_in_with_the_password(app):
    client = TestClient(app, client=("192.168.1.20", 50000))
    assert _signed_in(_login(client))


# -- two-factor on, and viewers -----------------------------------------------

def test_two_factor_on_moves_to_the_code_step_with_no_link(app, monkeypatch):
    from app import totp as local_totp
    monkeypatch.setattr(settings, "local_totp_secret", local_totp.generate_totp_secret(),
                        raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", True, raising=False)
    client = TestClient(app, client=("192.168.1.5", 50000),
                        headers={"X-Forwarded-For": "203.0.113.9"})
    r = _login(client)
    assert r.status_code == 200
    assert 'name="totp_code"' in r.text
    assert _LINK not in r.text
    assert ui_router._PROXY_2FA_MSG not in html.unescape(r.text)


def test_viewer_password_from_outside_gets_the_message_without_the_link(app):
    client = TestClient(app, client=("203.0.113.9", 50000))
    _refused_with(_login(client, _VIEWER_PW), ui_router._LOCAL_INTERNET_MSG, link=False)
    proxied = TestClient(app, client=("192.168.1.5", 50000),
                         headers={"X-Forwarded-For": "203.0.113.9"})
    _refused_with(_login(proxied, _VIEWER_PW), ui_router._PROXY_2FA_MSG, link=False)


def test_viewer_password_through_ingress_signs_in(app, monkeypatch):
    monkeypatch.setenv("PANTRY_RAIDER_ADDON", "1")
    client = TestClient(app, client=_INGRESS_PEER, headers=dict(_INGRESS_HEADERS))
    assert _signed_in(_login(client, _VIEWER_PW))


def test_refusal_copy_points_at_the_ways_in():
    for msg in (ui_router._PROXY_2FA_MSG, ui_router._LOCAL_INTERNET_MSG):
        assert "second factor" in msg
        assert "Forager" in msg and "setup code" in msg
        assert "\u2014" not in msg
