"""The pure internet-vs-LAN rule for forcing a second factor (FoodAssistant-x1ty).

A login is an internet login only when remote access is on and the request's
Host header is the tunnel's public hostname. LAN hosts, .local names, and the
loopback kiosk are never internet.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from app.services.request_origin import (
    is_internet_request, is_internet_origin, is_local_network,
    has_forwarding_headers, client_is_public)

PUBLIC = "https://home.forager.pantryraider.app"


def test_public_host_over_tunnel_is_internet():
    assert is_internet_request("home.forager.pantryraider.app", PUBLIC, True)
    # Port on the Host header is ignored; the hostname is what matters.
    assert is_internet_request("home.forager.pantryraider.app:443", PUBLIC, True)
    # Case-insensitive.
    assert is_internet_request("Home.Forager.PantryRaider.App", PUBLIC, True)


def test_lan_and_local_hosts_are_not_internet():
    assert not is_internet_request("192.168.1.50:9284", PUBLIC, True)
    assert not is_internet_request("pantry.local", PUBLIC, True)
    assert not is_internet_request("kitchen-pi", PUBLIC, True)


def test_tunnel_off_or_no_public_url_is_never_internet():
    # Remote access off: even the public host reads as LAN.
    assert not is_internet_request("home.forager.pantryraider.app", PUBLIC, False)
    # No public URL configured: nothing to match against.
    assert not is_internet_request("home.forager.pantryraider.app", "", True)


def test_loopback_client_is_never_internet():
    # The local kiosk browsing on 127.0.0.1 is never forced, whatever the Host.
    assert not is_internet_request("home.forager.pantryraider.app", PUBLIC, True,
                                   client_host="127.0.0.1")
    assert not is_internet_request("home.forager.pantryraider.app", PUBLIC, True,
                                   client_host="::1")


# --- The broader origin helpers (FoodAssistant-gbs8 / -7svb) -----------------

def test_forwarding_header_detection():
    assert has_forwarding_headers({"x-forwarded-for": "1.2.3.4"})
    assert has_forwarding_headers({"forwarded": "for=1.2.3.4"})
    assert has_forwarding_headers({"cf-connecting-ip": "1.2.3.4"})
    assert not has_forwarding_headers({"host": "pantry.local"})
    assert not has_forwarding_headers({})
    assert not has_forwarding_headers(None)


def test_client_is_public():
    assert client_is_public("8.8.8.8")
    assert not client_is_public("192.168.1.5")
    assert not client_is_public("127.0.0.1")
    assert not client_is_public("not-an-ip")


def test_internet_origin_forces_2fa_behind_any_proxy():
    # A non-Forager reverse proxy (tunnel off): the peer is the proxy's private
    # IP, but a forwarding header is present, so it is treated as internet and a
    # second factor is required (FoodAssistant-7svb).
    assert is_internet_origin("kitchen.example.com", "", False,
                              client_host="10.99.0.1", has_forwarded=True)
    # A public peer address is internet even with no forwarding header.
    assert is_internet_origin("whatever", "", False, client_host="8.8.8.8")
    # The built-in Forager tunnel still counts.
    assert is_internet_origin("home.forager.pantryraider.app", PUBLIC, True,
                              client_host="10.8.0.2")


def test_internet_origin_leaves_direct_lan_single_factor():
    # A directly connected LAN client with no proxy in front is not internet, so
    # a password-only login is still accepted on the LAN.
    assert not is_internet_origin("192.168.1.9:9284", "", False,
                                  client_host="192.168.1.9")
    # The loopback kiosk is never internet.
    assert not is_internet_origin("home.forager.pantryraider.app", PUBLIC, True,
                                  client_host="127.0.0.1")


def test_local_network_true_only_for_direct_lan():
    # Direct LAN peer, no proxy, non-public Host: genuinely local.
    assert is_local_network("192.168.1.9:9284", "", False,
                            client_host="192.168.1.9")
    assert is_local_network("pantry.local", "", False, client_host="10.0.0.4")
    # Loopback (the device itself / local kiosk) is local.
    assert is_local_network("anything", "", False, client_host="127.0.0.1")


def test_local_network_false_behind_proxy_or_tunnel():
    # Reverse proxy in front: the private peer is the proxy, not the LAN client,
    # so pairing / Cub firmware must NOT treat it as local (FoodAssistant-gbs8).
    assert not is_local_network("kitchen.example.com", "", False,
                                client_host="10.99.0.1", has_forwarded=True)
    # Forager tunnel: private WG peer, public Host, tunnel on: not local.
    assert not is_local_network("home.forager.pantryraider.app", PUBLIC, True,
                                client_host="10.8.0.2")
    # A public peer is never local.
    assert not is_local_network("whatever", "", False, client_host="8.8.8.8")
    # An unparseable client host (a proxy/test artifact) is not vouched for.
    assert not is_local_network("whatever", "", False, client_host="testclient")
