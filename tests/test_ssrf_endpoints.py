"""Endpoint-level SSRF refusals now that the shared egress guard is wired in.

Covers the three URL-taking endpoints the audit flagged:
  * POST /mealie/recipes/import-url (FoodAssistant-wa3g) refuses an internal
    target, including a hostname that resolves to loopback and an IPv4-mapped
    IPv6 literal.
  * POST /ha/connect (FoodAssistant-0h8i) refuses an internal base_url and does
    not persist it, and is admin-only.
  * The camera snapshot proxy connects through the pinned guard, so a saved
    camera whose name resolves to loopback at connect time is refused
    (FoodAssistant-wrib).

DNS resolution is mocked; nothing touches the network.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402
from app.services import egress  # noqa: E402


def _fake_getaddrinfo(mapping):
    def _fake(host, *a, **k):
        host = (host or "").strip("[]")
        ips = mapping.get(host)
        if ips is None:
            raise OSError("name not known")
        return [(2, 1, 6, "", (ip, 0)) for ip in ips]
    return _fake


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("admin-pw"), raising=False)
    monkeypatch.setattr(settings, "viewer_password", hash_secret("viewer-pw"), raising=False)
    monkeypatch.setattr(settings, "tunnel_enabled", False, raising=False)
    monkeypatch.setattr(settings, "qr_public_url", "", raising=False)
    monkeypatch.setattr(settings, "local_totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _login(client, password):
    r = client.post("/ui/login", data={"password": password}, follow_redirects=False)
    assert r.status_code in (302, 303, 307), r.text


# --- Recipe import (FoodAssistant-wa3g) -------------------------------------

def test_recipe_import_refuses_loopback_literal(client, monkeypatch):
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        _fake_getaddrinfo({"127.0.0.1": ["127.0.0.1"]}))
    _login(client, "admin-pw")
    r = client.post("/mealie/recipes/import-url",
                    json={"url": "http://127.0.0.1:9299/reboot"})
    assert r.status_code == 400
    assert "recipe site" in r.json()["detail"].lower()


def test_recipe_import_refuses_name_resolving_to_loopback(client, monkeypatch):
    # The old string guard let this through ("DNS decides"); the egress guard
    # resolves the name and refuses it.
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        _fake_getaddrinfo({"evil.example": ["127.0.0.1"]}))
    _login(client, "admin-pw")
    r = client.post("/mealie/recipes/import-url",
                    json={"url": "http://evil.example/recipe"})
    assert r.status_code == 400


def test_recipe_import_refuses_ipv4_mapped_ipv6(client, monkeypatch):
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        _fake_getaddrinfo({"::ffff:127.0.0.1": ["::ffff:127.0.0.1"]}))
    _login(client, "admin-pw")
    r = client.post("/mealie/recipes/import-url",
                    json={"url": "http://[::ffff:127.0.0.1]:9299/"})
    assert r.status_code == 400


def test_recipe_import_refuses_private_lan(client, monkeypatch):
    # No recipe lives on the LAN, so the public-only policy refuses it.
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        _fake_getaddrinfo({"192.168.1.9": ["192.168.1.9"]}))
    _login(client, "admin-pw")
    r = client.post("/mealie/recipes/import-url",
                    json={"url": "http://192.168.1.9/x"})
    assert r.status_code == 400


# --- HA connect (FoodAssistant-0h8i) ----------------------------------------

def test_ha_connect_refuses_loopback_and_does_not_persist(client, monkeypatch):
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        _fake_getaddrinfo({"127.0.0.1": ["127.0.0.1"]}))
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    _login(client, "admin-pw")
    r = client.post("/ha/connect",
                    json={"base_url": "http://127.0.0.1:9284", "token": "t"})
    assert r.status_code == 400
    # The refusal happens before any save, so the attacker base_url is not stored.
    assert settings.streamdeck_ha_base_url == ""


def test_ha_connect_allows_lan_home_assistant(client, monkeypatch):
    # HA legitimately lives on the LAN, so a private address is allowed. The
    # probe fetch is guarded but a private target passes the policy; we stub the
    # actual client so no socket is opened.
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        _fake_getaddrinfo({"192.168.1.10": ["192.168.1.10"]}))
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **k):
            return _FakeResp()

    monkeypatch.setattr(egress, "guarded_async_client", lambda **k: _FakeClient())
    _login(client, "admin-pw")
    r = client.post("/ha/connect",
                    json={"base_url": "http://192.168.1.10:8123", "token": "t"})
    assert r.status_code == 200
    assert settings.streamdeck_ha_base_url == "http://192.168.1.10:8123"
    assert r.json()["verified"] is True


def test_ha_connect_is_admin_only(client, monkeypatch):
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        _fake_getaddrinfo({"192.168.1.10": ["192.168.1.10"]}))
    _login(client, "viewer-pw")
    r = client.post("/ha/connect",
                    json={"base_url": "http://192.168.1.10:8123", "token": "t"})
    assert r.status_code == 403


# --- Camera snapshot rebinding (FoodAssistant-wrib) -------------------------

def test_camera_snapshot_refuses_rebound_loopback_at_connect(client, monkeypatch):
    # A saved manual camera whose name resolves to loopback: the pre-check would
    # refuse it, but even if it slipped past, the guarded connection re-resolves
    # and refuses. Here the name resolves to loopback, so it is blocked.
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        _fake_getaddrinfo({"cam.attacker": ["127.0.0.1"]}))
    from app.services import cameras
    monkeypatch.setattr(cameras.socket, "getaddrinfo",
                        _fake_getaddrinfo({"cam.attacker": ["127.0.0.1"]}))
    monkeypatch.setattr(settings, "streamdeck_cameras",
                        [{"name": "evil", "snapshot_url": "http://cam.attacker/snap.jpg"}],
                        raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    _login(client, "viewer-pw")
    r = client.get("/ui/camera/0/snapshot")
    assert r.status_code == 400
