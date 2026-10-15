"""Where a request came from (services/access.py, services/lan.py).

The origin order decides everything else: Home Assistant Ingress, a proxy,
the internet, this device's own programs (plain loopback) or the home
network. These are pure-function tables, plus one check through HTTP for a
same-host nginx on its defaults (HTTP/1.0, Host rewritten, no forwarding
header).
"""
from __future__ import annotations

import ipaddress
import socket
from pathlib import Path

import pytest

from app.services import access, lan

OWN = frozenset({"kitchen-pi", "kitchen-pi.local"})


def origin(peer, headers=None, method="GET", host="testserver", *, addon=False,
           extra=(), own=OWN, http_version="1.1"):
    headers = dict(headers or {})
    headers.setdefault("host", host)
    return access.classify_origin_raw(peer, {k.lower(): v for k, v in headers.items()},
                                      method, host, addon=addon, extra_networks=list(extra),
                                      own_names=own, http_version=http_version)


# --- The origin order ---------------------------------------------------------

@pytest.mark.parametrize("peer,want", [
    ("192.168.1.50", "lan_direct"),
    ("10.1.2.3", "lan_direct"),
    ("172.20.0.5", "lan_direct"),
    ("169.254.10.10", "lan_direct"),
    ("fd12:3456::1", "lan_direct"),
    ("fe80::1", "lan_direct"),
    ("127.0.1.1", "lan_direct"),
    ("100.64.1.1", "public"),          # CGNAT is not home unless listed
    ("192.0.2.10", "public"),          # documentation ranges are the internet
    ("198.51.100.7", "public"),
    ("203.0.113.9", "public"),
    ("2001:db8::1", "public"),
    ("8.8.8.8", "public"),
    ("testclient", "public"),
    (None, "public"),
    ("", "public"),
])
def test_peer_classes(peer, want):
    assert origin(peer) == want


def test_mapped_ipv4_is_unwrapped():
    assert access.unwrap_peer("::ffff:192.168.1.9") == "192.168.1.9"
    assert origin("::ffff:192.168.1.9") == "lan_direct"
    assert origin("::ffff:127.0.0.1", host="localhost:9284") == "plain_loopback"


def test_extra_networks_count_as_home():
    net = ipaddress.ip_network("100.64.0.0/10")
    assert origin("100.64.1.1", extra=[net]) == "lan_direct"


@pytest.mark.parametrize("host", ["localhost:9284", "127.0.0.1:9284", "[::1]:80", "localhost",
                                  "kitchen-pi:9284", "kitchen-pi.local:9284"])
def test_loopback_with_a_local_host_is_plain_loopback(host):
    assert origin("127.0.0.1", host=host) == "plain_loopback"
    assert origin("::1", host=host) == "plain_loopback"


@pytest.mark.parametrize("host", ["evil.example", "pr.local:9284", "testserver",
                                  "pantry.example.com"])
def test_loopback_with_a_foreign_host_is_proxied(host):
    assert origin("127.0.0.1", host=host) == "proxied"


def test_own_name_with_a_forwarding_header_is_proxied():
    assert origin("127.0.0.1", host="kitchen-pi:9284") == "plain_loopback"
    assert origin("127.0.0.1", {"X-Forwarded-For": "203.0.113.9"},
                  host="kitchen-pi:9284") == "proxied"


def test_a_screen_behind_a_same_host_proxy_is_proxied():
    assert origin("127.0.0.1", {"X-Forwarded-For": "127.0.0.1"},
                  host="pantry.example.com") == "proxied"


@pytest.mark.parametrize("name", ["forwarded", "via", "x-real-ip", "cf-connecting-ip",
                                  "true-client-ip", "x-client-ip", "fastly-client-ip",
                                  "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
                                  "X-Forwarded-Anything"])
def test_every_forwarding_header_means_proxied(name):
    assert origin("192.168.1.50", {name: "1"}) == "proxied"
    assert origin("127.0.0.1", {name: "1"}, host="localhost") == "proxied"


def test_cross_site_writes_on_loopback_are_proxied():
    assert origin("127.0.0.1", {"sec-fetch-site": "cross-site"}, method="POST",
                  host="localhost") == "proxied"
    assert origin("127.0.0.1", {"sec-fetch-site": "same-origin"}, method="POST",
                  host="localhost") == "plain_loopback"
    assert origin("127.0.0.1", {}, method="POST", host="localhost") == "plain_loopback"
    # A cross-site navigation (a GET) stays loopback: the kiosk's return from
    # a store page.
    assert origin("127.0.0.1", {"sec-fetch-site": "cross-site"}, method="GET",
                  host="localhost") == "plain_loopback"


