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


def bridge_client(**kwargs) -> httpx.AsyncClient:
    """An httpx.AsyncClient wired for the host bridge.

    Drop-in for httpx.AsyncClient at every app-to-bridge call site: attaches
    the X-Bridge-Token header and invalidates the cached token on a 401.
    """
    return httpx.AsyncClient(
        headers=bridge_headers(),
        event_hooks={"response": [_drop_token_on_401]},
        **kwargs,
    )
