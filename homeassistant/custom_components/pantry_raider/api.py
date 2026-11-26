"""Thin async client for a Pantry Raider install's Home Assistant endpoints.

One client talks to one base URL (a server, an appliance, or a bandit). It uses
Home Assistant's shared aiohttp session so there is no extra pip dependency and
no blocking I/O in the event loop.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from .helpers import client_header

# Sent as X-PR-Caps (kept here as well as in const.py so this module stays
# free of Home Assistant imports): this integration re-polls an approved
# pairing whose key has not arrived yet.
_CLIENT_CAPS = "pair1"

# A request that outlives this never blocks a poll cycle; the app answers
# /ha/state in well under a second on healthy hardware.
_REQUEST_TIMEOUT = 10


class PantryRaiderAuthError(Exception):
    """Raised when the install rejects the API key (401/403)."""


class PantryRaiderConnectionError(Exception):
    """Raised when the install cannot be reached or returns an error status."""


class PantryRaiderNotSupported(PantryRaiderConnectionError):
    """Raised on a 404 where a newer install has the route: the install is an
    older version. A subclass, so existing connection-error handling still
    catches it."""


class PantryRaiderClient:
    """Talks to one install's /ha/state and /ha/settings endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str | None = None,
        client_version: str = "",
    ) -> None:
        # Trailing slash trimmed so URL joins stay predictable.
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or None
        self._client_version = client_version or ""

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def api_key(self) -> str | None:
        return self._api_key

    def set_api_key(self, api_key: str | None) -> None:
        """Use a new key from the next request on (after a key exchange)."""
        self._api_key = api_key or None

    def _headers(self) -> dict[str, str]:
        # Who is calling, on every request (older installs ignore both). The
        # key is optional: an install with no password may accept a keyless
        # call, so the key header is only sent when there is one.
        headers = {
            "X-PR-Client": client_header(self._client_version),
            "X-PR-Caps": _CLIENT_CAPS,
        }
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    async def async_get_state(self) -> dict[str, Any]:
        """Fetch /ha/state, raising typed errors the caller can branch on."""

        url = f"{self._base_url}/ha/state"
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT):
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
            async with asyncio.timeout(_REQUEST_TIMEOUT):
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

    async def async_display_action(self, action: str) -> dict[str, Any]:
        """Sleep or wake the install's screen through POST /ha/display.

        The app answers 200 even when it cannot act (for example off-Pi, where
        there is no panel to sleep), carrying ``ok`` and an optional ``detail``,
        so the caller decides how to surface a soft failure. An older server
        without this endpoint answers 404, raised as a connection error the
        button turns into a clear "not supported" message.
        """
        url = f"{self._base_url}/ha/display"
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT):
                resp = await self._session.post(
                    url, headers=self._headers(), json={"action": action}
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

    async def _async_post_json(
        self, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """POST a JSON body to one of the install's endpoints, typed errors."""

        url = f"{self._base_url}{path}"
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT):
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

    async def async_call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = _REQUEST_TIMEOUT,
        ok_statuses: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        """One JSON call to the install, for the screen, recipe and timer
        routes. ``ok_statuses`` lists the answers that carry a JSON body worth
        returning (the app says "not found" or "still running" with a body and
        a 4xx). A 404 outside that list means an older install."""

        url = f"{self._base_url}{path}"
        try:
            async with asyncio.timeout(timeout):
                resp = await self._session.request(
                    method, url, headers=self._headers(), json=payload, params=params
                )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PantryRaiderConnectionError(str(err)) from err
        if resp.status in (401, 403):
            raise PantryRaiderAuthError(f"auth rejected ({resp.status})")
        if resp.status not in ok_statuses:
            if resp.status == 404:
                raise PantryRaiderNotSupported(f"{path} not supported")
            raise PantryRaiderConnectionError(f"unexpected status {resp.status}")
        try:
            body = await resp.json()
        except (aiohttp.ClientError, ValueError) as err:
            raise PantryRaiderConnectionError(f"bad json: {err}") from err
        return body if isinstance(body, dict) else {}

    async def async_screens(self) -> list[dict[str, Any]]:
        """Every screen the install can show something on (GET /api/screens)."""
        body = await self.async_call("GET", "/api/screens")
        return [s for s in body.get("screens") or [] if isinstance(s, dict)]

    async def async_show_recipe(
        self, query: str = "", screen: str = "", slug: str = ""
    ) -> dict[str, Any]:
        """Find a recipe by what was said and show it (POST
        /api/screens/show-recipe). The reply carries ``result`` and a
        ``speech`` line; a screen that does not exist comes back as ok False."""
        payload: dict[str, Any] = {"query": query, "screen": screen}
        if slug:
            payload["slug"] = slug
        return await self.async_call(
            "POST", "/api/screens/show-recipe", payload, ok_statuses=(200, 400, 404, 409)
        )

    async def async_timers(self) -> list[dict[str, Any]]:
        body = await self.async_call("GET", "/timers")
        return [t for t in body.get("timers") or [] if isinstance(t, dict)]

    async def async_start_timer(
        self, label: str, seconds: int, origin: str = "", screen: str = ""
    ) -> dict[str, Any]:
        return await self.async_call(
            "POST",
            "/timers",
            {"label": label, "seconds": seconds, "origin": origin, "screen": screen},
        )

    async def async_delete_timer(self, timer_id: int) -> bool:
        """Remove one timer, running or finished. False when it was gone."""
        body = await self.async_call(
            "DELETE", f"/timers/{int(timer_id)}", ok_statuses=(200, 404)
        )
        return bool(body.get("ok"))

    async def async_dismiss_timers(self, origin: str = "") -> int:
        """Dismiss every finished timer, or only those one origin set."""
        body = await self.async_call("POST", "/timers/dismiss", {"origin": origin})
        return int(body.get("dismissed") or 0)

    async def async_timer_events(self, since: int, wait: int = 0) -> dict[str, Any]:
        """What happened to timers since ``since`` (GET /timers/events). With
        ``wait`` the install holds the request until something happens."""
        return await self.async_call(
            "GET",
            "/timers/events",
            params={"since": since, "wait": wait},
            timeout=wait + _REQUEST_TIMEOUT,
        )

    async def async_show_page(self, page: str, screen: str) -> dict[str, Any]:
        """Move one screen to a named page, or close what it was sent."""
        if page == "close":
            return await self.async_call(
                "POST", "/events/close", {"screen": screen}, ok_statuses=(200, 400, 404)
            )
        return await self.async_call(
            "POST",
            "/events/navigate",
            {"page": page, "screen": screen},
            ok_statuses=(200, 400, 404),
        )

    async def async_notify(
        self,
        message: str,
        title: str = "",
        level: str = "info",
        timeout: int = 0,
    ) -> dict[str, Any]:
        """Show a notification toast on this install's screen.

        POSTs /events/notify, the same channel a rest_command used to fill.
        The app answers 200 with ``ok`` false when it refuses the payload, so
        the caller reads the reply rather than trusting the status alone.
        """
        return await self._async_post_json(
            "/events/notify",
            {"message": message, "title": title, "level": level, "timeout": timeout},
        )

    async def async_camera_popup(
        self, camera: str = "", seconds: int = 0
    ) -> dict[str, Any]:
        """Pop a camera feed up on this install's screen (POST /events/camera-popup).

        An empty camera name means the install's first configured camera;
        seconds 0 means the install's own configured pop-up length.
        """
        return await self._async_post_json(
            "/events/camera-popup", {"camera": camera, "seconds": seconds}
        )

    async def async_camera_detect(
        self, camera: str, detection_type: str, seconds: int = 0
    ) -> dict[str, Any]:
        """Report a camera detection and let the install decide whether to pop up.

        POSTs /events/camera-detect: the install checks the camera's own
        "pop up on" setting for this detection type, so the reply carries
        ``popped`` telling the caller whether anything showed.
        """
        return await self._async_post_json(
            "/events/camera-detect",
            {"camera": camera, "detection_type": detection_type, "seconds": seconds},
        )

    async def async_connect(
        self,
        base_url: str,
        token: str,
        token_kind: str = "owner",
        component_version: str = "",
    ) -> dict[str, Any]:
        """Hand the install a Home Assistant base URL and long-lived token.

        POSTs /ha/connect so the install can reach Home Assistant back (camera
        proxying, Stream Deck). ``token_kind`` says whose token it is
        ('dedicated' for the limited account made for this install); an older
        install ignores it and the version. Returns the app's reply, which
        carries the ``verified`` flag telling us whether the install could
        immediately reach that URL. An older server without the endpoint
        answers 404, raised as a connection error the caller logs and skips.
        """
        url = f"{self._base_url}/ha/connect"
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT):
                resp = await self._session.post(
                    url,
                    headers=self._headers(),
                    json={
                        "base_url": base_url,
                        "token": token,
                        "token_kind": token_kind,
                        "component_version": component_version,
                    },
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

    async def async_disconnect(self) -> dict[str, Any]:
        """Ask the install to forget the Home Assistant connection (POST
        /ha/disconnect). An older install without the route answers 404,
        which comes back as {'ok': False, 'unsupported': True}."""
        url = f"{self._base_url}/ha/disconnect"
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT):
                resp = await self._session.post(url, headers=self._headers(), json={})
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PantryRaiderConnectionError(str(err)) from err
        if resp.status == 404:
            return {"ok": False, "unsupported": True}
        if resp.status in (401, 403):
            raise PantryRaiderAuthError(f"auth rejected ({resp.status})")
        if resp.status != 200:
            raise PantryRaiderConnectionError(f"unexpected status {resp.status}")
        try:
            return await resp.json()
        except (aiohttp.ClientError, ValueError) as err:
            raise PantryRaiderConnectionError(f"bad json: {err}") from err

    async def async_revoke_own_key(self) -> dict[str, Any]:
        """Ask the install to turn off the key this client holds (POST
        /ha/revoke-key). The install only turns off a Home Assistant key it
        made itself; for any other key it answers ok False and keeps it. An
        older install without the route answers 404, which comes back as
        {'ok': False, 'unsupported': True}."""
        url = f"{self._base_url}/ha/revoke-key"
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT):
                resp = await self._session.post(url, headers=self._headers(), json={})
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PantryRaiderConnectionError(str(err)) from err
        if resp.status == 404:
            return {"ok": False, "unsupported": True}
        if resp.status in (401, 403):
            raise PantryRaiderAuthError(f"auth rejected ({resp.status})")
        try:
            body = await resp.json()
        except (aiohttp.ClientError, ValueError) as err:
            raise PantryRaiderConnectionError(f"bad json: {err}") from err
        if resp.status not in (200, 400, 409) or not isinstance(body, dict):
            raise PantryRaiderConnectionError(f"unexpected status {resp.status}")
        return body

    async def async_exchange_legacy_key(self) -> dict[str, Any]:
        """Swap the shared key this entry was set up with for a key of its
        own, limited to what Home Assistant needs (POST
        /api/credentials/exchange). Returns {'ok', 'id', 'key'}. The old key
        keeps working, so a failure changes nothing. An older install answers
        404 (PantryRaiderNotSupported)."""
        url = f"{self._base_url}/api/credentials/exchange"
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT):
                resp = await self._session.post(
                    url, headers=self._headers(), json={"kind": "ha"}
                )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PantryRaiderConnectionError(str(err)) from err
        if resp.status == 404:
            raise PantryRaiderNotSupported("key exchange not supported")
        if resp.status in (401, 403):
            raise PantryRaiderAuthError(f"auth rejected ({resp.status})")
        if resp.status != 200:
            raise PantryRaiderConnectionError(f"unexpected status {resp.status}")
        try:
            return await resp.json()
        except (aiohttp.ClientError, ValueError) as err:
            raise PantryRaiderConnectionError(f"bad json: {err}") from err

    async def async_request_pairing(
        self, hostname: str = "Home Assistant", kind: str = "ha"
    ) -> dict[str, Any]:
        """Open a pairing request on the install (FoodAssistant-4box flow).

        Unauthenticated on purpose: pairing is how a device that has no key yet
        earns one. ``kind`` asks for a Home Assistant key (an older install
        ignores it). The install gates pairing to the local network and to
        installs with device pairing enabled; a 403 here means pairing is
        turned off and the user should paste a key instead. A 404 means the
        install is older than pairing (PantryRaiderNotSupported).
        """
        url = f"{self._base_url}/api/pairing/request"
        headers = self._headers()
        headers.pop("X-API-Key", None)
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT):
                resp = await self._session.post(
                    url, headers=headers, json={"hostname": hostname, "kind": kind}
                )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PantryRaiderConnectionError(str(err)) from err
        if resp.status == 403:
            raise PantryRaiderAuthError("pairing unavailable")
        if resp.status == 404:
            raise PantryRaiderNotSupported("pairing not supported")
        if resp.status != 200:
            raise PantryRaiderConnectionError(f"unexpected status {resp.status}")
        try:
            return await resp.json()
        except (aiohttp.ClientError, ValueError) as err:
            raise PantryRaiderConnectionError(f"bad json: {err}") from err

    async def async_pairing_status(self, request_id: str) -> dict[str, Any]:
        """Poll a pairing request: pending, approved (with the key), denied,
        or expired."""
        url = f"{self._base_url}/api/pairing/status/{request_id}"
        headers = self._headers()
        headers.pop("X-API-Key", None)
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT):
                resp = await self._session.get(url, headers=headers)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PantryRaiderConnectionError(str(err)) from err
        if resp.status == 403:
            raise PantryRaiderAuthError("pairing unavailable")
        if resp.status != 200:
            raise PantryRaiderConnectionError(f"unexpected status {resp.status}")
        try:
            return await resp.json()
        except (aiohttp.ClientError, ValueError) as err:
            raise PantryRaiderConnectionError(f"bad json: {err}") from err
