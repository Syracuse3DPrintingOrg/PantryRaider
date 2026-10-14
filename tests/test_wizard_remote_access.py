"""The wizard turns Forager remote access on in the save that completes setup.

Before FoodAssistant-mzsci the wizard's Remote Access step POSTed
/setup/tunnel/enable while the install was still unconfigured, so that route
sat in main._SETUP_BYPASS and answered anyone on the network until setup
finished. Now the step only records the choice, the completing /setup/save
carries it, and the server runs the same tunnel_enable (with all its gates)
once the install is configured. The bypass entry is gone.

Network and WireGuard are faked: the cloud httpx client, the host bridge
client and the in-container wg helpers.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402

_PW = "kitchen-secret-pw"
_PUBLIC = "https://home.forager.pantryraider.app"


class _Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, routes, calls):
        self._routes = routes
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _match(self, method, url, kw):
        self._calls.append((method, url, kw.get("json")))
        for suffix, resp in self._routes.items():
            if url.endswith(suffix):
                return resp
        return _Resp(404)

    async def post(self, url, **kw):
        return self._match("POST", url, kw)

    async def get(self, url, **kw):
        return self._match("GET", url, kw)


def _cloud(calls, account_2fa=True):
    routes = {
        "/v1/instance/me": _Resp(200, {"account_2fa": account_2fa}),
        "/v1/tunnel/enable": _Resp(200, {
            "server_public_key": "SRVPUB", "server_endpoint": "vps.test:51820",
            "tunnel_ip": "10.99.4.9", "allowed_ips": "10.99.0.1/32",
            "public_url": _PUBLIC, "keepalive": 25}),
        "/v1/tunnel/disable": _Resp(200, {}),
    }
    return patch("app.routers.setup.httpx.AsyncClient",
                 side_effect=lambda *a, **kw: _FakeClient(routes, calls))


@pytest.fixture
def unconfigured(monkeypatch, tmp_path):
    """A server install sitting on the wizard: no inventory address and no
    password yet, linked to Forager by the person doing the setup, on a host
    that can run the tunnel in-container."""
    cwd = os.getcwd()
    os.chdir(SERVICE)
    s = settings
    for name, value in {
        "data_dir": str(tmp_path), "deployment_mode": "server",
        "grocy_base_url": "", "grocy_api_key": "", "auth_required": True,
        "auth_password": "", "totp_secret": "", "cloud_instance_token": "prc_token",
        "cloud_base_url": "https://cloud.test", "cloud_link_session": "",
        "tunnel_enabled": False, "tunnel_mode": "", "qr_public_url": "",
        "vision_provider": "none", "enrich_provider": "",
    }.items():
        monkeypatch.setattr(s, name, value, raising=False)
    monkeypatch.setattr("app.routers.setup.tunnel_local.wg_available", lambda: True)
    monkeypatch.setattr("app.routers.setup.tunnel_local.keygen", lambda: "DEVPUB")
    up_calls = []
    monkeypatch.setattr("app.routers.setup.tunnel_local.up",
                        lambda *a, **kw: up_calls.append(a))
    from app.main import app
    assert not settings.is_configured()
    try:
        with TestClient(app, client=("192.168.1.50", 50000)) as c:
            c.up_calls = up_calls
            yield c
    finally:
        os.chdir(cwd)


def _completing_save(**extra):
    body = {"grocy_base_url": "http://grocy.test:9383", "grocy_api_key": "gk",
            "auth_required": True, "auth_password": _PW}
    body.update(extra)
    return body


def test_completing_save_turns_remote_access_on(unconfigured):
    calls = []
    with _cloud(calls):
        r = unconfigured.post("/setup/save",
                              json=_completing_save(forager_remote_access=True))
    body = r.json()
    assert body["ok"] is True
    assert settings.is_configured()
    assert body["remote_access"] == {"ok": True, "public_url": _PUBLIC}
    assert settings.tunnel_enabled is True
    assert settings.tunnel_mode == "forager"
    assert settings.qr_public_url == _PUBLIC
    assert unconfigured.up_calls and unconfigured.up_calls[0][0] == "10.99.4.9"
    # The request flag is never stored as a setting of its own.
    assert not hasattr(settings, "forager_remote_access")


def test_completing_save_without_the_choice_leaves_the_tunnel_alone(unconfigured):
    calls = []
    with _cloud(calls):
        r = unconfigured.post("/setup/save", json=_completing_save())
    body = r.json()
    assert body["ok"] is True and "remote_access" not in body
    assert not any("/v1/tunnel/" in c[1] for c in calls)
    assert settings.tunnel_enabled is False


def test_a_refused_enable_still_finishes_setup(unconfigured):
    # No second factor anywhere: tunnel_enable's own gate refuses, and the
    # wizard gets that reason back while setup itself completes.
    calls = []
    with _cloud(calls, account_2fa=False):
        r = unconfigured.post("/setup/save",
                              json=_completing_save(forager_remote_access=True))
    body = r.json()
    assert body["ok"] is True
    assert settings.is_configured()
    assert body["remote_access"]["ok"] is False
    assert body["remote_access"].get("needs_2fa") is True
    assert settings.tunnel_enabled is False
    assert settings.tunnel_mode == ""
    assert not any(c[1].endswith("/v1/tunnel/enable") for c in calls)


def test_a_stranger_s_pre_setup_link_is_dropped_before_the_enable(
        unconfigured, monkeypatch):
    # The link was made from another browser (its stamp is not this session's),
    # so the completing save drops it first and the enable then refuses.
    monkeypatch.setattr(settings, "cloud_link_session", "someone-else", raising=False)
    calls = []
    with _cloud(calls):
        r = unconfigured.post("/setup/save",
                              json=_completing_save(forager_remote_access=True))
    body = r.json()
    assert body["ok"] is True
    assert body["remote_access"]["ok"] is False
    assert not any(c[1].endswith("/v1/tunnel/enable") for c in calls)
    assert settings.tunnel_enabled is False


def test_tunnel_enable_is_not_reachable_before_setup(unconfigured):
    # The route left _SETUP_BYPASS: before setup completes it is steered to the
    # wizard like any other page and the handler never runs.
    from app.main import _SETUP_BYPASS
    assert "/setup/tunnel/enable" not in _SETUP_BYPASS
    # The read-only status the wizard shows still answers JSON pre-setup.
    assert "/setup/tunnel/status" in _SETUP_BYPASS
    calls = []
    with _cloud(calls):
        r = unconfigured.post("/setup/tunnel/enable", json={},
                              follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/setup" in r.headers.get("location", "")
    assert calls == []
    assert unconfigured.up_calls == []
    assert settings.tunnel_enabled is False


def test_signed_in_enable_on_a_configured_install_is_unchanged(
        unconfigured, monkeypatch):
    # Settings, Remote Access on a finished install: the direct route still
    # turns the tunnel on for a signed-in owner.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test:9383", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "gk", raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret(_PW), raising=False)
    assert settings.is_configured()
    r = unconfigured.post("/ui/login", data={"password": _PW}, follow_redirects=False)
    assert r.status_code in (302, 303)
    calls = []
    with _cloud(calls):
        r = unconfigured.post("/setup/tunnel/enable", json={})
    assert r.json() == {"ok": True, "public_url": _PUBLIC}
    assert settings.tunnel_enabled is True


def test_configured_save_from_a_stranger_cannot_turn_it_on(unconfigured, monkeypatch):
    # Once configured, /setup/save is behind the login like the rest of /setup,
    # so the flag gives no way around it.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test:9383", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "gk", raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret(_PW), raising=False)
    calls = []
    with _cloud(calls):
        r = unconfigured.post("/setup/save", json={"forager_remote_access": True},
                              follow_redirects=False)
    assert r.status_code in (302, 303, 307, 401, 403)
    assert calls == []
    assert settings.tunnel_enabled is False