def test_ingress_needs_peer_header_and_runtime():
    h = {"X-Ingress-Path": "/api/hassio_ingress/abc", "X-Forwarded-For": "192.168.1.20, 172.30.32.1",
         "X-Forwarded-Host": "homeassistant.local:8123", "X-Hass-Source": "core.ingress"}
    assert origin("172.30.32.2", h, addon=True) == "ingress"
    assert origin("172.30.32.2", h, addon=False) == "proxied"
    assert origin("172.30.32.3", h, addon=True) == "proxied"
    no_path = {k: v for k, v in h.items() if k != "X-Ingress-Path"}
    assert origin("172.30.32.2", no_path, addon=True) == "proxied"


def test_addon_runtime():
    assert access.addon_runtime({"PANTRY_RAIDER_ADDON": "1"})
    assert access.addon_runtime({"SUPERVISOR_TOKEN": "x"})
    assert not access.addon_runtime({"PANTRY_RAIDER_ADDON": "0", "SUPERVISOR_TOKEN": ""})


# --- Same-host proxies on their defaults (round 2) ----------------------------

def test_nginx_defaults_over_http10_are_proxied():
    h = {}
    assert access.same_host_proxy_signal_raw(h, "1.0", OWN) == "http10"
    assert origin("127.0.0.1", h, host="127.0.0.1:9284", http_version="1.0") == "proxied"
    assert origin("127.0.0.1", h, host="127.0.0.1:9284", http_version="1.1") == "plain_loopback"


@pytest.mark.parametrize("headers", [
    {"referer": "https://pantry.example.com/ui/", "sec-fetch-site": "same-origin"},
    {"referer": "https://pantry.example.com/ui/"},
])
def test_a_foreign_referer_from_a_same_origin_page_is_proxied(headers):
    assert access.same_host_proxy_signal_raw(headers, "1.1", OWN) == "referer"
    assert origin("127.0.0.1", headers, host="127.0.0.1:9284") == "proxied"


def test_a_cross_site_referer_stays_loopback():
    h = {"referer": "https://www.amazon.com/", "sec-fetch-site": "cross-site"}
    assert access.same_host_proxy_signal_raw(h, "1.1", OWN) == ""
    assert origin("127.0.0.1", h, host="localhost:9284") == "plain_loopback"


def test_foreign_origin_signals():
    get = {"origin": "https://pantry.example.com", "sec-fetch-site": "same-origin"}
    assert access.same_host_proxy_signal_raw(get, "1.1", OWN) == "origin"
    assert origin("127.0.0.1", get, host="localhost:9284") == "proxied"
    null_post = {"origin": "null"}
    assert access.same_host_proxy_signal_raw(null_post, "1.1", OWN) == "origin"
    assert origin("127.0.0.1", null_post, method="POST", host="localhost:9284") == "proxied"
    probe = {"origin": "https://www.ebay.com", "sec-fetch-site": "cross-site"}
    assert access.same_host_proxy_signal_raw(probe, "1.1", OWN) == ""
    assert origin("127.0.0.1", probe, host="localhost:9284") == "plain_loopback"


def test_local_origin_and_referer_stay_loopback():
    h = {"origin": "http://localhost:9284", "referer": "http://kitchen-pi:9284/ui/"}
    assert access.same_host_proxy_signal_raw(h, "1.1", OWN) == ""
    assert origin("127.0.0.1", h, method="POST", host="localhost:9284") == "plain_loopback"


class _Req:
    def __init__(self, peer, headers, http_version="1.1"):
        self.client = type("C", (), {"host": peer})()
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.scope = {"http_version": http_version}
        self.method = "GET"
        self.state = type("S", (), {})()


