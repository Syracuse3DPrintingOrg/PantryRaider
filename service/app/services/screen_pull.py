"""A Bandit pulls the events the main server holds for its screen.

Home Assistant (or anything else) tells the main server "show this on that
Bandit". The server cannot count on reaching a Bandit, so the Bandit asks:
one long poll held open against GET /api/screens/poll, answered the moment
something is queued for it. What arrives is put in this device's own event
ring, marked for the display, so the kitchen screen moves and a phone that
has this Bandit's pages open does not.

A "recipe" event carries the recipe itself, because a Bandit keeps its own
current recipe: it is made current here before the page change that follows.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from . import upstream

log = logging.getLogger("foodassistant.screen_pull")

WAIT_SECONDS = 25
# An older main server has no such route; ask again now and then in case it
# is updated.
RETRY_UNSUPPORTED = 600.0
BACKOFF_MAX = 60.0
_SHOWN = ("navigate", "notification", "camera", "confirm")  # a Bandit's close is a navigate home


def apply_event(event: dict) -> bool:
    """Act on one pulled event. True when it was something this build shows."""
    from . import current_recipe, ha_events
    kind = event.get("type")
    if kind == "recipe":
        recipe = event.get("recipe")
        if isinstance(recipe, dict):
            current_recipe.set_active(recipe)
            return True
        return False
    if kind == "step":
        # Voice moved the step this screen is on (the server keeps it too).
        # Cook mode on this device follows the local marker.
        from . import screen_recipes
        try:
            n = int(event.get("step"))
        except (TypeError, ValueError):
            n = 0
        screen_recipes.set_step(screen_recipes.SERVER_KEY, current_recipe.get_active(),
                                n - 1 if n >= 1 else None)
        return True
    if kind in _SHOWN:
        ha_events.add_for_display(event)
        return True
    return False


async def run() -> None:
    """Pull until cancelled. Quiet while this device is not linked to a main
    server; backs off while the server cannot be reached."""
    from ..config import settings
    since = 999999999
    delay = 1.0
    async with httpx.AsyncClient(
            timeout=httpx.Timeout(WAIT_SECONDS + 10.0, connect=5.0)) as client:
        while True:
            if not (settings.is_satellite() and settings.upstream_linked()
                    and settings.device_id):
                await asyncio.sleep(30)
                continue
            base = settings.remote_server_url.rstrip("/")
            try:
                resp = await client.get(
                    f"{base}/api/screens/poll",
                    params={"screen": settings.device_id, "since": since,
                            "wait": WAIT_SECONDS},
                    **upstream.request_kwargs())
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX)
                continue
            if resp.status_code == 404:
                # No such route yet, or the server has not recorded this
                # device's next config pull.
                await asyncio.sleep(RETRY_UNSUPPORTED if delay > 30 else 60)
                delay = BACKOFF_MAX
                continue
            if resp.status_code != 200:
                await asyncio.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX)
                continue
            delay = 1.0
            try:
                data = resp.json()
                last = int(data.get("last_id", 0))
                events = data.get("events") or []
            except (ValueError, TypeError, AttributeError):
                await asyncio.sleep(5)
                continue
            # A cursor ahead of the server's means it restarted: start over
            # from where it is now.
            since = last
            for event in events:
                try:
                    if isinstance(event, dict):
                        await asyncio.to_thread(apply_event, event)
                except Exception:
                    log.debug("could not apply a screen event", exc_info=True)
