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


# --- Recipe image downloads (AI-4) -------------------------------------------
#
# A recipe's image address comes from the page being imported (its JSON-LD) or
# from a Forager card or share, so recipe_store.fetch_image must treat it as
# untrusted: public addresses only, every redirect hop re-checked, images only,
# and a size cap. Only the user's own Mealie is a trusted origin. These run
# against real listeners on loopback; DNS for the "public" names is mocked and
# their connections are steered to a loopback listener, so nothing leaves the
# machine.

import asyncio  # noqa: E402
import http.server  # noqa: E402
import threading  # noqa: E402

from app.services import recipe_store  # noqa: E402

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class _Listener:
    """A tiny HTTP server on 127.0.0.1 that records every request path and
    answers from a {path: (status, headers, body)} table."""

    def __init__(self, routes: dict):
        self.hits: list[str] = []
        routes_ref, hits = routes, self.hits

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                hits.append(self.path)
                status, headers, body = routes_ref.get(
                    self.path, (404, {"Content-Type": "text/plain"}, b"nope"))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def no_proxy_env(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def listener(no_proxy_env):
    made: list[_Listener] = []

    def _make(routes):
        srv = _Listener(routes)
        made.append(srv)
        return srv
    yield _make
    for srv in made:
        srv.close()


def _steer_public_host_to(monkeypatch, host: str, public_ip: str, port: int):
    """Resolve ``host`` to a public IP for the guard, then deliver the guarded
    connection to the loopback listener on ``port``, as if that public server
    had answered. Loopback itself resolves to loopback, so a redirect there is
    judged on the real address."""
    import httpcore

    monkeypatch.setattr(egress.socket, "getaddrinfo", _fake_getaddrinfo({
        host: [public_ip], "127.0.0.1": ["127.0.0.1"]}))
    real_connect = httpcore.AnyIOBackend.connect_tcp

    async def _connect(self, ip, p, **kw):
        # The guard hands over the pinned IP; an unguarded client would hand
        # over the name itself. Both reach the "public" listener.
        if ip in (public_ip, host):
            ip, p = "127.0.0.1", port
        return await real_connect(self, ip, p, **kw)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", _connect)


def test_untrusted_loopback_image_is_refused_without_a_request(listener):
    srv = listener({"/img.png": (200, {"Content-Type": "image/png"}, _PNG)})
    assert asyncio.run(recipe_store.fetch_image(srv.url("/img.png"))) is None
    assert srv.hits == []


def test_untrusted_redirect_to_loopback_is_refused(listener, monkeypatch):
    # The page's image lives on a "public" host that redirects to the host
    # bridge address. The redirect hop is re-checked and never connects.
    bridge = listener({"/restore": (200, {"Content-Type": "image/png"}, _PNG)})
    public = listener({
        "/photo.png": (302, {"Location": bridge.url("/restore")}, b""),
        "/ok.png": (200, {"Content-Type": "image/png"}, _PNG),
    })
    _steer_public_host_to(monkeypatch, "recipes.example", "93.184.216.34", public.port)

    got = asyncio.run(recipe_store.fetch_image("http://recipes.example/photo.png"))
    assert got is None
    assert public.hits == ["/photo.png"]
    assert bridge.hits == []

    # Control: the same steering does deliver a plain public image, so the
    # refusal above is the guard at work and not a broken harness.
    ok = asyncio.run(recipe_store.fetch_image("http://recipes.example/ok.png"))
    assert ok == (_PNG, "image/png")


def test_trusted_mealie_origin_on_loopback_is_fetched(listener):
    # The user's own Mealie is usually LAN, Docker-network, or localhost.
    srv = listener({"/api/media/recipes/r1/images/original.webp":
                    (200, {"Content-Type": "image/webp"}, _PNG)})
    got = asyncio.run(recipe_store.fetch_image(
        srv.url("/api/media/recipes/r1/images/original.webp"),
        headers={"Authorization": "Bearer t"}, trusted_origin=True))
    assert got == (_PNG, "image/webp")


def test_non_image_content_type_is_refused(listener):
    srv = listener({"/page": (200, {"Content-Type": "text/html"}, b"<html>hi</html>"),
                    "/json": (200, {"Content-Type": "application/json"}, b"{}")})
    assert asyncio.run(recipe_store.fetch_image(srv.url("/page"), trusted_origin=True)) is None
    assert asyncio.run(recipe_store.fetch_image(srv.url("/json"), trusted_origin=True)) is None


def test_oversized_image_is_refused(listener, monkeypatch):
    monkeypatch.setattr(recipe_store, "MAX_IMAGE_BYTES", 1024)
    big = b"\x00" * 4096
    srv = listener({
        # Declared too large up front.
        "/declared.png": (200, {"Content-Type": "image/png",
                                "Content-Length": str(len(big))}, big),
        # No length header: the streamed byte count trips the cap.
        "/streamed.png": (200, {"Content-Type": "image/png"}, big),
        "/small.png": (200, {"Content-Type": "image/png"}, b"\x89PNG" + b"\x00" * 100),
    })
    assert asyncio.run(recipe_store.fetch_image(srv.url("/declared.png"),
                                                trusted_origin=True)) is None
    assert asyncio.run(recipe_store.fetch_image(srv.url("/streamed.png"),
                                                trusted_origin=True)) is None
    assert asyncio.run(recipe_store.fetch_image(srv.url("/small.png"),
                                                trusted_origin=True)) is not None
