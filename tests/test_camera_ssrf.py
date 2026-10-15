"""Camera fetch proxies must not be pointed at the server itself or internal
addresses (SSRF, FoodAssistant-e9al).

The preview/snapshot proxies fetch a URL server-side and hand back the body. An
authenticated user (including a reduced-privilege viewer) could otherwise set
the URL to loopback (fetched as a trusted local admin) or the cloud metadata
address. These cover the pure block rule, the admin-only gate on the
arbitrary-URL preview, and the refusal on the preview and snapshot fetches,
while confirming ordinary LAN and public camera addresses still go through.

No network: DNS resolution and the camera HTTP fetch are both mocked.
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
from app.services import cameras  # noqa: E402


# --- The pure block rule -----------------------------------------------------

def _fake_getaddrinfo(mapping):
    """A getaddrinfo stand-in that maps a host string to one or more IPs."""
    def _fake(host, port, *a, **k):
        ips = mapping.get(host)
        if ips is None:
            raise OSError("name not known")
        return [(2, 1, 6, "", (ip, 0)) for ip in ips]
    return _fake


@pytest.mark.parametrize("host", [
    "127.0.0.1", "localhost", "::1", "169.254.169.254", "0.0.0.0",
])
def test_blocks_loopback_linklocal_and_unspecified(monkeypatch, host):
    monkeypatch.setattr(cameras.socket, "getaddrinfo", _fake_getaddrinfo({
        "127.0.0.1": ["127.0.0.1"],
        "localhost": ["127.0.0.1"],
        "::1": ["::1"],
        "169.254.169.254": ["169.254.169.254"],
        "0.0.0.0": ["0.0.0.0"],
    }))
    assert cameras.is_blocked_fetch_host(host) is True


@pytest.mark.parametrize("addr", [
    "192.168.1.50", "10.0.0.5", "172.16.4.4", "203.0.113.10",
])
def test_allows_private_lan_and_public(monkeypatch, addr):
    monkeypatch.setattr(cameras.socket, "getaddrinfo", _fake_getaddrinfo({
        addr: [addr],
    }))
    # RFC1918 stays allowed because real cameras live on the LAN; a normal
    # public host is allowed too.
    assert cameras.is_blocked_fetch_host(addr) is False


def test_blocks_from_full_url_and_host_port(monkeypatch):
    monkeypatch.setattr(cameras.socket, "getaddrinfo", _fake_getaddrinfo({
        "127.0.0.1": ["127.0.0.1"],
    }))
    assert cameras.is_blocked_fetch_host("http://127.0.0.1:9284/x") is True
    assert cameras.is_blocked_fetch_host("127.0.0.1:8080") is True


def test_blocks_when_any_resolved_ip_is_disallowed(monkeypatch):
    # A name that resolves to a good LAN IP and also to loopback (a rebinding
    # style trick) is blocked because ANY bad address is enough.
    monkeypatch.setattr(cameras.socket, "getaddrinfo", _fake_getaddrinfo({
        "sneaky.cam": ["192.168.1.9", "127.0.0.1"],
    }))
    assert cameras.is_blocked_fetch_host("http://sneaky.cam/snap") is True


def test_fails_closed_on_resolution_error(monkeypatch):
    monkeypatch.setattr(cameras.socket, "getaddrinfo", _fake_getaddrinfo({}))
    assert cameras.is_blocked_fetch_host("http://nope.invalid/x") is True
    assert cameras.is_blocked_fetch_host("") is True


# --- The routes --------------------------------------------------------------

class _FakeResp:
    def __init__(self, status=200, content=b"\xff\xd8jpeg", ctype="image/jpeg"):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": ctype}


class _FakeClient:
    """Stand-in for httpx.AsyncClient that returns a canned image, so a route
    that is NOT supposed to reach the network still answers without one."""
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, **k):
        return _FakeResp()


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    # A configured, password-protected server so the auth middleware is live and
    # the viewer role can be exercised.
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("admin-pw"), raising=False)
    monkeypatch.setattr(settings, "viewer_password", hash_secret("viewer-pw"), raising=False)
    monkeypatch.setattr(settings, "tunnel_enabled", False, raising=False)
    monkeypatch.setattr(settings, "qr_public_url", "", raising=False)
    # No local 2FA, so a LAN password login completes in one step.
    monkeypatch.setattr(settings, "local_totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _login(client, password):
    r = client.post("/ui/login", data={"password": password},
                    follow_redirects=False)
    assert r.status_code in (302, 303, 307), r.text


def test_preview_blocks_loopback_snapshot_url(client, monkeypatch):
    monkeypatch.setattr(cameras.socket, "getaddrinfo", _fake_getaddrinfo({
        "127.0.0.1": ["127.0.0.1"],
    }))
    _login(client, "admin-pw")
    r = client.get("/ui/camera/preview",
                   params={"snapshot_url": "http://127.0.0.1:9284/health"})
    assert r.status_code == 400
    assert "internal" in r.json()["detail"].lower()


def test_preview_blocks_cloud_metadata_snapshot_url(client, monkeypatch):
    monkeypatch.setattr(cameras.socket, "getaddrinfo", _fake_getaddrinfo({
        "169.254.169.254": ["169.254.169.254"],
    }))
    _login(client, "admin-pw")
    r = client.get("/ui/camera/preview",
                   params={"snapshot_url": "http://169.254.169.254/latest/meta-data/"})
    assert r.status_code == 400


def test_preview_is_admin_only(client, monkeypatch):
    # A viewer session cannot use the arbitrary-URL preview: it is a setup action.
    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    monkeypatch.setattr(cameras.socket, "getaddrinfo", _fake_getaddrinfo({
        "192.168.1.50": ["192.168.1.50"],
    }))
    _login(client, "viewer-pw")
    r = client.get("/ui/camera/preview",
                   params={"snapshot_url": "http://192.168.1.50/snap.jpg"})
    assert r.status_code == 403


def test_admin_can_preview_lan_camera(client, monkeypatch):
    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    monkeypatch.setattr(cameras.socket, "getaddrinfo", _fake_getaddrinfo({
        "192.168.1.50": ["192.168.1.50"],
    }))
    _login(client, "admin-pw")
    r = client.get("/ui/camera/preview",
                   params={"snapshot_url": "http://192.168.1.50/snap.jpg"})
    assert r.status_code == 200
    assert r.content == b"\xff\xd8jpeg"


def test_snapshot_blocks_manual_camera_pointed_at_loopback(client, monkeypatch):
    # The real guarded client runs here on purpose. The refusal comes from the
    # transport, which validates the address it is about to connect to, so a
    # fake client in its place would test nothing: there is no separate
    # pre-flight lookup on this path any more (it resolved DNS on the event
    # loop every 1.5 seconds and could refuse nothing the connect does not).
    # A manual camera (no HA, no Reolink) whose snapshot URL is loopback.
    monkeypatch.setattr(settings, "streamdeck_cameras",
                        [{"name": "evil", "snapshot_url": "http://127.0.0.1:9284/health"}],
                        raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    _login(client, "viewer-pw")
    r = client.get("/ui/camera/0/snapshot")
    assert r.status_code == 400


def test_snapshot_allows_manual_lan_camera(client, monkeypatch):
    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    monkeypatch.setattr(cameras.socket, "getaddrinfo", _fake_getaddrinfo({
        "192.168.1.77": ["192.168.1.77"],
    }))
    monkeypatch.setattr(settings, "streamdeck_cameras",
                        [{"name": "kitchen", "snapshot_url": "http://192.168.1.77/snap.jpg"}],
                        raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    _login(client, "viewer-pw")
    r = client.get("/ui/camera/0/snapshot")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8jpeg"


def test_snapshot_allows_ha_camera_on_lan(client, monkeypatch):
    # An HA camera composed from an admin-entered LAN base URL still works.
    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    monkeypatch.setattr(cameras.socket, "getaddrinfo", _fake_getaddrinfo({
        "ha.local": ["192.168.1.10"],
    }))
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "http://ha.local:8123", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "tok", raising=False)
    monkeypatch.setattr(settings, "streamdeck_cameras",
                        [{"name": "front", "ha_entity": "camera.front_door"}],
                        raising=False)
    _login(client, "viewer-pw")
    r = client.get("/ui/camera/0/snapshot")
    assert r.status_code == 200


# --- No DNS on the event loop (launch audit, Sep 2026) -----------------------

def test_snapshot_fetch_does_no_lookup_on_the_event_loop(client, monkeypatch):
    """The kiosk refetches a snapshot every 1.5 seconds. The blocking lookup
    that used to run first is gone: the guarded transport is the guard, and it
    resolves in a worker thread."""
    def _boom(*a, **kw):
        raise AssertionError("blocking lookup ran on the event loop")

    monkeypatch.setattr(cameras, "is_blocked_fetch_host", _boom)
    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "http://ha.local:8123",
                        raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "tok", raising=False)
    monkeypatch.setattr(settings, "streamdeck_cameras",
                        [{"name": "front", "ha_entity": "camera.front_door"}],
                        raising=False)
    _login(client, "viewer-pw")
    assert client.get("/ui/camera/0/snapshot").status_code == 200


def test_reolink_snapshot_still_refuses_loopback(client, monkeypatch):
    """The early check gives a clear refusal; the guarded client behind it is
    tested with a rebinding name below."""
    monkeypatch.setattr(cameras.socket, "getaddrinfo", _fake_getaddrinfo({
        "127.0.0.1": ["127.0.0.1"],
    }))
    monkeypatch.setattr(settings, "streamdeck_cameras",
                        [{"name": "door", "source": "reolink", "host": "127.0.0.1",
                          "username": "admin", "password": "x"}],
                        raising=False)
    _login(client, "viewer-pw")
    r = client.get("/ui/camera/0/snapshot")
    assert r.status_code == 400
    assert "internal" in r.json()["detail"].lower()


def _rebinding_getaddrinfo(first: str, then: str):
    """Answers ``first`` to the first lookup and ``then`` to every later one,
    the way a DNS rebinding server does."""
    calls = {"n": 0}

    def _fake(host, port, *a, **k):
        calls["n"] += 1
        ip = first if calls["n"] == 1 else then
        return [(2, 1, 6, "", (ip, 0))]
    return _fake, calls


@pytest.mark.parametrize("target", ["127.0.0.1", "169.254.169.254", "::1"])
def test_reolink_fetch_connects_only_to_an_address_it_checked(monkeypatch, target):
    """The pre-check passes (the name answered a LAN address), but the fetch
    resolves again as it connects and is refused there, before any request
    reaches the device itself, the host bridge or the metadata address."""
    import asyncio

    from app.services import egress

    fake, calls = _rebinding_getaddrinfo("192.168.1.20", target)
    monkeypatch.setattr(cameras.socket, "getaddrinfo", fake)
    entry = {"source": "reolink", "host": "cam.example.test", "username": "a",
             "password": "b"}
    assert cameras.is_blocked_fetch_host("cam.example.test", fail_closed=False) is False
    cameras._reolink_tokens.clear()
    with pytest.raises(egress.BlockedHostError):
        asyncio.run(cameras.fetch_reolink_snapshot(entry, timeout=2.0))
    assert calls["n"] >= 2


def test_reolink_route_reports_a_rebound_address_as_blocked(client, monkeypatch):
    fake, _calls = _rebinding_getaddrinfo("192.168.1.20", "127.0.0.1")
    monkeypatch.setattr(cameras.socket, "getaddrinfo", fake)
    monkeypatch.setattr(settings, "streamdeck_cameras",
                        [{"name": "door", "source": "reolink", "host": "cam.example.test",
                          "username": "admin", "password": "x"}],
                        raising=False)
    cameras._reolink_tokens.clear()
    _login(client, "viewer-pw")
    r = client.get("/ui/camera/0/snapshot")
    assert r.status_code == 400
    assert r.json()["detail"] == cameras.BLOCKED_HOST_MESSAGE


@pytest.mark.parametrize("target", ["127.0.0.1", "169.254.169.254", "0.0.0.0"])
def test_frigate_discover_refuses_internal_addresses(monkeypatch, target):
    from app.services import frigate

    monkeypatch.setattr(cameras.socket, "getaddrinfo", _fake_getaddrinfo({
        "frigate.example.test": [target], target: [target]}))
    for base in ("http://frigate.example.test:5000", f"http://{target}:9299"):
        out = frigate.discover(base)
        assert out["ok"] is False
        assert "internal address" in out["error"], out


def test_frigate_discover_lets_a_lan_address_through_the_guard(monkeypatch):
    """A LAN Frigate passes the guard; here nothing listens, so the result is
    the ordinary could-not-reach message, not a refusal."""
    from app.services import egress, frigate

    seen = []

    def _connect(host, allow_private):
        seen.append(egress.pin_address(host, allow_private=allow_private))
        raise OSError("connection refused")

    monkeypatch.setattr(cameras.socket, "getaddrinfo", _fake_getaddrinfo({
        "frigate.example.test": ["192.168.1.30"]}))
    monkeypatch.setattr(egress, "_guarded_connect_host", _connect)
    out = frigate.discover("http://frigate.example.test:5000")
    assert out["ok"] is False and "Could not reach Frigate" in out["error"]
    assert seen == ["192.168.1.30"]
