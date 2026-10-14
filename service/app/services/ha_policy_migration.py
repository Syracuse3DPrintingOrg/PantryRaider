"""The one-time Home Assistant key policy migration (0.21).

From 0.21 a Home Assistant key that unlocks a door, disarms an alarm, opens a
garage or calls an action Pantry Raider does not know runs only after the
owner allows it on that key. Keys like that already work on existing
installs, and an update must never quietly stop a key the household uses, so
this runs once and:

- marks each existing key that makes such a call with allow_elevated (it
  keeps working) and elevated_grandfathered (the editor shows it was allowed
  during the update), and raises one inbox item listing them to confirm or
  remove;
- marks each key whose call is not valid with disabled_reason 'invalid' and
  raises one inbox item listing them;
- lists the Bluetooth button presses mapped to such a key in one inbox
  item. Those presses stop running at once (gadgets_buttons pauses them at
  press time); the mapping itself is never rewritten.

Nothing is deleted and no entity or service is changed. Every settings change
goes out in one settings.save that also sets ha_key_policy_version to 1, which
is what turns the policy on (ha_allowlist.policy_enforced). Any failure
propagates before that save, so the version stays 0 and the policy stays off
until the next start tries again.

On a satellite the keys, the Home Assistant slots and the saved profiles come
from the main server and are left alone (the server migrates its own copies,
and the satellite follows the server's policy version); only the satellite's
own Bluetooth buttons are examined.
"""
from __future__ import annotations

import copy
import json
import logging

log = logging.getLogger(__name__)

POLICY_VERSION = 1

KIND_GRANDFATHERED = "ha_keys_grandfathered"
KIND_INVALID = "ha_keys_invalid"
KIND_BLE = "ble_buttons_paused"

DEDUPE_GRANDFATHERED = "ha-key-policy-v1-grandfathered"
DEDUPE_INVALID = "ha-key-policy-v1-invalid"
DEDUPE_BLE = "ha-key-policy-v1-ble"

TITLE_GRANDFATHERED = "Check the Home Assistant keys that control locks, alarms or doors"
BODY_GRANDFATHERED = ("These keys kept working after the update. Confirm or remove "
                      "each one in Settings, Start Page:")
TITLE_INVALID = "A Home Assistant key was turned off"
BODY_INVALID = ("Its action is not valid, so it no longer runs. Edit it in "
                "Settings, Start Page:")
# A key in a saved Stream Deck profile is edited by loading the profile, so
# those keys get their own sentence after the Start Page ones.
BODY_PROFILE_KEYS = ("Keys in a saved Stream Deck profile: load the profile under "
                     "Settings, Stream Deck, Saved profiles, change the key, then "
                     "save the profile again under the same name:")
TITLE_BLE = "Some Bluetooth button actions are paused"
BODY_BLE = ("Bluetooth buttons can't run actions that unlock, open or disarm, "
            "because anyone nearby could fake a press. Map these presses to "
            "something else in Settings, Thermometers & Sensors:")

_PRESS_NAMES = {"single": "single press", "double": "double press", "long": "long press"}

# The settings lists of keys this migration examines.
KEY_FIELDS = ("streamdeck_key_overrides", "streamdeck_ha_slots")


