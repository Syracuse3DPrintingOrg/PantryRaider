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
    public Host, so a proxied/tunneled request can no longer masquerade as
    LAN-local.

Since 0.21 the sign-in page, the setup code and the pairing gate decide with
services/access.py (access.sign_in_from_outside and pairing's
is_local_network_request), which also count the Home networks setting and the
networks this device is attached to; they still use ``is_internet_request``
for the remote-access address and ``client_is_public`` for a peer on a
public address. The pure helpers here keep the address rules on their own.

Pure and stdlib-only so it unit-tests without a request object or a clock.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

# Headers a reverse proxy / CDN adds in front of the app. Their mere presence
# means the immediate peer (request.client.host) is a proxy, not the real
# client, so the request cannot be trusted as LAN-local no matter how private
# that peer's address looks. The same list as services/access.py: any
# x-forwarded-* header counts too.
_FORWARDING_HEADERS = (
    "forwarded", "via", "x-real-ip", "cf-connecting-ip", "true-client-ip",
    "x-client-ip", "fastly-client-ip",
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
        names = [str(k).lower() for k in headers.keys()]
    except (TypeError, AttributeError):
        return False
    return any(n in _FORWARDING_HEADERS or n.startswith("x-forwarded-") for n in names)


# Home address ranges, listed explicitly. Never ipaddress.is_private: on
# Python 3.12 it calls the documentation ranges (192.0.2.0/24, 198.51.100.0/24,
# 203.0.113.0/24, 2001:db8::/32) private, and those must count as the internet.
_PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
))
_LINK_LOCAL_NETS = tuple(ipaddress.ip_network(n) for n in ("169.254.0.0/16", "fe80::/10"))
# The shared address space (RFC 6598) that Tailscale and carrier-grade NAT
# use. It is not the open internet: a password alone signed in from it before
# 0.21 and still does (services/access.sign_in_from_outside).
_SHARED_NETS = (ipaddress.ip_network("100.64.0.0/10"),)


def _ip(client_host: str | None):
    try:
        ip = ipaddress.ip_address((client_host or "").strip())
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip


def _in(ip, nets) -> bool:
    return any(ip.version == n.version and ip in n for n in nets)


def _is_private_client(client_host: str | None) -> bool:
    """True when the immediate peer address is private (RFC 1918 / ULA)."""
    ip = _ip(client_host)
    return ip is not None and _in(ip, _PRIVATE_NETS)


def client_is_public(client_host: str | None) -> bool:
    """True when the immediate peer is an address on the open internet: not
    loopback, private, link-local or the shared 100.64.0.0/10 range."""
    ip = _ip(client_host)
    if ip is None:
        return False
    return not (ip.is_loopback or _in(ip, _PRIVATE_NETS) or _in(ip, _LINK_LOCAL_NETS)
                or _in(ip, _SHARED_NETS) or ip.is_unspecified or ip.is_multicast)


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
                        client_host: str | None = None,
                        has_forwarded: bool = False) -> bool:
    """Whether a request should be treated as coming from the internet.

    True only when remote access is on (tunnel_enabled) AND the request's Host
    header matches the hostname of the configured public URL. A loopback client
    (the local kiosk) is never internet, and with no tunnel or no public URL
    every request is LAN. Hostnames compare case-insensitively, port ignored.
    A loopback peer that carries a forwarding header is a reverse proxy on
    this machine, not the kiosk, so it gets no shortcut.
    """
    if client_host and client_host in _LOOPBACK and not has_forwarded:
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
    The loopback kiosk is never internet; a loopback peer with a forwarding
    header is a proxy on this machine and gets no shortcut. Pure.
    """
    if client_host and client_host in _LOOPBACK and not has_forwarded:
        return False
    if is_internet_request(host_header, public_url, tunnel_enabled, client_host,
                           has_forwarded):
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
    if client_host and client_host in _LOOPBACK and not has_forwarded:
        return True
    if not _is_private_client(client_host):
        return False
    if has_forwarded:
        return False
    if is_internet_request(host_header, public_url, tunnel_enabled, client_host,
                           has_forwarded):
        return False
    return True
