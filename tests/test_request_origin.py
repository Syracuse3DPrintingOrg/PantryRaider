"""The pure internet-vs-LAN rule for forcing a second factor (FoodAssistant-x1ty).

A login is an internet login only when remote access is on and the request's
Host header is the tunnel's public hostname. LAN hosts, .local names, and the
loopback kiosk are never internet.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from app.services.request_origin import is_internet_request

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
