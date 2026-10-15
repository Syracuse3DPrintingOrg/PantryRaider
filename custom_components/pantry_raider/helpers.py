"""Pure helpers for the Pantry Raider integration.

Everything in this module is deliberately free of any Home Assistant import so
the repo's pure-logic test suite can exercise it without the heavyweight
``homeassistant`` package installed. The reasoning that decides which entities a
given /ha/state payload should produce, and the small display formatters, all
live here so they can be unit tested in isolation. The HA-facing modules
(sensor.py, number.py, etc.) wrap these values in entity objects.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any


def http_url(host: Any, port: Any = None) -> str:
    """An http:// URL for a host and optional port. An IPv6 address goes in
    brackets, or its colons would read as the port."""
    text = str(host or "").strip()
    if ":" in text and not text.startswith("["):
        text = f"[{text}]"
    return f"http://{text}:{port}" if port not in (None, "") else f"http://{text}"


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


# Cap on how many running timers we shape into an attribute. The app already
# caps timers.active at 20; this is a belt-and-suspenders guard so a
# misbehaving or newer server can never flood the entity attributes.
_MAX_ACTIVE_TIMERS = 20

# Home Assistant rejects a state string longer than 255 characters, so the
# "Timers" summary sensor is clipped to fit.
_MAX_STATE_LENGTH = 255


def active_timers(data: Any) -> list[dict[str, Any]]:
    """Shape ``timers.active`` into a clean attribute list.

    Returns ``[{"label", "remaining_seconds", "expired"}, ...]``. Older servers
    omit ``timers.active`` entirely, which degrades to an empty list rather than
    raising. Anything that is not a list or whose rows are not dicts is dropped,
    and the result is capped so the attribute stays small.
    """

    timers = (data or {}).get("timers") or {}
    active = timers.get("active")
    if not isinstance(active, list):
        return []
    shaped: list[dict[str, Any]] = []
    for row in active:
        if not isinstance(row, dict):
            continue
        shaped.append(
            {
                "label": row.get("label"),
                "remaining_seconds": row.get("remaining_seconds"),
                "expired": bool(row.get("expired", False)),
            }
        )
        if len(shaped) >= _MAX_ACTIVE_TIMERS:
            break
    return shaped


def timers_summary(active: Any) -> str:
    """Human string for the "Timers" sensor: "none" or a compact join.

    Example: ``"Pasta 7:42, Soft egg 2:10"``. Rows without a label are skipped.
    The result is clipped to Home Assistant's 255-character state limit.
    """

    rows = active if isinstance(active, list) else []
    parts: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = row.get("label")
        if not label:
            continue
        parts.append(f"{label} {format_timer_remaining(row.get('remaining_seconds'))}")
    if not parts:
        return "none"
    text = ", ".join(parts)
    if len(text) > _MAX_STATE_LENGTH:
        text = text[:_MAX_STATE_LENGTH]
    return text


def has_display(mode: Any, is_satellite: bool) -> bool:
    """True for an install that drives a physical screen.

    A plain server has no display of its own; an appliance (pi_hosted) and a
    bandit (pi_remote, whether added directly or discovered under a server) do,
    so the sleep/wake buttons belong only on those.
    """

    if is_satellite:
        return True
    return mode in ("pi_hosted", "pi_remote")


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
            "timers_summary",
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
    if key == "timers_summary":
        return timers_summary(active_timers(payload))
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


# The detection types the app's /events/camera-detect accepts. "visitor" is a
# doorbell button press. Kept here so the service validator, services.yaml,
# and a test all agree on one list.
DETECTION_TYPES: tuple[str, ...] = ("person", "vehicle", "animal", "visitor")

# The app clamps a pop-up to five minutes; mirror that so a service call never
# sends a value the install would have to reject.
MAX_POPUP_SECONDS = 300


def normalize_detection_type(value: Any) -> str:
    """Reduce a detection type to one the app accepts, or "".

    Case and surrounding whitespace are forgiven, anything else (an unknown
    word, None, a number) comes back empty so the caller can refuse the call
    with a clear message instead of posting junk.
    """

    if not isinstance(value, str):
        return ""
    cleaned = value.strip().lower()
    return cleaned if cleaned in DETECTION_TYPES else ""


def clamp_popup_seconds(value: Any) -> int:
    """Coerce a pop-up length to an int in 0..MAX_POPUP_SECONDS.

    0 means "use the install's configured default". Bad input (None, text,
    a negative number) degrades to 0 rather than raising, so a sloppy service
    call still pops the camera up for the default length.
    """

    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return 0
    if seconds < 0:
        return 0
    return min(seconds, MAX_POPUP_SECONDS)


def match_install(target_id: Any, main_id: Any, satellite_ids: Any) -> str | None:
    """Which install of one config entry a targeted device id refers to.

    Returns "main" for the primary install, the satellite's own device id for
    a bandit, or None when the id belongs to neither (so the caller can try
    the next config entry). Pure so it is unit-testable without Home
    Assistant: the service handler feeds it the ids it pulled from the device
    registry and the runtime data.
    """

    if not target_id:
        return None
    target = str(target_id)
    if main_id is not None and target == str(main_id):
        return "main"
    for sat_id in satellite_ids or []:
        if target == str(sat_id):
            return target
    return None


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


# --- Home Assistant account and token handover --------------------------------
# Every decision about which account and token an install gets lives here, as
# pure functions over the install's /ha/state payload and the entry's options,
# so it can be tested without Home Assistant. __init__.py only carries out the
# plan. The option keys and issue ids repeat const.py (which imports Home
# Assistant, so this module cannot); a test keeps the two in step.

OPT_CONNECT_DONE = "connect_done"
OPT_TOKEN_ID = "ha_token_id"
OPT_TOKEN_CREATED = "ha_token_created"
OPT_TOKEN_KIND = "ha_token_kind"
OPT_OWNER_TOKEN_ID = "ha_owner_token_id"
OPT_REMOVED_REFS = "ha_removed_refs"

ROTATE_AFTER_DAYS = 365
# How many removed tokens an entry remembers (see token_ref_for_id).
MAX_REMOVED_REFS = 8

ISSUE_APP_UPDATE = "app_update_needed"
ISSUE_OPEN_GRACE = "open_install_grace"
ISSUE_OPEN_NAME = "open_install_name"
ISSUE_MANUAL_TOKEN = "manual_token"

GROUP_CONTROL = "system-users"
GROUP_READ_ONLY = "system-read-only"

_MAX_CLIENT_HEADER = 64


def client_header(version: Any) -> str:
    """The X-PR-Client value this integration sends: 'ha-component/<version>'."""
    text = "ha-component/" + (str(version).strip() if version else "unknown")
    cleaned = "".join(ch for ch in text if 32 < ord(ch) < 127)
    return cleaned[:_MAX_CLIENT_HEADER]


def _features(state: Any) -> list:
    features = (state or {}).get("features") if isinstance(state, dict) else None
    return features if isinstance(features, list) else []


def _ha_access(state: Any) -> dict:
    block = (state or {}).get("ha_access") if isinstance(state, dict) else None
    return block if isinstance(block, dict) else {}


def dedicated_supported(state: Any) -> bool:
    """True when the install can take a limited account's token: it stores
    the token kind and receives probe alerts through this integration."""
    features = _features(state)
    return "ha_token_dedicated" in features and "probe_alert_service" in features


def desired_group(needs: Any) -> str:
    """The Home Assistant group the limited account belongs in: allowed to
    control devices only when one of the install's keys does that."""
    return GROUP_CONTROL if "control" in (needs or []) else GROUP_READ_ONLY


