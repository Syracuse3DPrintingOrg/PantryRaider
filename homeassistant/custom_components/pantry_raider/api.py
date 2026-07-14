"""Thin async client for a Pantry Raider install's Home Assistant endpoints.

One client talks to one base URL (a server, an appliance, or a bandit). It uses
Home Assistant's shared aiohttp session so there is no extra pip dependency and
no blocking I/O in the event loop.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import async_timeout

# A request that outlives this never blocks a poll cycle; the app answers
# /ha/state in well under a second on healthy hardware.
_REQUEST_TIMEOUT = 10


class PantryRaiderAuthError(Exception):
    """Raised when the install rejects the API key (401/403)."""


class PantryRaiderConnectionError(Exception):
    """Raised when the install cannot be reached or returns an error status."""


class PantryRaiderClient:
    """Talks to one install's /ha/state and /ha/settings endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str | None = None,
    ) -> None:
        # Trailing slash trimmed so URL joins stay predictable.
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or None

    @property
    def base_url(self) -> str:
        return self._base_url

    def _headers(self) -> dict[str, str]:
        # The key is optional: an install with no password accepts an
        # unauthenticated call, so only send the header when we have one.
        if self._api_key:
            return {"X-API-Key": self._api_key}
        return {}

    async def async_get_state(self) -> dict[str, Any]:
        """Fetch /ha/state, raising typed errors the caller can branch on."""

        url = f"{self._base_url}/ha/state"
        try:
            async with async_timeout.timeout(_REQUEST_TIMEOUT):
                resp = await self._session.get(url, headers=self._headers())
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PantryRaiderConnectionError(str(err)) from err

        if resp.status in (401, 403):
            raise PantryRaiderAuthError(f"auth rejected ({resp.status})")
        if resp.status != 200:
            raise PantryRaiderConnectionError(f"unexpected status {resp.status}")
        try:
            return await resp.json()
        except (aiohttp.ClientError, ValueError) as err:
            raise PantryRaiderConnectionError(f"bad json: {err}") from err

    async def async_post_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a settings subset to /ha/settings and return the app's reply."""

        url = f"{self._base_url}/ha/settings"
        try:
            async with async_timeout.timeout(_REQUEST_TIMEOUT):
                resp = await self._session.post(
                    url, headers=self._headers(), json=payload
                )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PantryRaiderConnectionError(str(err)) from err

        if resp.status in (401, 403):
            raise PantryRaiderAuthError(f"auth rejected ({resp.status})")
        if resp.status != 200:
            raise PantryRaiderConnectionError(f"unexpected status {resp.status}")
        try:
            return await resp.json()
        except (aiohttp.ClientError, ValueError) as err:
            raise PantryRaiderConnectionError(f"bad json: {err}") from err