def test_signal_wrapper_only_reports_the_rewritten_host_shape(monkeypatch):
    monkeypatch.setattr(access, "own_loopback_names", lambda: OWN)
    probe = _Req("127.0.0.1", {"host": "localhost:9284", "origin": "https://www.ebay.com",
                               "sec-fetch-site": "cross-site"})
    assert access.same_host_proxy_signal(probe) == ""
    rebinding = _Req("127.0.0.1", {"host": "evil.example", "referer": "http://evil.example/"})
    assert access.same_host_proxy_signal(rebinding) == ""
    nginx = _Req("127.0.0.1", {"host": "127.0.0.1:9284"}, http_version="1.0")
    assert access.same_host_proxy_signal(nginx) == "http10"
    lan = _Req("192.168.1.5", {"host": "127.0.0.1:9284"}, http_version="1.0")
    assert access.same_host_proxy_signal(lan) == ""


# --- Names --------------------------------------------------------------------

def test_own_loopback_names(monkeypatch):
    from app.config import settings
    access.clear_name_cache()
    monkeypatch.setattr(socket, "gethostname", lambda: "Kitchen-Pi.")
    monkeypatch.setattr(settings, "device_hostname", "pantry.example.com", raising=False)
    names = access.own_loopback_names()
    assert "kitchen-pi" in names and "kitchen-pi.local" in names
    assert "pantry.example.com" not in names
    assert "localhost" not in names
    access.clear_name_cache()
    monkeypatch.setattr(socket, "gethostname", lambda: "localhost")
    monkeypatch.setattr(settings, "device_hostname", "pr.local", raising=False)
    names = access.own_loopback_names()
    assert names == frozenset({"pr", "pr.local"})
    access.clear_name_cache()


def test_sighting_host():
    proxied = _Req("127.0.0.1", {"host": "<script>", "x-forwarded-for": "203.0.113.9"})
    assert access.sighting_host(proxied) == "a proxy"
    named = _Req("192.168.1.5", {"host": "Pantry.Example.com.:8443"})
    assert access.sighting_host(named) == "pantry.example.com"
    bare = _Req("203.0.113.9", {"host": ""})
    assert access.sighting_host(bare) == "203.0.113.9"
    ip = _Req("203.0.113.9", {"host": "[2001:db8::1]:9284"})
    assert access.sighting_host(ip) == "2001:db8::1"


@pytest.mark.parametrize("host,want", [
    ("", True), ("192.168.1.5:9284", True), ("[fd00::1]:9284", True), ("localhost", True),
    ("kitchen", True), ("pr.local", True), ("pantry.lan", True), ("x.home.arpa", True),
    ("box.internal", True), ("pantry.fritz.box", False), ("evil.example", False),
    ("pantry.example.com", False),
])
def test_is_home_host(host, want):
    assert access.is_home_host(host, device_names=frozenset(), extra_names=[]) is want


def test_is_home_host_listed_and_device_names():
    assert access.is_home_host("pantry.example.com", device_names=frozenset({"pantry.example.com"}),
                               extra_names=[])
    assert access.is_home_host("pantry.fritz.box", device_names=frozenset(),
                               extra_names=["pantry.fritz.box"])


def test_origin_header_ok():
    assert access.origin_header_ok({}, "192.168.1.5:9284")
    assert not access.origin_header_ok({"origin": "null"}, "192.168.1.5:9284")
    assert access.origin_header_ok({"origin": "http://192.168.1.5:9284"}, "192.168.1.5:9284")
    assert not access.origin_header_ok({"origin": "http://evil.example"}, "192.168.1.5:9284")
    assert access.origin_header_ok({"origin": "http://pr.local"}, "pr.local:80")
    assert access.origin_header_ok({"origin": "https://pr.local"}, "pr.local")
    assert not access.origin_header_ok({"origin": "http://pr.local:8080"}, "pr.local")


def test_client_and_caps_parsing():
    assert access.parse_client({"x-pr-client": "streamdeck/1.2"}) == "streamdeck/1.2"
    assert access.parse_client({"x-pr-client": "has space"}) == ""
    assert access.parse_client({"x-pr-client": "x" * 65}) == ""
    caps = access.parse_caps({"x-pr-caps": "Relay1, deckproxy1,bad cap,toolongtoolongtoolong," + ",".join(f"c{i}" for i in range(30))})
    assert "relay1" in caps and "deckproxy1" in caps
    assert "bad cap" not in caps and len(caps) == 16


# --- lan.py -------------------------------------------------------------------