def local_only_for(used_internal_url: Any) -> bool:
    """A limited account may be kept to the local network only when the
    install was handed Home Assistant's own local address."""
    return used_internal_url is True


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def rotation_due(created_iso: Any, now_iso: Any) -> bool:
    """True when the limited account's token should be replaced: its
    creation time is missing or unreadable, or it is a year old."""
    created = _parse_time(created_iso)
    if created is None:
        return True
    now = _parse_time(now_iso)
    if now is None:
        return False
    return (now - created).total_seconds() >= ROTATE_AFTER_DAYS * 86400


def owner_token_name(title: Any, entry_id: Any) -> str:
    """Name of the owner token one entry hands to an older install. Unique
    per entry, so one entry never touches another entry's token."""
    return f"Pantry Raider ({title or 'install'} {str(entry_id or '')[:6]})"


def dedicated_user_name(title: Any) -> str:
    """Name of the limited Home Assistant account made for one install."""
    return f"Pantry Raider ({title or 'install'})"


def dedicated_token_name(gen: Any) -> str:
    """Name of the limited account's token. Home Assistant refuses two
    long-lived tokens with one name on one account, so each gets a number."""
    return f"Pantry Raider {gen}"


def token_ref_for_id(token_id: Any) -> str:
    """The short digest an install reports for the token it holds
    (ha_access.token_ref): sha256 of the refresh token's id, first 16 hex
    characters. Neither side ever shows the token or the id itself."""
    if not token_id:
        return ""
    return hashlib.sha256(str(token_id).encode("utf-8")).hexdigest()[:16]


