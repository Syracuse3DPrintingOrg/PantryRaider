"""With no password set, device-level actions stay on the device's own screen.

A second screen used to ship with the login turned off, and the auth middleware
returns immediately when no password is stored, so anything on the network could
reboot the device, trigger an update or a restore, read the recovery hotspot
password, or act on the main server through the forwarding routes (which carry
that server's full-access key). Those stay closed to remote callers until a
password exists; the local display, deck and sensor reader all connect over the
loopback address and are unaffected (security audit, Sep 2026).
"""
import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    from fastapi.testclient import TestClient
    from app.main import app

    def _make(host="192.168.1.50"):
        return TestClient(app, client=(host, 50000))

    try:
        yield _make
    finally:
        os.chdir(cwd)


def _satellite(monkeypatch, password=""):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "http://server.test", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "upstream-key", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", password, raising=False)


def _server(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)


HOST_ACTIONS = [
    ("post", "/setup/update"),
    ("post", "/setup/restore"),
    ("post", "/setup/maintenance/reboot"),
    ("post", "/setup/deployment/to-hosted"),
    ("post", "/setup/tunnel/enable"),
    ("get", "/setup/ap/config"),
    ("post", "/setup/ap/config"),
    ("post", "/setup/network/hostname"),
]


@pytest.mark.parametrize("method,path", HOST_ACTIONS)
def test_device_actions_refuse_a_remote_caller_without_a_password(
        make_client, monkeypatch, method, path):
    _satellite(monkeypatch)
    client = make_client()
    kw = {"json": {}} if method == "post" else {}
    r = getattr(client, method)(path, follow_redirects=False, **kw)
    assert r.status_code == 403, path


@pytest.mark.parametrize("method,path", HOST_ACTIONS)
def test_the_devices_own_screen_still_reaches_them(
        make_client, monkeypatch, method, path):
    _satellite(monkeypatch)
    client = make_client("127.0.0.1")
    kw = {"json": {}} if method == "post" else {}
    r = getattr(client, method)(path, follow_redirects=False, **kw)
    assert r.status_code != 403, path


def test_rejoining_wifi_stays_open_so_a_lost_device_can_be_rescued(
        make_client, monkeypatch):
    # The recovery hotspot is the only way back onto the network for a device
    # that fell off it, and a browser on that hotspot is not the loopback
    # address, so these two must not be locked behind the password.
    _satellite(monkeypatch)
    client = make_client()
    assert client.get("/setup/network/scan").status_code != 403
    assert client.post("/setup/network/wifi", json={}).status_code != 403


def test_forwarding_routes_refuse_a_remote_caller_without_a_password(
        make_client, monkeypatch):
    _satellite(monkeypatch)
    client = make_client()
    for path in ("/pending/", "/timers", "/audit/status", "/action-items"):
        assert client.get(path, follow_redirects=False).status_code == 403, path
    assert client.post("/pending/scan", json={"barcode": "1"}).status_code == 403


def test_a_standalone_install_without_a_password_is_untouched(
        make_client, monkeypatch):
    # Every install other than a second screen only ends up without a password
    # because its owner turned the login off on purpose, usually because another
    # layer handles it. Narrowing their settings page would break a working
    # setup rather than close a hole they did not open.
    _server(monkeypatch)
    client = make_client()
    assert client.get("/timers", follow_redirects=False).status_code != 403
    assert client.get("/action-items", follow_redirects=False).status_code != 403
    assert client.post("/setup/update", json={},
                       follow_redirects=False).status_code != 403
    assert client.get("/setup/ap/config",
                      follow_redirects=False).status_code != 403


def test_a_valid_api_key_still_works_without_a_password(make_client, monkeypatch):
    # An API key is a deliberate credential and the only one a headless client
    # has. Satellite sync, the Stream Deck, the sensor reader and the Home
    # Assistant integration all authenticate this way, so the no-password gate
    # must not refuse them.
    _satellite(monkeypatch)
    monkeypatch.setattr(settings, "api_key", "device-key", raising=False)
    client = make_client()
    headers = {"X-API-Key": "device-key"}
    assert client.get("/timers", headers=headers,
                      follow_redirects=False).status_code != 403
    assert client.post("/setup/update", json={}, headers=headers,
                       follow_redirects=False).status_code != 403


def test_a_wrong_api_key_is_still_refused_without_a_password(make_client, monkeypatch):
    _satellite(monkeypatch)
    monkeypatch.setattr(settings, "api_key", "device-key", raising=False)
    client = make_client()
    headers = {"X-API-Key": "not-the-key"}
    assert client.get("/timers", headers=headers,
                      follow_redirects=False).status_code == 403
    assert client.post("/setup/update", json={}, headers=headers,
                       follow_redirects=False).status_code == 403


def test_a_password_restores_the_normal_login_rules(make_client, monkeypatch):
    # With a password set the gate is irrelevant: the usual auth answer applies
    # (401 for an API path with no session), not the no-password refusal.
    from app.passwords import hash_secret
    _satellite(monkeypatch, password=hash_secret("hunter2"))
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    client = make_client()
    assert client.post("/setup/update", json={}).status_code == 401
