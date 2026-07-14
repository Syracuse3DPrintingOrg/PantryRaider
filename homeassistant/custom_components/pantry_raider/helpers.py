"""Pure helpers for the Pantry Raider integration.

Everything in this module is deliberately free of any Home Assistant import so
the repo's pure-logic test suite can exercise it without the heavyweight
``homeassistant`` package installed. The reasoning that decides which entities a
given /ha/state payload should produce, and the small display formatters, all
live here so they can be unit tested in isolation. The HA-facing modules
(sensor.py, number.py, etc.) wrap these values in entity objects.
"""

from __future__ import annotations

from typing import Any


# Modes that run the full stack. A satellite (pi_remote) install returns a thin
# payload with no inventory, timers, or thermometers, so the server-only
# entities must never be built for it.
_SERVER_MODES = ("server", "pi_hosted")


# Screensaver modes the app accepts, mapped to the label a person reads in the
# HA dropdown. Keeping the mapping here keeps select.py free of copy and lets a
# test assert the option set matches the app's allowed values.
SCREENSAVER_MODE_LABELS: dict[str, str] = {
    "bounce": "Bounce logo",
    "photos": "Photo slideshow",
    "toasters": "Flying toasters",
    "starfield": "Starfield",
}

# The three presence-wake choices the app exposes.
WAKE_ON_PRESENCE_OPTIONS: tuple[str, ...] = ("auto", "on", "off")
WAKE_ON_PRESENCE_LABELS: dict[str, str] = {
    "auto": "Auto (wake when someone is near)",
    "on": "Always on",
    "off": "Never wake automatically",
}


def is_server_mode(mode: Any) -> bool:
    """True when the install runs the full stack (server or pi_hosted)."""

    return mode in _SERVER_MODES


def server_device_model(mode: Any) -> str:
    """Device-registry model string for the primary install.

    A pi_hosted box is a physical appliance, a plain server is not, and a
    bandit added directly by its own address still reports as a Bandit so the
    card in HA matches what the user physically owns.
    """

    if mode == "pi_hosted":
        return "Pantry Raider Appliance"
    if mode == "pi_remote":
        return "Pantry Raider Bandit"
    return "Pantry Raider Server"


def format_timer_remaining(seconds: Any) -> str:
    """Render a countdown as mm:ss (minutes are not clamped to two digits).

    The app hands back a float number of seconds; a long bake can exceed an
    hour, so minutes are shown in full rather than wrapped.
    """

    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "0:00"
    if total < 0:
        total = 0
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def next_timer_state(next_obj: Any) -> str:
    """State string for the "next timer" sensor: "Label mm:ss" or "none"."""

    if not isinstance(next_obj, dict):
        return "none"
    label = next_obj.get("label")
    if not label:
        return "none"
    return f"{label} {format_timer_remaining(next_obj.get('remaining_seconds'))}"


def applicable_sensor_keys(data: Any) -> list[str]:
    """Which static sensor keys a payload supports.

    Version and the label-printer queue come back for every mode, so a
    directly-added bandit still gets them. The inventory, review, and timer
    counts only exist on a full-stack install.
    """

    keys = ["version", "label_queue"]
    payload = data or {}
    if is_server_mode(payload.get("mode")):
        keys += [
            "expired",
            "today",
            "within_3_days",
            "within_7_days",
            "pending",
            "action_items",
            "timers_running",
            "next_timer",
        ]
    return keys


def sensor_value(key: str, data: Any) -> Any:
    """Extract a single static sensor's native value from a payload.

    Every lookup tolerates a missing branch (returns None) so a payload that
    briefly drops a section never raises inside the event loop.
    """

    payload = data or {}
    if key == "version":
        return payload.get("version")
    if key == "label_queue":
        return (payload.get("printers") or {}).get("label_queue")

    expiring = payload.get("expiring") or {}
    if key == "expired":
        return expiring.get("expired")
    if key == "today":
        return expiring.get("today")
    if key == "within_3_days":
        return expiring.get("within_3_days")
    if key == "within_7_days":
        return expiring.get("within_7_days")

    counts = payload.get("counts") or {}
    if key == "pending":
        return counts.get("pending")
    if key == "action_items":
        return counts.get("action_items")

    timers = payload.get("timers") or {}
    if key == "timers_running":
        return timers.get("running")
    if key == "next_timer":
        return next_timer_state(timers.get("next"))
    return None


def expiring_attention(data: Any) -> bool:
    """True when something is already expired or expires today.

    Drives the "expiring attention" problem binary sensor: the two buckets a
    person should act on now, as opposed to the 3/7 day lookahead.
    """

    expiring = (data or {}).get("expiring") or {}
    try:
        expired = int(expiring.get("expired") or 0)
        today = int(expiring.get("today") or 0)
    except (TypeError, ValueError):
        return False
    return (expired + today) > 0


def probe_display_name(thermometer_name: Any, role_label: Any) -> str:
    """Friendly name for one thermometer probe, e.g. "TempSpike Internal"."""

    base = str(thermometer_name).strip() if thermometer_name else "Thermometer"
    role = str(role_label).strip() if role_label else ""
    return f"{base} {role}".strip() if role else base


def probe_unique_id(device_id: Any, thermometer_id: Any, index: Any) -> str:
    """Stable unique_id for a probe sensor, keyed to its install and slot."""

    return f"{device_id}_{thermometer_id}_p{index}"


def iter_probes(data: Any):
    """Yield (thermometer, probe) pairs from a payload's thermometer list.

    Both the app and a flaky BLE link can hand back partial rows, so anything
    that is not a dict is skipped rather than trusted.
    """

    for thermo in (data or {}).get("thermometers") or []:
        if not isinstance(thermo, dict):
            continue
        for probe in thermo.get("probes") or []:
            if not isinstance(probe, dict):
                continue
            yield thermo, probe


def satellite_device_ids(data: Any) -> list[str]:
    """Device ids of the satellites a server payload advertises.

    Rows without a usable device_id are dropped so a coordinator is never keyed
    on None.
    """

    ids: list[str] = []
    for sat in (data or {}).get("satellites") or []:
        if not isinstance(sat, dict):
            continue
        device_id = sat.get("device_id")
        if device_id:
            ids.append(str(device_id))
    return ids