def _version(settings) -> int:
    try:
        return int(getattr(settings, "ha_key_policy_version", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _is_satellite(settings) -> bool:
    fn = getattr(settings, "is_satellite", None)
    if callable(fn):
        return bool(fn())
    return getattr(settings, "deployment_mode", "") == "pi_remote"


def _entry_name(entry: dict, index: int, field: str) -> str:
    label = str(entry.get("label") or entry.get("id") or "").strip()
    if label:
        return label
    if field == "streamdeck_ha_slots":
        return f"ha_{index + 1}"
    return f"Key {index + 1}"


def _mark_entries(entries: list, slots: list, field: str,
                  grandfathered: list, invalid: list, *, profile: str = "") -> bool:
    """Mark the entries of one key list in place and add a line per marked
    call to grandfathered or invalid. Returns True when any entry changed.
    Each call is judged on the entry that governs it, so a macro's Home
    Assistant child is marked on its slot, not on the macro."""
    from . import ha_allowlist, ha_policy
    changed = False
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        own = [(ent, svc) for ent, svc, owner in ha_allowlist.owned_calls(entry, slots)
               if owner is entry]
        if not own:
            continue
        name = _entry_name(entry, index, field)
        if profile:
            name = f"{name} (profile {profile})"
        verdicts = [(ha_policy.classify(ent, svc)[0], ent, svc) for ent, svc in own]
        bad = [(ent, svc) for v, ent, svc in verdicts if v == "invalid"]
        if bad:
            if entry.get("disabled_reason") != "invalid":
                entry["disabled_reason"] = "invalid"
                changed = True
            ent, svc = bad[0]
            invalid.append(f"{name}: {str(ent).strip()} ({str(svc).strip()})")
            continue
        elevated = [(ent, svc) for v, ent, svc in verdicts if v == "elevated"]
        if not elevated:
            continue
        if entry.get("allow_elevated") is True and not entry.get("elevated_grandfathered"):
            continue  # the owner allowed it already
        if not (entry.get("allow_elevated") is True and entry.get("elevated_grandfathered")):
            # A key marked by an earlier, interrupted run is listed again but
            # left as it is.
            entry["allow_elevated"] = True
            entry["elevated_grandfathered"] = True
            changed = True
        for ent, svc in elevated:
            n_ent, n_svc = ha_policy.normalize(ent, svc)
            grandfathered.append(f"{name}: {n_ent} ({n_svc})")
    return changed


def _paused_presses(settings) -> list:
    """'<button name>, <press type>: <token>' for each Bluetooth press mapped
    to an elevated or invalid Home Assistant call."""
    from . import gadgets_buttons
    out = []
    for dev in getattr(settings, "button_devices", None) or []:
        if not isinstance(dev, dict):
            continue
        name = str(dev.get("name") or dev.get("id") or "Button").strip()
        mappings = gadgets_buttons.normalize_mappings(dev.get("mappings"))
        for event_type, mapping in mappings.items():
            if gadgets_buttons.mapping_policy(mapping)["paused"]:
                press = _PRESS_NAMES.get(event_type, event_type)
                out.append(f"{name}, {press}: {mapping.get('token') or ''}")
    return out


def _item(db, kind: str, title: str, body: str, items: list, dedupe: str,
          profile_items: list | None = None) -> None:
    """One inbox item listing ``items`` after ``body``, and any profile keys
    after the sentence that says where those are edited."""
    from . import action_items
    profile_items = list(profile_items or [])
    if not items and not profile_items:
        return
    parts = []
    if items:
        parts.append(body + " " + "; ".join(items) + ".")
    if profile_items:
        parts.append(BODY_PROFILE_KEYS + " " + "; ".join(profile_items) + ".")
    action_items.create(db, kind, title, body=" ".join(parts),
                        dedupe_key=dedupe, level="warning",
                        payload={"keys": items + profile_items})


def migrate_once(db) -> dict:
    """Run the migration if it has not run on this install yet.

    Returns {'skipped': True} when it already ran, else {'skipped': False,
    'grandfathered': n, 'invalid': n, 'ble_paused': n}. Exceptions propagate
    (the caller logs them) and leave ha_key_policy_version at 0."""
    from ..config import SATELLITE_PULL_FIELDS, settings
    from ..models.db_models import StreamDeckProfile

    if _version(settings) >= POLICY_VERSION:
        return {"skipped": True}

    satellite = _is_satellite(settings)
    grandfathered: list = []
    invalid: list = []
    changes: dict = {}

    # The slots resolve every slot key and macro child; work on one copy so
    # a slot is found at its own position by identity.
    slots = copy.deepcopy(list(getattr(settings, "streamdeck_ha_slots", None) or []))
    for field in KEY_FIELDS:
        if satellite and field in SATELLITE_PULL_FIELDS:
            continue
        if field == "streamdeck_ha_slots":
            entries = slots
        else:
            entries = copy.deepcopy(list(getattr(settings, field, None) or []))
        if _mark_entries(entries, slots, field, grandfathered, invalid):
            changes[field] = entries

    changed_rows = []
    profile_grandfathered: list = []
    profile_invalid: list = []
    if not satellite:
        for row in db.query(StreamDeckProfile).all():
            try:
                overrides = json.loads(row.key_overrides or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(overrides, list):
                continue
            if _mark_entries(overrides, slots, "profile", profile_grandfathered,
                             profile_invalid, profile=str(row.name or "")):
                changed_rows.append((row, overrides))

    ble = _paused_presses(settings)

    for row, overrides in changed_rows:
        row.key_overrides = json.dumps(overrides)
    if changed_rows:
        db.commit()

    _item(db, KIND_GRANDFATHERED, TITLE_GRANDFATHERED, BODY_GRANDFATHERED,
          grandfathered, DEDUPE_GRANDFATHERED, profile_grandfathered)
    _item(db, KIND_INVALID, TITLE_INVALID, BODY_INVALID, invalid, DEDUPE_INVALID,
          profile_invalid)
    _item(db, KIND_BLE, TITLE_BLE, BODY_BLE, ble, DEDUPE_BLE)

    changes["ha_key_policy_version"] = POLICY_VERSION
    settings.save(changes)
    result = {"skipped": False,
              "grandfathered": len(grandfathered) + len(profile_grandfathered),
              "invalid": len(invalid) + len(profile_invalid), "ble_paused": len(ble)}
    log.info("Home Assistant key policy migration: %s", result)
    return result
