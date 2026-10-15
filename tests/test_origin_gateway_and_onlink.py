"""Two edges of "the home network" (services/access.py, services/lan.py).

A reverse proxy on a Docker host in front of a container on a bridge network
reaches the app from Docker's gateway address, which is a private address.
Left on nginx's defaults it sends no forwarding header, so only the same
signs that give it away on loopback (HTTP/1.0, or a browser's Origin or
Referer naming another site while the Host is a local name) can tell it
apart from a program on the Docker host. Those signs now count for the
gateway too.

The networks a device is attached to count as home, so a home whose LAN uses
global IPv6 addresses is recognized. A server with a public address of its
own (a rented server, or one plugged straight into the modem) is attached to
its provider's network, where the neighbours are strangers: there only the
private ranges count.
"""
from __future__ import annotations

import ipaddress

from app.services import access, lan

OWN = frozenset({"kitchen-pi", "kitchen-pi.local"})
GW = frozenset({"172.18.0.1"})


def origin(peer, headers=None, method="GET", host="testserver", *, gateways=GW,
           http_version="1.1", extra=()):
    headers = dict(headers or {})
    headers.setdefault("host", host)
    return access.classify_origin_raw(peer, {k.lower(): v for k, v in headers.items()},
                                      method, host, addon=False, extra_networks=list(extra),
                                      own_names=OWN, http_version=http_version,
                                      gateway_peers=gateways)


# --- The Docker gateway --------------------------------------------------------

def test_nginx_defaults_through_the_gateway_are_proxied():
    assert origin("172.18.0.1", host="127.0.0.1:9284", http_version="1.0") == "proxied"
    assert origin("172.18.0.1", host="localhost:9284", http_version="1.0") == "proxied"


def test_a_foreign_origin_or_referer_through_the_gateway_is_proxied():
    same = {"Sec-Fetch-Site": "same-origin"}
    assert origin("172.18.0.1", {**same, "Origin": "https://pantry.example.com"},
                  method="POST", host="127.0.0.1:9284") == "proxied"
    assert origin("172.18.0.1", {**same, "Referer": "https://pantry.example.com/ui/"},
                  host="127.0.0.1:9284") == "proxied"


def test_a_program_on_the_docker_host_stays_home():
    assert origin("172.18.0.1", host="localhost:9284") == "lan_direct"
    assert origin("172.18.0.1", {"Referer": "http://localhost:9284/ui/"},
                  host="localhost:9284") == "lan_direct"
    # Home Assistant on the host reaching the published port by the server's
    # address: no local Host, so no same-host proxy shape.
    assert origin("172.18.0.1", host="192.168.1.10:9284", http_version="1.0") == "lan_direct"


def test_only_the_gateway_gets_the_check():
    # A device at home is never judged by these signs.
    assert origin("192.168.1.20", host="127.0.0.1:9284", http_version="1.0") == "lan_direct"
    assert origin("172.18.0.1", host="127.0.0.1:9284", http_version="1.0",
                  gateways=frozenset()) == "lan_direct"


def test_classify_origin_passes_the_gateway_only_on_a_docker_bridge(monkeypatch):
    from starlette.requests import Request

    def req(peer, version):
        return Request({"type": "http", "method": "GET", "path": "/ui/",
                        "headers": [(b"host", b"127.0.0.1:9284")], "client": (peer, 1),
                        "query_string": b"", "http_version": version, "scheme": "http",
                        "server": ("testserver", 80), "root_path": ""})

    monkeypatch.delenv("PANTRY_RAIDER_ADDON", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(lan, "onlink_networks", lambda proc_root="/proc": [])
    monkeypatch.setattr(lan, "default_gateways", lambda proc_root="/proc": {"172.18.0.1"})
    monkeypatch.setattr(lan, "in_docker_bridge", lambda *a, **k: False)
    assert access.classify_origin(req("172.18.0.1", "1.0")) == "lan_direct"
    monkeypatch.setattr(lan, "in_docker_bridge", lambda *a, **k: True)
    assert access.classify_origin(req("172.18.0.1", "1.0")) == "proxied"
    assert access.same_host_proxy_signal(req("172.18.0.1", "1.0")) == "http10"
    assert access.same_host_proxy_signal(req("172.18.0.1", "1.1")) == ""
    # In the Home Assistant add-on the gateway is Home Assistant itself.
    monkeypatch.setenv("PANTRY_RAIDER_ADDON", "1")
    assert access.classify_origin(req("172.18.0.1", "1.0")) == "lan_direct"
    assert access.same_host_proxy_signal(req("172.18.0.1", "1.0")) == ""


# --- Networks the device is attached to ------------------------------------------

_HEADER = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"


def _proc(tmp_path, routes, inet6=""):
    net = tmp_path / "net"
    net.mkdir()
    (net / "route").write_text(_HEADER + routes)
    (net / "if_inet6").write_text(inet6)
    lan.clear_cache()
    return str(tmp_path)


def _hex(cidr: str) -> tuple[str, str]:
    n = ipaddress.ip_network(cidr)
    le = lambda a: int.from_bytes(a.packed, "little").to_bytes(4, "big").hex().upper()  # noqa: E731
    return le(n.network_address), le(n.netmask)


def _route(iface, cidr):
    dest, mask = _hex(cidr)
    return f"{iface}\t{dest}\t00000000\t0001\t0\t0\t0\t{mask}\t0\t0\t0\n"


_GUA = "20010db8000100000000000000000005 02 40 00 80 eth0\n"   # 2001:db8:1::5/64


def test_a_home_behind_a_router_keeps_its_global_ipv6_network(tmp_path):
    root = _proc(tmp_path, _route("eth0", "192.168.1.0/24"), _GUA)
    nets = {str(n) for n in lan.onlink_networks(root)}
    assert nets == {"192.168.1.0/24", "2001:db8:1::/64"}
    lan.clear_cache()


def test_a_server_with_a_public_address_trusts_only_private_networks(tmp_path):
    routes = (_route("eth0", "198.51.96.0/20") + _route("docker0", "172.17.0.0/16"))
    root = _proc(tmp_path, routes, _GUA)
    nets = {str(n) for n in lan.onlink_networks(root)}
    assert nets == {"172.17.0.0/16"}
    lan.clear_cache()


def test_carrier_grade_nat_is_not_a_public_address(tmp_path):
    routes = _route("eth0", "192.168.1.0/24") + _route("eth1", "100.64.0.0/10")
    root = _proc(tmp_path, routes, _GUA)
    nets = {str(n) for n in lan.onlink_networks(root)}
    assert nets == {"192.168.1.0/24", "100.64.0.0/10", "2001:db8:1::/64"}
    lan.clear_cache()


def test_a_neighbour_of_a_public_server_is_not_home(tmp_path):
    root = _proc(tmp_path, _route("eth0", "198.51.96.0/20"))
    extra = lan.onlink_networks(root)
    assert extra == []
    assert access.classify_origin_raw("198.51.100.9", {"host": "x"}, "GET", "x", addon=False,
                                      extra_networks=extra) == "public"
    # The owner can still list such a network by hand.
    listed, _bad = lan.parse_cidrs("198.51.96.0/20")
    assert access.classify_origin_raw("198.51.100.9", {"host": "x"}, "GET", "x", addon=False,
                                      extra_networks=listed) == "lan_direct"
    lan.clear_cache()
