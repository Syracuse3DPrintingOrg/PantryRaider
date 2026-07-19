"""One egress guard for every server-side fetch that takes a user-supplied URL.

Several endpoints fetch a URL the caller controls: the recipe URL import, the
Home Assistant connection probe, and the camera proxies. Left unguarded, a
crafted URL turns the server into a proxy for its own internal network (SSRF):
loopback (the app's own admin surface and the root host bridge), link-local
(the cloud metadata address 169.254.169.254), and, for the recipe import, any
private LAN host.

A string check on the URL is not enough. A hostname whose DNS record points at
127.0.0.1, a decimal/hex/octal encoding of an address (``2130706433``,
``0x7f000001``, ``0177.0.0.1``), an IPv4-mapped IPv6 literal
(``::ffff:127.0.0.1``), and a 302 redirect to any of those all sail past a
literal check. The only correct guard resolves the name and inspects the actual
address the connection will use, and does so for every hop.

This module does exactly that, in two layers:

  * ``is_safe_public_url`` / ``resolve_guard`` resolve a host once and reject it
    if ANY resolved address is disallowed, so the caller can refuse a bad URL
    up front with a clean message.
  * ``guarded_async_client`` / ``guarded_client`` build an httpx client whose
    network layer re-resolves and re-validates at the moment it opens each TCP
    connection, then connects to that exact validated address. Because the
    resolution and the connect happen together, a short-TTL name cannot answer
    "public" to the pre-check and "loopback" to the connection (DNS rebinding /
    TOCTOU), and every redirect hop opens a fresh connection that is validated
    again. TLS still uses the original hostname for SNI and certificate
    verification, so real HTTPS sites keep working.

Two policies:

  * public-only (``allow_private=False``, the default): only globally routable
    public addresses pass. Used by the recipe import, where no recipe lives on
    a private address.
  * LAN-allowed (``allow_private=True``): private LAN ranges pass, but
    loopback, link-local (metadata), reserved, multicast, and the carrier-grade
    NAT range are still refused. Used by the camera proxies and the Home
    Assistant probe, which legitimately reach devices on the local network.

The pure classification helpers take no network and no clock, so they unit-test
against every known bypass class directly.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

# The user-forward refusal shared by the guarded fetches. Deliberately vague: it
# should read as "that address will not work" without naming what is behind it.
BLOCKED_URL_MESSAGE = (
    "That address points at this device or an internal address, so it cannot "
    "be used. Enter a public web address instead.")

# RFC 6598 carrier-grade NAT shared address space. Python's ``is_private`` does
# not flag it, and no real public site or LAN device lives there, so it is
# refused under both policies.
_CGNAT4 = ipaddress.ip_network("100.64.0.0/10")


class BlockedHostError(Exception):
    """Raised when a server-side fetch targets a disallowed address.

    Carries a user-forward message so a caller can surface it directly. It is
    deliberately NOT an httpx/httpcore exception subclass: httpx passes an
    unknown exception through its error mapping unchanged, so it reaches the
    request site as-is instead of being reshaped into a generic ConnectError.
    """

    def __init__(self, message: str = BLOCKED_URL_MESSAGE):
        super().__init__(message)
        self.user_message = message


def _effective_ip(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    """Unwrap an IPv4 address hidden inside an IPv6 form.

    ``::ffff:127.0.0.1`` (IPv4-mapped) and ``2002:7f00:0001::`` (6to4) both
    embed an IPv4 address that the loopback/private checks would otherwise miss,
    so judge them as their IPv4.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return mapped
    sixtofour = getattr(ip, "sixtofour", None)
    if sixtofour is not None:
        return sixtofour
    return ip


def ip_is_blocked(ip: ipaddress._BaseAddress, *, allow_private: bool) -> bool:
    """Whether a single resolved address must be refused for a server fetch.

    Always blocks loopback, link-local (which includes 169.254.169.254),
    unspecified, multicast, reserved, and the CGNAT range. When
    ``allow_private`` is False it additionally blocks every private / non-global
    address, so only public hosts pass. Pure.
    """
    ip = _effective_ip(ip)
    if (ip.is_loopback or ip.is_link_local or ip.is_unspecified
            or ip.is_multicast or ip.is_reserved):
        return True
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT4:
        return True
    if not allow_private:
        # ``is_global`` is the strongest public test; the ``is_private`` check
        # is belt-and-suspenders for ranges an old stdlib might miss.
        if ip.is_private or not ip.is_global:
            return True
    return False


def _host_of(url_or_host: str) -> str:
    """The bare hostname/IP from a URL or a ``host[:port]`` string, or ""."""
    s = (url_or_host or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "//" + s
    try:
        return (urlsplit(s).hostname or "").strip()
    except ValueError:
        return ""


def _resolve_addrs(host: str) -> list[str]:
    """Every IP ``host`` resolves to (bare IPs included), or raise gaierror."""
    host = (host or "").strip().strip("[]")
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0].split("%", 1)[0] for info in infos if info and info[4]]