def remember_removed_ref(refs: Any, token_id: Any) -> list:
    """The entry's list of removed-token digests with one more added (the
    newest last, at most MAX_REMOVED_REFS)."""
    out = [str(r) for r in (refs if isinstance(refs, list) else []) if r]
    ref = token_ref_for_id(token_id)
    if ref and ref not in out:
        out.append(ref)
    return out[-MAX_REMOVED_REFS:]


def pasted_by_hand(token: Any, own_ids: Any, shared_name: str) -> bool:
    """Whether the token an install holds is one somebody pasted into it by
    hand, rather than one this integration handed over. ``token`` is
    {'id', 'client_name', 'owner'} for the Home Assistant token whose
    token_ref the install reports, or None when no token matches. Before
    0.21 an install recorded no kind for either, so a pasted token looked
    like an old handover. Only the owner token older versions shared and
    the tokens this entry made count as handed over; anything else that
    matches was pasted, and the integration leaves it in place. None (no
    token here matches) is not treated as pasted: the install's link is to a
    token this Home Assistant does not have, so it gets a new one."""
    if not isinstance(token, dict) or not token.get("id"):
        return False
    if token.get("id") in {i for i in (own_ids or []) if i}:
        return False
    if token.get("owner") and token.get("client_name") == shared_name:
        return False
    return True


def legacy_owner_token_removable(entries: Any) -> bool:
    """Whether the owner token older versions shared between every install
    may go. ``entries`` describes every pantry_raider entry, loaded or not:
    [{'history': bool, 'token_kind': str}]. An entry with history once handed
    a token to its install; every one of those must now use a limited
    account, or an install still on the shared token would be cut off."""
    for entry in entries or []:
        if not isinstance(entry, dict) or not entry.get("history"):
            continue
        if entry.get("token_kind") != "dedicated":
            return False
    return True


