"""Best-effort Reolink AI-detection poller (FoodAssistant-akd0).

A Reolink camera can decide to pop itself up without any Home Assistant
automation: this polls each configured Reolink camera's ``GetAiState`` and
queues a camera pop-up (through the existing ``ha_events.add_camera`` channel)
when a detection type the camera's "pop up on" setting enabled is currently
alarming. Every step is best-effort: a camera that is offline, does not
support AI detection, or rejects the login is skipped rather than raised, so
one unreachable camera never stops the others from being checked.

This module is intentionally not wired into a background task loop yet (that
is a follow-up): ``poll_reolink_cameras_once`` is a single pass a caller (a
future periodic task in ``main.py``, or a manual "check now" action) can
invoke. Keeping it a plain async function keeps it easy to unit test with an
injected fetcher, and to call however it ends up being scheduled.
"""
from __future__ import annotations

from typing import Awaitable, Callable


async def poll_reolink_cameras_once(
    cameras: list,
    fetch_ai_state: Callable[[dict], Awaitable[dict]],
    popup_seconds: int,
    add_camera: Callable[..., int],
) -> list[int]:
    """One pass over ``cameras``: pop up any Reolink camera with an enabled,
    currently-alarming detection type. Returns the indexes popped.

    ``fetch_ai_state`` and ``add_camera`` are injected so this stays testable
    without a network or the real event ring; production callers pass
    ``cameras.fetch_reolink_ai_state`` and ``ha_events.add_camera``.
    """
    from .cameras import is_reolink
    from .camera_detect import reolink_popup_decision

    popped: list[int] = []
    for idx, cam in enumerate(cameras or []):
        if not isinstance(cam, dict) or not is_reolink(cam):
            continue
        enabled = cam.get("popup_types") or []
        if not enabled:
            continue
        try:
            state = await fetch_ai_state(cam)
        except Exception:
            continue
        should, _detected = reolink_popup_decision(state, enabled)
        if not should:
            continue
        name = cam.get("name", "") or f"Camera {idx}"
        add_camera(name=name, src=f"ui/camera/{idx}/snapshot", seconds=popup_seconds)
        popped.append(idx)
    return popped
