"""Pi device-health toasts (FoodAssistant-h28s).

Turns a live Raspberry Pi power/thermal condition (under-voltage, throttling,
over-temperature) into a prominent on-screen toast on the kiosk. The host
bridge already detects these and exposes them at GET /system/warnings (a 60s
loop), and the app relays that at setup/system/health. Until now the only
on-screen surface was the nav-bar warning triangle, which is hidden in kiosk
mode, so an active under-voltage on an appliance showed nothing. This module
watches the same warnings feed and queues a device-health toast (ha_events
add_warning, always shown, even with HA events off) when a condition first goes
live.

Edge-triggered on purpose: a held condition toasts once at onset, not every
poll. A key that clears and later recurs toasts again. A since-boot-only flag
(the condition happened earlier but is not happening now) never toasts; it
stays on the nav icon and the status page. All decode/decision logic here is
pure so it is unit-testable without a Pi or the bridge.
"""
from __future__ import annotations

from . import ha_events

# Keys that read as a genuine fault the user should act on now, so their toast
# uses the error (red) level; the rest use the warning (amber) level. Mirrors
# the keys the host bridge emits (see _THROTTLE_BITS / _warnings_snapshot).
_ERROR_KEYS = frozenset({"undervoltage", "temp_limit", "temperature"})

# User-forward copy per warning key: (title, message). Written for the person
# looking at the kitchen screen, not the developer. Unknown keys fall back to
# the bridge's own message so a new condition still surfaces something useful.
_TOAST_COPY = {
    "undervoltage": (
        "Power warning",
        "Under-voltage detected. Check the Pi power supply and cable.",
    ),
    "throttled": (
        "Performance warning",
        "The Pi is throttling to protect itself. Check its power and cooling.",
    ),
    "freq_capped": (
        "Performance warning",
        "The Pi has capped its speed to cope. Check its power and cooling.",
    ),
    "temp_limit": (
        "Heat warning",
        "The Pi is running hot and slowing down. Improve the airflow around it.",
    ),
    "temperature": (
        "Heat warning",
        "The Pi is running hot and slowing down. Improve the airflow around it.",
    ),
    "disk": (
        "Storage warning",
        "Storage is almost full on the Pi. Free some space soon.",
    ),
}


def warning_level(key: str) -> str:
    """Toast level for a warning key: error for a real fault, else warning."""
    return "error" if key in _ERROR_KEYS else "warning"


def warning_toast_copy(warning: dict) -> tuple[str, str]:
    """User-forward (title, message) for one warning dict from the bridge feed.

    Falls back to the bridge's own message (then a generic line) for a key we
    do not have bespoke copy for, so an unfamiliar condition still says
    something rather than nothing. Pure."""
    key = (warning.get("key") or "") if isinstance(warning, dict) else ""
    if key in _TOAST_COPY:
        return _TOAST_COPY[key]
    msg = (warning.get("message") if isinstance(warning, dict) else "") or \
        "A device warning is active on the Pi."
    return ("Device warning", str(msg))


def warnings_to_toast(prev_active_keys, warnings):
    """Decide which warnings to toast this poll, edge-triggered. Pure.

    ``prev_active_keys`` is the set of live warning keys from the previous poll.
    ``warnings`` is the bridge feed (a list of {key, message, live} dicts).

    Returns ``(new_toasts, active_keys)``:
      - active_keys is the set of keys that are LIVE right now (what the next
        poll compares against).
      - new_toasts is the subset of live warnings whose key was not live on the
        previous poll, i.e. a fresh onset, in feed order.

    A since-boot-only flag (live is falsey) never enters active_keys and never
    toasts: it is informational history, not a condition happening now. A live
    condition that was already live last poll is not re-toasted (de-dup); one
    that cleared and recurs is, because it dropped out of active_keys when it
    cleared."""
    prev = set(prev_active_keys or ())
    active = set()
    new_toasts = []
    for w in warnings or []:
        if not isinstance(w, dict) or not w.get("live"):
            continue
        key = w.get("key") or ""
        if not key or key in active:
            continue
        active.add(key)
        if key not in prev:
            new_toasts.append(w)
    return new_toasts, active


# Live warning keys seen on the previous poll. Module-level state for the
# edge-trigger: a Pi appliance runs a single uvicorn worker, so one process
# owns the poll and one toast fires per onset. (The toast itself rides the
# shared ha_events ring, so it reaches the kiosk whichever worker it polls.)
_active_keys: set = set()


async def poll_and_toast(fetch_warnings) -> int:
    """Fetch the warnings feed once and toast any newly live condition.

    ``fetch_warnings`` is an async callable returning the warnings list (or
    None on an error / no bridge). Fail-soft: any error, a None feed, or a
    non-list yields no toast and leaves the remembered state untouched, so a
    bridge hiccup never false-toasts or clears a standing condition. Returns
    the number of toasts queued."""
    global _active_keys
    try:
        warnings = await fetch_warnings()
    except Exception:
        return 0
    if not isinstance(warnings, list):
        return 0
    new_toasts, active = warnings_to_toast(_active_keys, warnings)
    _active_keys = active
    for w in new_toasts:
        key = w.get("key") or ""
        title, message = warning_toast_copy(w)
        ha_events.add_warning(message, title=title, key=key,
                              level=warning_level(key))
    return len(new_toasts)


def reset() -> None:
    """Forget the remembered live keys (used by tests)."""
    global _active_keys
    _active_keys = set()
