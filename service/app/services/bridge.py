"""Shared-token handshake with the host bridge (FoodAssistant-pxcm).

The host bridge (scripts/image-build/foodassistant-host-bridge, root daemon on
127.0.0.1:9299) writes a random token to <data_dir>/bridge-token at startup.
On a pi_hosted appliance the bridge writes to INSTALL_DIR/data, which is the
container's /app/data bind mount; on a pi_remote venv install the app's
DATA_DIR points at the same INSTALL_DIR/data on the host filesystem. Either
way the app reads the token from settings.data_dir and sends it back on every
bridge call as the X-Bridge-Token header.

The token is cached after the first successful read. A missing file is never
cached (first boot: the app can come up before the bridge has written it), and
a 401 from the bridge invalidates the cache so the next call re-reads a
rotated token from disk.
"""
from __future__ import annotations

import asyncio
import weakref
from pathlib import Path

import httpx

from ..config import settings

TOKEN_FILENAME = "bridge-token"
BRIDGE_TOKEN_HEADER = "X-Bridge-Token"

_cached_token: str = ""


def bridge_token_path() -> Path:
    """Where the bridge drops the shared token (under the app data dir)."""
    return Path(settings.data_dir) / TOKEN_FILENAME


def bridge_token() -> str:
    """The shared bridge token, cached after the first successful read.

    Returns '' when the file is not there yet (first boot, non-Pi install);
    that miss is deliberately not cached so the token is picked up as soon as
    the bridge writes it.
    """
    global _cached_token
    if _cached_token:
        return _cached_token
    try:
        token = bridge_token_path().read_text().strip()
    except OSError:
        return ""
    if token:
        _cached_token = token
    return token


def read_bridge_token_fresh() -> str:
    """Read the token from disk now, skipping the cache.

    A token found this way replaces the cached one, so a token the bridge
    rotated (or a restore replaced) is picked up without waiting for a 401.
    Returns '' when the file is missing or empty; the cache is then left as
    it was.
    """
    global _cached_token
    try:
        token = bridge_token_path().read_text().strip()
    except OSError:
        return ""
    if token:
        _cached_token = token
    return token


def invalidate_bridge_token() -> None:
    """Drop the cached token so the next call re-reads it from disk."""
    global _cached_token
    _cached_token = ""


def bridge_headers() -> dict:
    """Headers to attach to a bridge request: the token when we have one."""
    token = bridge_token()
    return {BRIDGE_TOKEN_HEADER: token} if token else {}


async def _drop_token_on_401(response: httpx.Response) -> None:
    """Response hook: a 401 means our token is stale (or missing), so re-read
    the file on the next call. The caller's normal retry path then succeeds."""
    if response.status_code == 401:
        invalidate_bridge_token()


# One pooled client per event loop (FoodAssistant-e6k25). The bridge is on
# loopback and speaks HTTP/1.1 keep-alive, so reusing a connection saves a
# TCP handshake (and a bridge handler thread) on every call. Idle connections
# are let go after a few seconds, well inside the bridge's own 30 second idle
# timeout, so the app never sends on a socket the bridge is about to close.
_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=8,
                       keepalive_expiry=5.0)
_DEFAULT_TIMEOUT = 5.0

_shared: httpx.AsyncClient | None = None
_shared_loop: "weakref.ref[asyncio.AbstractEventLoop] | None" = None


def _shared_client() -> httpx.AsyncClient:
    """The pooled bridge client for the running event loop, built on first
    use and rebuilt when the loop changed (tests, a --reload restart) or the
    client was closed. A client from another loop is dropped, not closed: its
    sockets belong to that loop and cannot be awaited from this one."""
    global _shared, _shared_loop
    loop = asyncio.get_running_loop()
    if (_shared is None or _shared.is_closed or _shared_loop is None
            or _shared_loop() is not loop):
        from .tls import verify
        _shared = httpx.AsyncClient(
            verify=verify(True),
            limits=_LIMITS,
            timeout=_DEFAULT_TIMEOUT,
            event_hooks={"response": [_drop_token_on_401]},
        )
        _shared_loop = weakref.ref(loop)
    return _shared


async def aclose_bridge_client() -> None:
    """Close the pooled client (app shutdown). Safe to call when none was
    built, or when it belongs to a loop that has already gone away."""
    global _shared, _shared_loop
    client, loop_ref = _shared, _shared_loop
    _shared, _shared_loop = None, None
    if client is None or client.is_closed:
        return
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if loop_ref is not None and loop_ref() is current:
        await client.aclose()


class _BridgeSession:
    """What `async with bridge_client(...) as c` hands the caller: the shared
    client seen through the caller's timeout, with the token header computed
    on each request so a rotated token is picked up straight away."""

    def __init__(self, client: httpx.AsyncClient, timeout) -> None:
        self._client = client
        self._timeout = timeout

    @property
    def headers(self) -> httpx.Headers:
        merged = httpx.Headers(self._client.headers)
        merged.update(bridge_headers())
        return merged

    def _prepare(self, kwargs: dict) -> dict:
        headers = dict(bridge_headers())
        if kwargs.get("headers"):
            headers.update(kwargs["headers"])
        kwargs["headers"] = headers
        kwargs.setdefault("timeout", self._timeout)
        return kwargs

    async def request(self, method: str, url, **kwargs) -> httpx.Response:
        return await self._client.request(method, url, **self._prepare(kwargs))

    async def get(self, url, **kwargs) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url, **kwargs) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url, **kwargs) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url, **kwargs) -> httpx.Response:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url, **kwargs) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)

    async def head(self, url, **kwargs) -> httpx.Response:
        return await self.request("HEAD", url, **kwargs)

    def stream(self, method: str, url, **kwargs):
        return self._client.stream(method, url, **self._prepare(kwargs))

    def __getattr__(self, name):
        return getattr(self._client, name)


class _BridgeClientContext:
    """The async context manager bridge_client() returns. Leaving it never
    closes the shared client; aclose_bridge_client() does that at shutdown."""

    def __init__(self, timeout) -> None:
        self._timeout = timeout

    async def __aenter__(self) -> _BridgeSession:
        return _BridgeSession(_shared_client(), self._timeout)

    async def __aexit__(self, *exc) -> None:
        return None


def bridge_client(*, timeout=_DEFAULT_TIMEOUT) -> _BridgeClientContext:
    """`async with bridge_client(timeout=...) as c:` for every app-to-bridge
    call: requests go through one pooled connection, carry the
    X-Bridge-Token header, honour this caller's timeout, and a 401 drops the
    cached token."""
    return _BridgeClientContext(timeout)
