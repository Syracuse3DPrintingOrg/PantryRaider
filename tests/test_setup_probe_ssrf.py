"""The setup connection tests must not become an unauthenticated fetch
primitive (SSRF, FoodAssistant-3okp).

/setup/test/grocy, /setup/test/mealie and /setup/first-run/* are reachable
WITHOUT authentication during the unconfigured window (they are in the setup
bypass so a fresh device can be set up at all), and each fetches an address the
caller supplies. Two things have to hold:

* the fetch is refused for addresses no real backend uses (loopback, which the
  app itself trusts as an administrator, and link-local, which covers the cloud
  metadata address), while ordinary LAN and public backends still work, because
  that is where Grocy and Mealie actually live;
* a failing probe reports a status, never the fetched body, so the test cannot
  be used to read whatever answered.

No network: DNS resolution and the HTTP fetch are both mocked.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import cameras  # noqa: E402


def _fake_getaddrinfo(mapping):
    """A getaddrinfo stand-in mapping a host string to one or more IPs."""
    def _fake(host, port, *a, **k):
        ips = mapping.get(host)
        if ips is None:
            raise OSError("name not known")
        return [(2, 1, 6, "", (ip, 0)) for ip in ips]
    return _fake


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _Client:
    """Stands in for httpx.AsyncClient; records the URL and returns a canned
    response so no probe ever leaves the test."""
    last_url = None
    resp = _Resp()

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **k):
        type(self).last_url = url
        return type(self).resp


@pytest.fixture
def client(monkeypatch, tmp_path):
    """An UNCONFIGURED install, which is exactly the window these endpoints are
    reachable without a session."""
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    from app.routers import setup as setup_router
    monkeypatch.setattr(setup_router.httpx, "AsyncClient", _Client)
    _Client.last_url = None
    _Client.resp = _Resp()
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


@pytest.mark.parametrize("host,ip", [
    ("metadata.test", "169.254.169.254"),  # cloud metadata
    ("rebind.test", "127.0.0.1"),          # a name that resolves to loopback
])
def test_grocy_probe_refuses_addresses_no_backend_uses(client, monkeypatch, host, ip):
    monkeypatch.setattr(cameras.socket, "getaddrinfo",
                        _fake_getaddrinfo({host: [ip]}))
    r = client.post("/setup/test/grocy",
                    json={"grocy_base_url": f"http://{host}", "grocy_api_key": "k"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "not allowed" in body["error"].lower()
    # The refusal has to happen BEFORE any fetch is attempted.
    assert _Client.last_url is None


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_a_made_up_loopback_address_is_never_fetched(client, monkeypatch, host):
    """A local-looking entry legitimately falls back to our own fixed
    candidates (a co-hosted Grocy really can be on loopback), so the probe may
    still succeed. What must never happen is fetching the address the CALLER
    named: that is the one an attacker controls, and the app trusts a loopback
    client as an administrator."""
    monkeypatch.setattr(cameras.socket, "getaddrinfo",
                        _fake_getaddrinfo({host: ["127.0.0.1"],
                                           "localhost": ["127.0.0.1"],
                                           "127.0.0.1": ["127.0.0.1"],
                                           "grocy": ["172.18.0.5"]}))
    fetched: list[str] = []

    class _Recorder(_Client):
        async def get(self, url, **k):
            fetched.append(url)
            return type(self).resp

    from app.routers import setup as setup_router
    monkeypatch.setattr(setup_router.httpx, "AsyncClient", _Recorder)
    _Recorder.resp = _Resp(200, {"grocy_version": "4.0.3"})

    client.post("/setup/test/grocy",
                json={"grocy_base_url": f"http://{host}:8000", "grocy_api_key": "k"})
    # The caller's own address (the app's admin port) was never contacted.
    assert not any(f"{host}:8000" in u for u in fetched), fetched


def test_mealie_probe_refuses_loopback(client, monkeypatch):
    monkeypatch.setattr(cameras.socket, "getaddrinfo",
                        _fake_getaddrinfo({"127.0.0.1": ["127.0.0.1"]}))
    r = client.post("/setup/test/mealie",
                    json={"mealie_base_url": "http://127.0.0.1:9000",
                          "mealie_api_key": "k"})
    assert r.json()["ok"] is False
    assert "not allowed" in r.json()["error"].lower()
    assert _Client.last_url is None


def test_a_real_lan_backend_is_still_allowed(client, monkeypatch):
    """The guard must not break setup: Grocy lives on the LAN or as a sibling
    container (the shipped default is http://grocy:80), so those must go
    through."""
    monkeypatch.setattr(cameras.socket, "getaddrinfo",
                        _fake_getaddrinfo({"grocy": ["172.18.0.5"],
                                           "nas.lan": ["192.168.1.50"]}))
    _Client.resp = _Resp(200, {"grocy_version": "4.0.3"})
    r = client.post("/setup/test/grocy",
                    json={"grocy_base_url": "http://grocy", "grocy_api_key": "k"})
    assert r.json()["ok"] is True
    assert _Client.last_url and _Client.last_url.startswith("http://grocy")

    _Client.last_url = None
    r = client.post("/setup/test/grocy",
                    json={"grocy_base_url": "http://nas.lan:9283",
                          "grocy_api_key": "k"})
    assert r.json()["ok"] is True


def test_a_failing_probe_never_echoes_what_answered(client, monkeypatch):
    """A non-2xx must report the status only. Echoing the fetched body would
    turn the connection test into a read primitive for anything on the network
    that answers."""
    secret = "SUPER-SECRET-INTERNAL-BODY"
    monkeypatch.setattr(cameras.socket, "getaddrinfo",
                        _fake_getaddrinfo({"nas.lan": ["192.168.1.50"]}))
    _Client.resp = _Resp(500, {}, text=secret)
    r = client.post("/setup/test/grocy",
                    json={"grocy_base_url": "http://nas.lan:9283",
                          "grocy_api_key": "k"})
    body = r.json()
    assert body["ok"] is False
    assert secret not in body["error"]
    assert "500" in body["error"]

    _Client.resp = _Resp(500, {}, text=secret)
    r = client.post("/setup/test/mealie",
                    json={"mealie_base_url": "http://nas.lan:9000",
                          "mealie_api_key": "k"})
    assert secret not in r.json()["error"]


def test_first_run_refuses_the_same_addresses(client, monkeypatch):
    monkeypatch.setattr(cameras.socket, "getaddrinfo",
                        _fake_getaddrinfo({"127.0.0.1": ["127.0.0.1"]}))
    r = client.post("/setup/first-run/grocy",
                    json={"base_url": "http://127.0.0.1:8000"})
    assert r.json()["ok"] is False
    assert "not allowed" in r.json()["error"].lower()
