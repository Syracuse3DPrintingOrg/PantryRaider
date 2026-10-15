"""The Forager tunnel hub is outside, not the home network (security review, Oct 2026).

Requests through Forager remote access arrive from the hub at 10.99.0.1, inside
10.0.0.0/8, and the hub adds no forwarding header of its own, so they were
classified lan_direct and skipped every outside rule.
"""
import sys
from pathlib import Path

import ipaddress

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import access  # noqa: E402


def _origin(peer, *, extra=(), forager=True):
    return access.classify_origin_raw(
        peer, {"host": "kitchen.example"}, "GET", "kitchen.example", addon=False,
        extra_networks=[ipaddress.ip_network(n) for n in extra],
        forager_tunnel=forager)


def test_the_hub_is_proxied():
    assert _origin("10.99.0.1") == "proxied"
    assert _origin("10.99.4.7") == "proxied"
    assert "proxied" in access.OUTSIDE_ORIGINS


def test_other_private_addresses_stay_home():
    assert _origin("10.1.2.3") == "lan_direct"
    assert _origin("10.100.0.1") == "lan_direct"


def test_home_networks_cannot_widen_it_while_the_tunnel_is_used():
    assert _origin("10.99.0.1", extra=("10.0.0.0/8",)) == "proxied"


def test_a_home_network_on_the_same_range_can_still_be_listed():
    # Without the Forager tunnel, a home network that really uses 10.99.x.x
    # can be named under Home networks.
    assert _origin("10.99.0.20", forager=False) == "proxied"
    assert _origin("10.99.0.20", extra=("10.99.0.0/16",), forager=False) == "lan_direct"