def handover_plan(*, is_server: bool, connect_back: bool, state: Any,
                  options: Any, now_iso: str, used_internal_url: bool) -> dict:
    """What this entry should do about the install's Home Assistant token.

    Returns {'action', 'group', 'local_only', 'mint', 'rotate', 'repair'}
    with action one of:
      'none'            nothing to do;
      'owner'           an older install: hand it an owner token, once;
      'dedicated'       give it the limited account's token (mint says
                        whether a new one is needed now);
      'disconnect_own'  connect-back was turned off: take back only the
                        tokens this entry made.
    repair names the Repairs issue to raise, or None."""
    opts = options if isinstance(options, dict) else {}
    plan = {"action": "none", "group": None, "local_only": False,
            "mint": False, "rotate": False, "repair": None}
    if not is_server:
        return plan
    access = _ha_access(state)
    app_kind = str(access.get("token_kind") or "")
    if not connect_back:
        owns_tokens = bool(opts.get(OPT_TOKEN_ID) or opts.get(OPT_OWNER_TOKEN_ID))
        if owns_tokens and app_kind != "manual":
            plan["action"] = "disconnect_own"
        return plan
    supported = dedicated_supported(state)
    if supported and app_kind == "manual":
        # The owner pasted a token into Pantry Raider: leave it alone.
        plan["repair"] = ISSUE_MANUAL_TOKEN
        return plan
    if not supported:
        plan["action"] = "owner"
        # Once, as before; and again when this entry last handed over a
        # limited account's token: the install went back to a version that
        # cannot use one (it sends probe alerts the way only an
        # administrator may), so it gets an owner token named for this entry.
        plan["mint"] = (not opts.get(OPT_CONNECT_DONE)
                        or opts.get(OPT_TOKEN_KIND) == "dedicated")
        plan["repair"] = ISSUE_APP_UPDATE
        return plan
    needs = access.get("needs")
    if not isinstance(needs, list):
        needs = ["read", "control"]
    has_dedicated = bool(opts.get(OPT_TOKEN_ID)) and opts.get(OPT_TOKEN_KIND) == "dedicated"
    # The install still holds a token this entry has removed: a handover
    # whose reply timed out after the install saved the token, or a backup
    # restored from before a yearly replacement. Its Home Assistant link is
    # dead, so it gets a new token.
    app_ref = str(access.get("token_ref") or "")
    removed = opts.get(OPT_REMOVED_REFS)
    holds_removed = bool(app_ref) and isinstance(removed, list) and app_ref in removed
    mint = (not has_dedicated
            or rotation_due(opts.get(OPT_TOKEN_CREATED), now_iso)
            or app_kind != "dedicated"
            or holds_removed)
    plan.update(action="dedicated", group=desired_group(needs),
                local_only=local_only_for(used_internal_url), mint=bool(mint),
                rotate=bool(mint and has_dedicated))
    return plan


def pairing_result(payload: Any) -> str:
    """'approved', 'approved_no_key', 'pending', 'denied' or 'expired' for a
    pairing status answer. An approval without the key means the key was
    already handed out; the install repeats it for a short while, so the
    caller asks again."""
    data = payload if isinstance(payload, dict) else {}
    status = data.get("status")
    if status == "approved":
        return "approved" if data.get("api_key") else "approved_no_key"
    if status in ("pending", "denied"):
        return status
    return "expired"


def auth_failure_action(is_satellite: bool) -> str:
    """'reauth' when the main install refuses the key (Home Assistant asks
    the user to reconnect), 'warn' for a bandit (its entities go unavailable)."""
    return "warn" if is_satellite else "reauth"


def _auth(state: Any) -> dict:
    block = (state or {}).get("auth") if isinstance(state, dict) else None
    return block if isinstance(block, dict) else {}


def grace_issue_placeholders(state: Any) -> dict | None:
    """{'date': ...} while an install without a password is counting down to
    refusing outside requests, else None."""
    until = _auth(state).get("grace_until")
    if not until:
        return None
    when = _parse_time(until)
    return {"date": when.date().isoformat() if when else str(until)}


def grace_issue_id(state: Any) -> str | None:
    """Which countdown issue to show: an unknown name needs the name added,
    anything else needs two-factor and a password. None without a countdown."""
    if grace_issue_placeholders(state) is None:
        return None
    if _auth(state).get("grace_kind") == "name":
        return ISSUE_OPEN_NAME
    return ISSUE_OPEN_GRACE


def probe_alert_event_data(data: Any) -> dict:
    """The event payload for a probe_alert service call: exactly what the
    install sent, so existing automations see the same event as before."""
    return dict(data or {})