def pin_address(host: str, *, allow_private: bool) -> str:
    """Resolve ``host`` and return one validated address to connect to.

    Rejects the host (``BlockedHostError``) when it cannot be resolved, resolves
    to nothing, or ANY resolved address is disallowed, so a name that points
    partly at a blocked address (a rebinding-style trick) is caught. Prefers an
    IPv4 result for connection reliability. Used inside the guarded transport, so
    the address it returns is the exact one the socket connects to.
    """
    host = (host or "").strip().strip("[]")
    if not host:
        raise BlockedHostError()
    try:
        addrs = _resolve_addrs(host)
    except OSError as exc:  # gaierror and friends: unresolvable is unreachable
        raise BlockedHostError(
            "That address could not be resolved, so it cannot be used.") from exc
    if not addrs:
        raise BlockedHostError()
    safe: list[str] = []
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError as exc:
            raise BlockedHostError() from exc
        if ip_is_blocked(ip, allow_private=allow_private):
            raise BlockedHostError()
        safe.append(a)
    for a in safe:  # prefer IPv4
        if ":" not in a:
            return a
    return safe[0]


def is_safe_public_url(url: str, *, allow_private: bool = False) -> bool:
    """Best-effort pre-flight: True when ``url``'s host resolves to only allowed
    addresses under the chosen policy. False on a blocked address OR any
    resolution failure (fail closed), so a caller can refuse up front. The
    guarded client is the real guard; this is for the friendly early error.
    """
    host = _host_of(url)
    if not host:
        return False
    try:
        pin_address(host, allow_private=allow_private)
        return True
    except BlockedHostError:
        return False


# --------------------------------------------------------------------------
# Guarded httpx transports: resolve + validate + pin at connection time.
# --------------------------------------------------------------------------
#
# httpcore calls the network backend's ``connect_tcp(host, port)`` for every new
# connection (including each redirect hop) and, separately, does the TLS
# handshake with the ORIGIN hostname for SNI/certificate verification. So a
# backend that resolves ``host`` itself, validates it, and connects to the
# resolved IP pins the connection to a checked address without breaking TLS: the
# check and the connect share one resolution, closing the rebinding window.


def _guarded_connect_host(host: str, allow_private: bool) -> str:
    return pin_address(host, allow_private=allow_private)


class _GuardedAsyncBackend:
    """Async httpcore network backend that validates and pins every connect."""

    def __init__(self, allow_private: bool):
        import httpcore
        self._allow_private = allow_private
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None,
                          socket_options=None):
        import anyio
        ip = await anyio.to_thread.run_sync(
            _guarded_connect_host, host, self._allow_private)
        return await self._inner.connect_tcp(
            ip, port, timeout=timeout, local_address=local_address,
            socket_options=socket_options)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        # No user-supplied fetch has any business opening a unix socket.
        raise BlockedHostError("Unix-socket connections are not allowed.")

    async def sleep(self, seconds):
        await self._inner.sleep(seconds)


class _GuardedSyncBackend:
    """Sync counterpart of the guarded backend, for sync httpx callers."""

    def __init__(self, allow_private: bool):
        import httpcore
        self._allow_private = allow_private
        self._inner = httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None,
                    socket_options=None):
        ip = _guarded_connect_host(host, self._allow_private)
        return self._inner.connect_tcp(
            ip, port, timeout=timeout, local_address=local_address,
            socket_options=socket_options)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise BlockedHostError("Unix-socket connections are not allowed.")

    def sleep(self, seconds):
        self._inner.sleep(seconds)


def guarded_async_client(*, allow_private: bool = False,
                         **client_kwargs) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` whose connections are SSRF-guarded and pinned.

    Pass ``allow_private=True`` for fetches that legitimately reach LAN devices
    (cameras, Home Assistant). Any other client kwarg (``timeout``,
    ``follow_redirects``, ``verify``, ``headers``) is forwarded. A blocked
    address raises ``BlockedHostError`` from the request call.

    ``trust_env`` defaults to False so an ``HTTP(S)_PROXY`` in the environment
    cannot route a request around the guarded transport (which would let the
    proxy resolve the target and defeat the guard). A caller can pass
    ``trust_env=True`` explicitly if it truly needs env-based proxying.
    """
    client_kwargs.setdefault("trust_env", False)
    transport = httpx.AsyncHTTPTransport(verify=client_kwargs.pop("verify", True))
    transport._pool._network_backend = _GuardedAsyncBackend(allow_private)
    return httpx.AsyncClient(transport=transport, **client_kwargs)


def guarded_client(*, allow_private: bool = False,
                   **client_kwargs) -> httpx.Client:
    """Sync counterpart of ``guarded_async_client``."""
    client_kwargs.setdefault("trust_env", False)
    transport = httpx.HTTPTransport(verify=client_kwargs.pop("verify", True))
    transport._pool._network_backend = _GuardedSyncBackend(allow_private)
    return httpx.Client(transport=transport, **client_kwargs)