def test_parse_cidrs():
    nets, rejected = lan.parse_cidrs("100.64.0.0/10, 10.0.0.0/4 nonsense\nfd00::/16 2001:db8::/12")
    assert [str(n) for n in nets] == ["100.64.0.0/10", "fd00::/16"]
    assert rejected == ["10.0.0.0/4", "nonsense", "2001:db8::/12"]
    many = " ".join(f"10.{i}.0.0/16" for i in range(40))
    nets, rejected = lan.parse_cidrs(many)
    assert len(nets) == 32 and len(rejected) == 8
    assert lan.parse_cidrs(None) == ([], [])


def test_parse_host_names():
    names, rejected = lan.parse_host_names("Pantry.Example.COM. bad_name -x.example ok.lan")
    assert names == ["pantry.example.com", "ok.lan"]
    assert rejected == ["bad_name", "-x.example"]


def test_onlink_networks_from_proc(tmp_path):
    net = tmp_path / "net"
    net.mkdir()
    (net / "route").write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
        "eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
        "wg0\t0000000A\t00000000\t0001\t0\t0\t0\t000000FF\t0\t0\t0\n"
        "eth1\t00004064\t00000000\t0001\t0\t0\t0\t0000C0FF\t0\t0\t0\n"
        "eth2\t00000000\t00000000\t0001\t0\t0\t0\t00000080\t0\t0\t0\n")
    (net / "if_inet6").write_text(
        "fd001234000000000000000000000001 02 40 00 80 eth0\n"
        "fe800000000000000000000000000001 02 40 20 80 eth0\n"
        "20010db8000000000000000000000001 03 10 00 80 eth0\n")
    lan.clear_cache()
    nets = [str(n) for n in lan.onlink_networks(str(tmp_path))]
    assert "192.168.1.0/24" in nets and "100.64.0.0/10" in nets
    assert "fd00:1234::/64" in nets
    assert not any(n.startswith("10.") for n in nets)      # wg0 skipped
    assert not any(n.startswith("fe80") for n in nets)     # link scope skipped
    assert "2001:db8::/16" not in nets                       # shorter than /32
    assert lan.default_gateways(str(tmp_path)) == {"192.168.1.1"}
    lan.clear_cache()
    assert lan.onlink_networks(str(tmp_path / "missing")) == []


def test_in_docker_bridge(tmp_path):
    net = tmp_path / "net"
    net.mkdir()
    (net / "route").write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\n")
    marker = tmp_path / "dockerenv"
    lan.clear_cache()
    assert not lan.in_docker_bridge(str(tmp_path), str(marker))
    marker.write_text("")
    # Cached like the gateways, so the new marker shows after the cache clears.
    assert not lan.in_docker_bridge(str(tmp_path), str(marker))
    lan.clear_cache()
    assert lan.in_docker_bridge(str(tmp_path), str(marker))
    lan.clear_cache()


def test_recovery_hotspot_is_false_when_the_bind_fails(monkeypatch):
    class _Sock:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def bind(self, addr):
            raise OSError("cannot assign requested address")

    lan.clear_cache()
    monkeypatch.setattr(lan.socket, "socket", _Sock)
    assert lan.recovery_hotspot_up() is False
    lan.clear_cache()


# --- Through HTTP: nginx on its defaults --------------------------------------

def test_http10_loopback_is_treated_as_a_proxy_and_recorded(monkeypatch, tmp_path, own_screen):
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app
    from app.passwords import hash_secret
    from app.services import action_items, auth_state

    monkeypatch.chdir(Path(__file__).resolve().parents[1] / "service")
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("hunter2"), raising=False)
    items = []
    monkeypatch.setattr(access, "raise_item",
                        lambda kind, title, body, dedupe, level="info": items.append((kind, dedupe)))

    async def http10(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "http_version": "1.0"}
        await app(scope, receive, send)

    client = TestClient(http10, client=("127.0.0.1", 50000), base_url="http://127.0.0.1:9284")
    # Settings is refused (no legacy trust for a proxy) and a page asks for
    # the sign-in.
    assert client.get("/setup", follow_redirects=False).status_code == 401
    r = client.get("/ui/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/ui/login")
    assert [k for k, _ in items].count("same_host_proxy") == 1
    client.get("/setup", follow_redirects=False)
    assert [k for k, _ in items].count("same_host_proxy") == 1
    assert auth_state.same_host_proxy_recent()
    # The same request over HTTP/1.1 is the device's own program.
    plain = TestClient(app, client=("127.0.0.1", 50000), base_url="http://127.0.0.1:9284")
    assert plain.get("/setup", follow_redirects=False).status_code == 200
    del action_items
