"""The LAN-only gate must not be fooled behind a reverse proxy (FoodAssistant-
gbs8).

Pairing (which mints an API key) and the Cub firmware endpoints were reachable
from the internet whenever the app sat behind a reverse proxy or tunnel, because
request.client.host became the proxy's own private IP and the old check read
that as "on the LAN". The gate now also refuses a request that carries a
forwarding header or arrived over the public tunnel Host.

These drive the endpoints through the TestClient with a forwarding header set,
so the request looks like it came through a proxy.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import pairing  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    # No app password, so the auth middleware is out of the way and we test the
    # endpoints' own LAN gate directly.
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "local_device_pairing_enabled", True, raising=False)
    monkeypatch.setattr(settings, "tunnel_enabled", False, raising=False)
    monkeypatch.setattr(settings, "qr_public_url", "", raising=False)
    pairing.reset()
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        pairing.reset()
        os.chdir(cwd)


def _lan(monkeypatch):
    """Make the immediate peer look like a real LAN client."""
    monkeypatch.setattr("app.services.request_origin._is_private_client",
                        lambda host: True)


def test_pairing_request_refused_when_forwarded_header_present(client, monkeypatch):
    # A private peer, but an X-Forwarded-For header means a proxy is in front, so
    # the real client is remote: pairing must be refused.
    _lan(monkeypatch)
    r = client.post("/api/pairing/request", json={"hostname": "kitchen-pi"},
                    headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403
    assert "local network" in r.json()["error"]


def test_pairing_request_allowed_direct_lan(client, monkeypatch):
    # Same private peer, no forwarding header: a genuine LAN request pairs.
    _lan(monkeypatch)
    r = client.post("/api/pairing/request", json={"hostname": "kitchen-pi"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_pairing_request_refused_over_tunnel_host(client, monkeypatch):
    # Reached over the public tunnel Host with the tunnel on: not local.
    _lan(monkeypatch)
    monkeypatch.setattr(settings, "tunnel_enabled", True, raising=False)
    monkeypatch.setattr(settings, "qr_public_url",
                        "https://home.forager.pantryraider.app", raising=False)
    r = client.post("/api/pairing/request", json={"hostname": "kitchen-pi"},
                    headers={"Host": "home.forager.pantryraider.app"})
    assert r.status_code == 403


def test_cub_firmware_gate_refuses_proxied_request(monkeypatch):
    # The pure gate the Cub-firmware middleware check uses: a proxied request is
    # not local, so the firmware endpoints are not internet-exposed behind a
    # proxy. (Driven at the helper level; the middleware calls the same thing.)
    from app.services.request_origin import is_local_network
    # Direct LAN client, no proxy: local (Cub on the LAN still updates).
    assert is_local_network("pantry.local", "", False, client_host="192.168.1.20")
    # Behind a proxy: refused.
    assert not is_local_network("pantry.local", "", False,
                                client_host="192.168.1.20", has_forwarded=True)
