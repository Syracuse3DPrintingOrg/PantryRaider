"""Tell a login coming in over the internet apart from one on the LAN.

Remote access publishes the kitchen at a public web address through the
Forager tunnel. A request whose Host header is that public hostname arrived
from the internet; a request on a LAN IP or a .local name did not, and the
local kiosk on the loopback address never counts. The login flow uses this to
require a second factor for outside logins only (FoodAssistant-x1ty).

Pure and stdlib-only so it unit-tests without a request object or a clock.
"""
from __future__ import annotations

from urllib.parse import urlparse

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _host_only(value: str) -> str:
    """The hostname from a Host header or URL, lowercased, port stripped.

    Handles bare hosts ("kitchen.example.com"), host:port, and full URLs. An
    IPv6 literal in brackets keeps its address; anything unparseable returns ''.
    """
    value = (value or "").strip().lower()
    if not value:
        return ""
    if "://" not in value:
        # Give urlparse a scheme so it splits host:port for us, including the
        # [::1]:9284 bracketed-IPv6 form.
        value = "//" + value
    return (urlparse(value).hostname or "").rstrip(".")


def is_internet_request(host_header: str, public_url: str, tunnel_enabled: bool,
                        client_host: str | None = None) -> bool:
    """Whether a request should be treated as coming from the internet.

    True only when remote access is on (tunnel_enabled) AND the request's Host
    header matches the hostname of the configured public URL. A loopback client
    (the local kiosk) is never internet, and with no tunnel or no public URL
    every request is LAN. Hostnames compare case-insensitively, port ignored.
    """
    if client_host and client_host in _LOOPBACK:
        return False
    if not tunnel_enabled:
        return False
    public_host = _host_only(public_url)
    if not public_host:
        return False
    return _host_only(host_header) == public_host
