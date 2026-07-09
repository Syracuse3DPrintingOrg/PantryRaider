"""Server-side execution for deck-only Start Page keys (Pantry Raider).

The Start Page renders the shared custom keys (streamdeck_key_overrides) with
the same faces as the Stream Deck, but the action-typed keys (HA toggle, media,
macro) used to only work on a physical deck; on-screen they showed a "needs a
deck" note. This module fires them from the server instead: it resolves an
override with the same semantics as the deck's override_to_spec and calls Home
Assistant directly using the shared deck HA settings (streamdeck_ha_base_url /
streamdeck_ha_token). The built-in ha_1..ha_5 slot keys resolve from
streamdeck_ha_slots the same way the deck controller binds them.

Macros run their server-executable children (HA slot keys and the preset
kitchen timers, which become shared server timers); children that only make
sense on deck hardware (paging, brightness, navigation) are skipped and
reported, matching the deck's own skip-unknown behaviour.

Resolution is kept in pure helpers so it is unit-testable without a network;
only call_ha_service touches HTTP.
"""
from __future__ import annotations

import re

# Mirrors the deck's MEDIA_ACTIONS mapping (streamdeck actions.MEDIA_ACTIONS):
# each media transport action is a media_player service that takes only an
# entity_id, so the plain HA service call handles all of them.
MEDIA_SERVICES: dict[str, str] = {
    "play_pause": "media_player.media_play_pause",
    "next": "media_player.media_next_track",
    "previous": "media_player.media_previous_track",
    "volume_up": "media_player.volume_up",
    "volume_down": "media_player.volume_down",
    "stop": "media_player.media_stop",
}

# Preset kitchen timers a macro can run on-screen, mirroring the deck's
# timer_eggs/timer_pasta/timer_rice ActionSpecs (label, minutes). On-screen
# they become shared server timers so every surface sees the countdown.
MACRO_TIMER_PRESETS: dict[str, tuple[str, int]] = {
    "timer_eggs": ("Eggs", 6),
    "timer_pasta": ("Pasta", 10),
    "timer_rice": ("Rice", 18),
}

_HA_SLOT_RE = re.compile(r"^ha_([1-5])$")

# Built-in cycle timer keys and the stages a press walks through, mirroring
# the deck's TIMER_CYCLE_MINUTES. On-screen the cycle runs against the SHARED
# registry: press while running advances to the next stage, past the last
# stage stops, and a press on an expired timer dismisses it.
TIMER_CYCLE_MINUTES: tuple[int, ...] = (5, 10, 15, 30, 60)
_CYCLE_TIMER_LABELS = {"timer_1": "Timer 1", "timer_2": "Timer 2", "timer_3": "Timer 3"}


def _mmss(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _fire_timer(label: str, minutes: int, long: bool = False) -> dict:
    """Press a timer key against the shared registry (FoodAssistant-b8o1).

    Short press: an idle key starts its timer (the preset minutes, or 1:00 for
    a plain Timer 1/2/3 key), a running one adds a minute, an expired one is
    dismissed. Long press: cancels/resets whatever the key holds.
    """
    from . import timers
    existing = next((t for t in timers.list_timers() if t["label"] == label), None)
    if long:
        if existing:
            timers.cancel_timer(existing["id"])
            return {"ok": True, "detail": f"{label} reset."}
        return {"ok": True, "detail": f"{label} is not running."}
    if existing and existing["expired"]:
        timers.cancel_timer(existing["id"])
        return {"ok": True, "detail": f"{label} dismissed."}
    if existing:
        extended = timers.extend_timer(existing["id"], 60)
        if extended:
            return {"ok": True, "detail":
                    f"{label} +1:00 ({_mmss(extended['remaining_seconds'])} left)."}
        timers.cancel_timer(existing["id"])
        return {"ok": True, "detail": f"{label} dismissed."}
    start = (minutes if minutes > 0 else 1) * 60
    timers.create_timer(label, start)
    return {"ok": True, "detail": f"{label} started: {_mmss(start)}."}


def find_override(custom_id: str, overrides: list | None) -> dict | None:
    """The override entry with the given id, or None."""
    for ov in overrides or []:
        if isinstance(ov, dict) and str(ov.get("id") or "").strip() == custom_id:
            return ov
    return None


def resolve_ha_call(override: dict) -> tuple[str, str] | None:
    """(entity_id, service) for an ha_action override, or None if unusable.

    Same semantics as the deck's override_to_spec: either a bare entity_id
    (toggled via homeassistant.toggle) or an explicit service such as
    "script.goodnight"; a bare service implies its own entity."""
    entity_id = str(override.get("entity_id", "")).strip()
    service = str(override.get("service", "")).strip()
    if not entity_id and not service:
        return None
    if not service:
        service = "homeassistant.toggle"
    if not entity_id and "." in service:
        entity_id = service
    return entity_id, service


def resolve_media_call(override: dict) -> tuple[str, str] | None:
    """(entity_id, service) for a media override, or None without an entity."""
    entity_id = str(override.get("entity_id", "")).strip()
    if not entity_id:
        return None
    action = str(override.get("action", "play_pause")).strip()
    service = MEDIA_SERVICES.get(action) or MEDIA_SERVICES["play_pause"]
    return entity_id, service


def resolve_ha_slot(name: str, slots: list | None) -> tuple[str, str] | None:
    """(entity_id, service) for a built-in ha_1..ha_5 slot key, or None.

    Slots map to keys ha_1..ha_5 in order, exactly as the deck controller
    binds streamdeck_ha_slots."""
    m = _HA_SLOT_RE.match(str(name or ""))
    if not m:
        return None
    idx = int(m.group(1)) - 1
    slots = [s for s in (slots or []) if isinstance(s, dict)]
    if idx >= len(slots):
        return None
    entity_id = str(slots[idx].get("entity_id", "")).strip()
    if not entity_id:
        return None
    service = str(slots[idx].get("service", "")).strip() or "homeassistant.toggle"
    return entity_id, service


async def call_ha_service(entity_id: str, service: str) -> tuple[bool, str]:
    """POST a Home Assistant service call using the shared deck HA settings.

    Returns (ok, short detail). Not configured or unreachable is reported, not
    raised, so a Start Page press always gets a toastable answer."""
    from ..config import settings
    base = (settings.streamdeck_ha_base_url or "").strip().rstrip("/")
    token = (settings.streamdeck_ha_token or "").strip()
    if not base or not token:
        return False, "Home Assistant is not configured (Settings > Stream Deck)."
    domain, svc = (service.split(".", 1) + ["turn_on"])[:2]
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                f"{base}/api/services/{domain}/{svc}",
                json={"entity_id": entity_id},
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
            )
        if r.status_code < 300:
            return True, f"Sent {service}"
        return False, f"Home Assistant answered {r.status_code}."
    except Exception:  # noqa: BLE001 - surfaced as a toast, never a 500
        return False, "Could not reach Home Assistant."


