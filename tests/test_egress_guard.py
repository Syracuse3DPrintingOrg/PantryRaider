"""The shared SSRF egress guard (FoodAssistant-wa3g/-wrib/-0h8i).

Every server-side fetch of a user-supplied URL routes through services/egress.
These cover the guard's whole job: the pure address policy (every known bypass
class), the resolve-and-reject pre-check, and the pinning transport that
validates the address AT CONNECT TIME so a rebinding name cannot pass the
pre-check and then connect somewhere else.

No real network: DNS resolution is mocked, and the pinning test intercepts the
inner backend's connect so nothing leaves the machine.
"""
from __future__ import annotations

import ipaddress
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import egress  # noqa: E402


def _blk(addr, allow_private=False):
    return egress.ip_is_blocked(ipaddress.ip_address(addr), allow_private=allow_private)


# --- The pure address policy -------------------------------------------------

@pytest.mark.parametrize("addr", [
    "127.0.0.1", "127.5.6.7",          # loopback
    "169.254.169.254", "169.254.0.1",  # link-local + cloud metadata
    "0.0.0.0",                          # unspecified
    "::1",                              # IPv6 loopback
    "fe80::1",                          # IPv6 link-local
    "::ffff:127.0.0.1",                 # IPv4-mapped loopback
    "100.64.0.1", "100.127.255.255",    # CGNAT (RFC 6598)
    "224.0.0.1",                        # multicast
])
def test_always_blocked_under_both_policies(addr):
    assert _blk(addr, allow_private=False) is True
    assert _blk(addr, allow_private=True) is True


@pytest.mark.parametrize("addr", [
    "10.0.0.5", "192.168.1.10", "172.16.4.4",  # RFC 1918
    "fc00::1",                                  # IPv6 ULA
])
def test_private_blocked_only_for_public_policy(addr):
    # Public-only refuses a private address; the LAN-allowed policy permits it
    # (cameras and Home Assistant live on the LAN).
    assert _blk(addr, allow_private=False) is True
    assert _blk(addr, allow_private=True) is False


@pytest.mark.parametrize("addr", ["8.8.8.8", "1.1.1.1", "2600::1"])
def test_public_addresses_pass_both(addr):
    assert _blk(addr, allow_private=False) is False
    assert _blk(addr, allow_private=True) is False


# --- Resolve-and-reject pre-check -------------------------------------------

def _fake_getaddrinfo(mapping):
    def _fake(host, *a, **k):
        ips = mapping.get(host)
        if ips is None:
            raise OSError("name not known")
        return [(2, 1, 6, "", (ip, 0)) for ip in ips]
    return _fake


@pytest.mark.parametrize("host", [
    "2130706433",       # decimal 127.0.0.1
    "0x7f000001",       # hex 127.0.0.1
    "0177.0.0.1",       # octal-ish 127.0.0.1
])
def test_numeric_encodings_of_loopback_are_refused(monkeypatch, host):
    # getaddrinfo resolves each numeric form to the real address, and the guard
    # judges the RESOLVED address, so every encoding of 127.0.0.1 is refused.
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        _fake_getaddrinfo({host: ["127.0.0.1"]}))
    assert egress.is_safe_public_url(f"http://{host}/") is False


def test_ipv4_mapped_ipv6_literal_is_refused(monkeypatch):
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        _fake_getaddrinfo({"::ffff:127.0.0.1": ["::ffff:127.0.0.1"]}))
    assert egress.is_safe_public_url("http://[::ffff:127.0.0.1]:9299/") is False


def test_dns_name_pointing_at_loopback_is_refused(monkeypatch):
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        _fake_getaddrinfo({"evil.example": ["127.0.0.1"]}))
    assert egress.is_safe_public_url("http://evil.example/recipe") is False


def test_any_blocked_resolved_ip_rejects_the_whole_host(monkeypatch):
    # A name that resolves to a good public IP AND loopback (a rebinding trick)
    # is refused: ANY bad address is enough.
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        _fake_getaddrinfo({"mix.example": ["93.184.216.34", "127.0.0.1"]}))
    assert egress.is_safe_public_url("http://mix.example/") is False


def test_private_host_refused_public_but_allowed_lan(monkeypatch):
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        _fake_getaddrinfo({"cam.local": ["192.168.1.50"]}))
    assert egress.is_safe_public_url("http://cam.local/snap.jpg") is False
    assert egress.is_safe_public_url("http://cam.local/snap.jpg", allow_private=True) is True


def test_unresolvable_host_fails_closed(monkeypatch):
    monkeypatch.setattr(egress.socket, "getaddrinfo", _fake_getaddrinfo({}))
    assert egress.is_safe_public_url("http://nope.invalid/") is False
    assert egress.is_safe_public_url("") is False


# --- The pinning transport (connect-time validation) ------------------------

def test_guarded_client_refuses_loopback_at_connect(monkeypatch):
    # Even with no pre-check, the connection itself is guarded: a name that
    # resolves to loopback at connect time raises rather than connecting.
    import asyncio

    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        _fake_getaddrinfo({"rebind.example": ["127.0.0.1"]}))

    async def _go():
        async with egress.guarded_async_client(timeout=3.0) as c:
            with pytest.raises(egress.BlockedHostError):
                await c.get("http://rebind.example/")
    asyncio.run(_go())


def test_guarded_client_connects_to_the_validated_address(monkeypatch):
    # Pinning: the inner backend is handed the RESOLVED, validated IP, not the
    # hostname, so httpx never gets to re-resolve to something else
    # (FoodAssistant-wrib). We resolve a name to a public IP and assert the inner
    # connect target is exactly that IP.
    import asyncio
    import httpcore

    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        _fake_getaddrinfo({"good.example": ["93.184.216.34"]}))
    seen = {}

    async def _fake_connect(self, host, port, **kw):
        seen["host"] = host
        seen["port"] = port
        raise httpcore.ConnectError("stop here; we only care about the target")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", _fake_connect)

    async def _go():
        async with egress.guarded_async_client(timeout=3.0) as c:
            with pytest.raises(Exception):
                await c.get("http://good.example:8080/x")
    asyncio.run(_go())
    assert seen["host"] == "93.184.216.34"  # the pinned IP, not "good.example"
    assert seen["port"] == 8080
