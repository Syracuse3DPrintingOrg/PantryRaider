"""Tell a login coming in over the internet apart from one on the LAN.

Remote access publishes the kitchen at a public web address through the
Forager tunnel. A request whose Host header is that public hostname arrived
from the internet; a request on a LAN IP or a .local name did not, and the
local kiosk on the loopback address never counts. The login flow uses this to
require a second factor for outside logins only (FoodAssistant-x1ty).

The Forager-tunnel Host match is not the whole story, though. When the app
sits behind ANY reverse proxy or tunnel (Pangolin, Cloudflare, an L3 tunnel),
``request.client.host`` is the proxy's own private address, so a naive
private-IP check reads a public request as "on the LAN". That once exposed the
unauthenticated pairing and Cub-firmware endpoints to the open internet
(FoodAssistant-gbs8) and let a bare password through as a single factor over a
non-Forager proxy (FoodAssistant-7svb). So two broader helpers here decide
origin from more than the immediate peer:

  * ``is_internet_origin`` treats a request as internet when the Forager Host
    matches, OR a proxy sits in front (forwarding headers present), OR the peer
    is a public address. The login flow forces a second factor for all of
    these, not just the built-in tunnel.
  * ``is_local_network`` returns True ONLY for a genuine LAN peer: a private (or
    loopback) client address, no forwarding headers, and not the tunnel's
    public Host. Pairing and the Cub firmware endpoints gate on this, so a
    proxied/tunneled request can no longer masquerade as LAN-local.

Pure and stdlib-only so it unit-tests without a request object or a clock.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

# Headers a reverse proxy / CDN adds in front of the app. Their mere presence
# means the immediate peer (request.client.host) is a proxy, not the real
# client, so the request cannot be trusted as LAN-local no matter how private
# that peer's address looks.
_FORWARDING_HEADERS = (
    "x-forwarded-for", "forwarded", "x-real-ip",
    "cf-connecting-ip", "true-client-ip", "x-forwarded-host",
)


def has_forwarding_headers(headers) -> bool:
    """True when a request carries any reverse-proxy / CDN forwarding header.

    ``headers`` is anything with case-insensitive ``in`` (a Starlette Headers,
    or a plain dict of lowercased keys). Used to detect that a proxy sits in
    front, so the private-looking peer address must not be trusted as LAN.
    """
    if headers is None:
        return False
    try:
        return any(h in headers for h in _FORWARDING_HEADERS)
    except TypeError:
        return False


def _is_private_client(client_host: str | None) -> bool:
    """True when the immediate peer address is private (RFC 1918 / ULA)."""
    try:
        ip = ipaddress.ip_address((client_host or "").strip())
    except ValueError:
        return False
    return ip.is_private and not ip.is_link_local


def client_is_public(client_host: str | None) -> bool:
    """True when the immediate peer is a globally routable public address."""
    try:
        ip = ipaddress.ip_address((client_host or "").strip())
    except ValueError:
        return False
    return ip.is_global


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


def is_internet_origin(host_header: str, public_url: str, tunnel_enabled: bool,
                       client_host: str | None = None,
                       has_forwarded: bool = False) -> bool:
    """Whether a login should be forced to a second factor as an internet login.

    Broader than ``is_internet_request``: a request is internet when the built-in
    Forager tunnel Host matches (the original rule), OR a reverse proxy sits in
    front (``has_forwarded``), OR the peer is a public address. This closes the
    single-factor-over-a-non-Forager-proxy gap (FoodAssistant-7svb): app-auth
    behind Pangolin/Cloudflare now demands a second factor like the tunnel does.
    The loopback kiosk is never internet. Pure.
    """
    if client_host and client_host in _LOOPBACK:
        return False
    if is_internet_request(host_header, public_url, tunnel_enabled, client_host):
        return True
    if has_forwarded:
        return True
    return client_is_public(client_host)


def is_local_network(host_header: str, public_url: str, tunnel_enabled: bool,
                     client_host: str | None = None,
                     has_forwarded: bool = False) -> bool:
    """Whether a request genuinely originates on the local network.

    True only when the immediate peer is loopback or a private LAN address, no
    reverse-proxy forwarding header is present, and the request did not arrive
    over the tunnel's public Host. Everything a proxy or tunnel could disguise
    is refused, so the unauthenticated pairing and Cub-firmware endpoints can no
    longer be reached from the internet through a proxy (FoodAssistant-gbs8).
    Pure.
    """
    if client_host and client_host in _LOOPBACK:
        return True
    if not _is_private_client(client_host):
        return False
    if has_forwarded:
        return False
    if is_internet_request(host_header, public_url, tunnel_enabled, client_host):
        return False
    return True