async def _fire_macro(override: dict) -> dict:
    """Run a macro override's server-executable children in order.

    Executable children: built-in HA slot keys (ha_1..ha_5) and the preset
    kitchen timers. Anything else (paging, brightness, navigation, nested
    macros) is deck-only and skipped, mirroring the deck's skip-unknown rule.
    The first HA failure stops the run so a half-fired macro is visible."""
    from ..config import settings
    from . import timers
    raw = override.get("actions", [])
    if isinstance(raw, str):
        names = [n.strip() for n in raw.split(",") if n.strip()]
    elif isinstance(raw, (list, tuple)):
        names = [str(n).strip() for n in raw if str(n).strip()]
    else:
        names = []
    if not names:
        return {"ok": False, "detail": "This macro has no actions."}
    ran, skipped = 0, []
    for name in names:
        slot = resolve_ha_slot(name, settings.streamdeck_ha_slots)
        if slot:
            ok, detail = await call_ha_service(*slot)
            if not ok:
                return {"ok": False, "detail": f"Stopped at {name}: {detail}"}
            ran += 1
            continue
        preset = MACRO_TIMER_PRESETS.get(name)
        if preset:
            label, minutes = preset
            timers.create_timer(label, minutes * 60)
            ran += 1
            continue
        skipped.append(name)
    if ran == 0:
        return {"ok": False,
                "detail": "No actions in this macro can run on-screen; it needs a connected Stream Deck."}
    detail = f"Ran {ran} of {len(names)} actions"
    if skipped:
        detail += f" ({', '.join(skipped)} need a connected Stream Deck)"
    return {"ok": True, "detail": detail + "."}


async def fire_key(name: str, long: bool = False) -> dict:
    """Execute a Start Page key press server-side by token.

    ``name`` is a Start Page token: a custom key id from
    streamdeck_key_overrides, or a built-in ha_1..ha_5 slot key. Returns a
    JSON-ready {"ok": bool, "detail": str}; unknown or non-executable tokens
    are reported, never raised."""
    from ..config import settings
    slot = resolve_ha_slot(name, settings.streamdeck_ha_slots)
    if slot:
        ok, detail = await call_ha_service(*slot)
        return {"ok": ok, "detail": detail}
    if name in _CYCLE_TIMER_LABELS:
        return _fire_timer(_CYCLE_TIMER_LABELS[name], 0, long=long)
    if name in MACRO_TIMER_PRESETS:
        label, minutes = MACRO_TIMER_PRESETS[name]
        return _fire_timer(label, minutes, long=long)
    override = find_override(name, settings.streamdeck_key_overrides)
    if not override:
        return {"ok": False, "detail": "Unknown key."}
    otype = str(override.get("type", ""))
    if otype == "ha_action":
        call = resolve_ha_call(override)
        if not call:
            return {"ok": False, "detail": "This key has no entity or service configured."}
        ok, detail = await call_ha_service(*call)
        return {"ok": ok, "detail": detail}
    if otype == "media":
        call = resolve_media_call(override)
        if not call:
            return {"ok": False, "detail": "This key has no media player configured."}
        ok, detail = await call_ha_service(*call)
        return {"ok": ok, "detail": detail}
    if otype == "macro":
        return await _fire_macro(override)
    if otype == "timer":
        label = str(override.get("label", "")).strip() or "Timer"
        try:
            minutes = max(0, int(override.get("minutes", 0)))
        except (TypeError, ValueError):
            minutes = 0
        return _fire_timer(label, minutes, long=long)
    return {"ok": False, "detail": "This key opens a page instead."}
