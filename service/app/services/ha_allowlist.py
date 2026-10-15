"""The Home Assistant calls and entities this install has set up.

ha_policy decides whether one call may run. This module answers the
questions around it from the saved settings: which (entity, service) pairs
the owner's keys actually make, which entities the app reads, whether any
key controls anything at all, and whether the key policy is switched on for
this install yet. The HA proxy, the Home Assistant client, the deck config
and the Settings save all read from here, so they agree on one answer.

Everything here reads settings and the resolvers in start_actions; nothing
talks to Home Assistant.
"""
from __future__ import annotations

import copy
import json

from . import ha_policy

_VERDICT_RANK = {"ok": 0, "elevated": 1, "invalid": 2}


def _dict_slots(slots) -> list:
    return [s for s in (slots or []) if isinstance(s, dict)]


def _slot_position(entry: dict, slots) -> int:
    """Index of a slot dict among the dict slots (identity first, then
    equality), or -1 when the entry is not one of the slots."""
    dslots = _dict_slots(slots)
    for i, s in enumerate(dslots):
        if s is entry:
            return i
    for i, s in enumerate(dslots):
        if s == entry:
            return i
    return -1


def _macro_children(entry: dict) -> list:
    raw = entry.get("actions", [])
    if isinstance(raw, str):
        return [n.strip() for n in raw.split(",") if n.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(n).strip() for n in raw if str(n).strip()]
    return []


def _owned_calls(entry, slots) -> list:
    """[(entity_id, service, owner)] for an entry. The owner is the entry
    whose allow_elevated governs the call: the entry itself, or for a macro
    child the slot dict it names (the same entry a press passes)."""
    from . import start_actions
    if not isinstance(entry, dict):
        return []
    if "type" in entry:
        otype = str(entry.get("type") or "")
        call = None
        if otype == "ha_action":
            call = start_actions.resolve_ha_call(entry)
        elif otype == "media":
            call = start_actions.resolve_media_call(entry)
        elif otype == "macro":
            out = []
            dslots = _dict_slots(slots)
            for name in _macro_children(entry):
                slot_call = start_actions.resolve_ha_slot(name, slots)
                if slot_call:
                    idx = int(name.rsplit("_", 1)[1]) - 1
                    out.append((slot_call[0], slot_call[1], dslots[idx]))
            return out
        return [(call[0], call[1], entry)] if call else []
    pos = _slot_position(entry, slots)
    if pos < 0:
        return []
    call = start_actions.resolve_ha_slot(f"ha_{pos + 1}", slots)
    return [(call[0], call[1], entry)] if call else []


def owned_calls(entry, slots) -> list:
    """[(entity_id_string, service, owner)]: entry_calls plus, for each
    call, the entry whose allow_elevated governs it when the key is pressed.
    That is the entry itself, except that a macro's ha_N child is governed
    by the slot dict it names. Pass the owner to ha_policy.permitted."""
    return list(_owned_calls(entry, slots))


def entry_calls(entry, slots) -> list:
    """[(entity_id_string, service)] an override or a slot dict makes.

    ha_action and media overrides resolve the way a press does, a macro
    gives the calls of its ha_N children, and a slot dict from
    streamdeck_ha_slots resolves at its own position. Anything else gives
    []. The pairs are as saved; callers normalize."""
    return [(e, s) for e, s, _owner in _owned_calls(entry, slots)]


def _prefer(out: dict, key: tuple, entry) -> None:
    current = out.get(key)
    if current is None:
        out[key] = entry
    elif (not (isinstance(current, dict) and current.get("allow_elevated") is True)
          and isinstance(entry, dict) and entry.get("allow_elevated") is True):
        out[key] = entry


def configured_calls(settings, profiles) -> dict:
    """{(entity_id, service): entry} for every call the owner's keys make.

    Keys are single, normalized entity ids, so a multi-entity key adds one
    pair per entity. Sources: streamdeck_key_overrides, each profile's
    key_overrides and streamdeck_ha_slots. Calls ha_policy finds invalid are
    left out, since they never run. When several entries make the same
    pair, one with allow_elevated True wins."""
    slots = getattr(settings, "streamdeck_ha_slots", None) or []
    entries = list(getattr(settings, "streamdeck_key_overrides", None) or [])
    for profile in profiles or []:
        if isinstance(profile, dict):
            overrides = profile.get("key_overrides")
            if isinstance(overrides, list):
                entries.extend(overrides)
    entries.extend(_dict_slots(slots))
    out: dict = {}
    for entry in entries:
        for entity_id, service, owner in _owned_calls(entry, slots):
            if ha_policy.classify(entity_id, service)[0] == "invalid":
                continue
            _ent, svc = ha_policy.normalize(entity_id, service)
            for single in ha_policy.split_entities(entity_id):
                _prefer(out, (single, svc), owner)
    return out


def _valid_id(value) -> str:
    """The normalized entity id, or '' when it is not a well-formed id."""
    ent = str(value or "").strip().lower()
    return ent if ha_policy.ID_RE.fullmatch(ent) else ""


def camera_entities(settings) -> set:
    """The Home Assistant camera entities the configured cameras show."""
    from . import cameras
    out = set()
    for cam in getattr(settings, "streamdeck_cameras", None) or []:
        try:
            ent = _valid_id(cameras.resolve_ha_entity(cam)[0])
        except Exception:  # noqa: BLE001 - a bad camera row is skipped
            ent = ""
        if ent:
            out.add(ent)
    return out


def sensor_entities(settings) -> set:
    """The thermometer and hygrometer entities, sensors only.

    Anything that can open a kitchen page can add a thermometer, so these
    are not proof the owner chose them: only the sensor domain counts, and a
    reading passed on through the Home Assistant hop must look like a
    temperature or humidity (reading_needs_sensor_check)."""
    out = set()
    for value in getattr(settings, "gadget_ha_entities", None) or []:
        ent = _valid_id(value)
        if ent.startswith("sensor."):
            out.add(ent)
    try:
        from . import gadgets_ha
        pairs = gadgets_ha.configured_hygro_pairs()
    except Exception:  # noqa: BLE001 - no hygrometers is the safe answer
        pairs = []
    for pair in pairs:
        for field in ("temperature", "humidity"):
            ent = _valid_id(pair.get(field) if isinstance(pair, dict) else "")
            if ent.startswith("sensor."):
                out.add(ent)
    return out


def readable_entities(settings, profiles) -> set:
    """Entities whose state the app may read: the ones its keys call, the
    camera entities, and the thermometer and hygrometer sensors."""
    out = {entity for entity, _svc in configured_calls(settings, profiles)}
    out |= camera_entities(settings)
    out |= sensor_entities(settings)
    return out


def reading_needs_sensor_check(entity: str, settings, profiles) -> bool:
    """True when an entity is readable only because a thermometer or
    hygrometer lists it, so its state is passed on only when it looks like a
    temperature or humidity reading."""
    if entity not in sensor_entities(settings):
        return False
    keyed = {e for e, _svc in configured_calls(settings, profiles)}
    return entity not in keyed and entity not in camera_entities(settings)


def slot_review(slots) -> list:
    """The older HA 1 to HA 5 keys that need the owner's decision, for
    Settings, Start Page: [{position, name, entity_id, service, verdict,
    allowed, grandfathered}]. verdict is 'elevated' (it unlocks, opens or
    disarms something, and runs only while allowed) or 'invalid' (it never
    runs); position counts the saved slots, the way ha_1..ha_5 do."""
    out = []
    dslots = _dict_slots(slots)
    for pos, slot in enumerate(dslots):
        own = [(e, s) for e, s, owner in _owned_calls(slot, dslots) if owner is slot]
        if not own:
            continue
        verdicts = [ha_policy.classify(e, s)[0] for e, s in own]
        if "invalid" in verdicts or slot.get("disabled_reason") == "invalid":
            verdict = "invalid"
        elif "elevated" in verdicts:
            verdict = "elevated"
        else:
            continue
        ent, svc = ha_policy.normalize(*own[0])
        out.append({
            "position": pos,
            "name": (str(slot.get("label") or "").strip() or f"HA {pos + 1}")[:60],
            "entity_id": ent, "service": svc, "verdict": verdict,
            "allowed": slot.get("allow_elevated") is True,
            "grandfathered": bool(slot.get("elevated_grandfathered")),
        })
    return out


def needs_control(settings, profiles) -> bool:
    """True when any key calls a Home Assistant service."""
    return bool(configured_calls(settings, profiles))


def load_profiles(db) -> list:
    """[{'name', 'key_overrides'}] from the saved Stream Deck profiles.
    Rows whose key_overrides is not a JSON list are skipped."""
    from ..models.db_models import StreamDeckProfile
    out = []
    for row in db.query(StreamDeckProfile).all():
        try:
            overrides = json.loads(row.key_overrides or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(overrides, list):
            continue
        out.append({"name": row.name, "key_overrides": overrides})
    return out


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _is_satellite(settings) -> bool:
    fn = getattr(settings, "is_satellite", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001 - fall back to the raw field
            pass
    return getattr(settings, "deployment_mode", "") == "pi_remote"


def policy_enforced(settings) -> bool:
    """Whether elevated calls need the owner's permission on this install.

    A satellite follows its server: on once a verified pull carried
    ha_policy_version 1 or later. Any other install turns it on when its
    one-time key migration has run. Until then only invalid calls and
    elevated Bluetooth presses are refused."""
    if _is_satellite(settings):
        return _int(getattr(settings, "upstream_ha_policy_version", 0)) >= 1
    return _int(getattr(settings, "ha_key_policy_version", 0)) >= 1


def _judge(entry, slots) -> dict:
    calls = []
    verdict, reason = "ok", ""
    allowed, needs_review, message = True, False, ""
    review_calls = []
    for entity_id, service, owner in _owned_calls(entry, slots):
        v, r = ha_policy.classify(entity_id, service)
        ent, svc = ha_policy.normalize(entity_id, service)
        calls.append({"entity_id": ent, "service": svc, "verdict": v, "reason": r})
        if _VERDICT_RANK[v] > _VERDICT_RANK[verdict]:
            verdict, reason = v, r
        elif v == verdict == "elevated" and r == "high_risk":
            reason = r
        ok, msg = ha_policy.permitted(entity_id, service, owner, "screen", enforce=True)
        if not ok:
            allowed = False
            message = message or msg
            if v == "elevated":
                needs_review = True
                review_calls.append((ent, svc, msg))
    if verdict == "invalid":
        # The key cannot run until it is fixed; say that, not that it needs
        # permission.
        needs_review, message, review_calls = False, ha_policy.MSG_INVALID, []
    return {"verdict": verdict, "reason": reason, "allowed": allowed,
            "needs_review": needs_review, "message": message, "calls": calls,
            "_review": review_calls}


def describe_entry(entry, slots) -> dict:
    """How the key policy sees one override or slot.

    {'verdict', 'reason', 'allowed', 'needs_review', 'message', 'calls'}:
    the verdict is the worst over the entry's calls, allowed says whether a
    press would run with the policy on, and calls lists each normalized
    (entity_id, service) with its own verdict. An entry with no Home
    Assistant calls is ok."""
    out = _judge(entry, slots)
    out.pop("_review")
    return out


def _entry_ref(entry: dict, index: int) -> dict:
    return {
        "id": str(entry.get("id") or ""),
        "slot": entry.get("slot", index),
        "label": str(entry.get("label") or entry.get("id") or ""),
    }


def review_entries(entries, slots) -> tuple:
    """(entries_out, errors, review) for a list of overrides or slots.

    entries_out is a deep copy; no entry is ever dropped. An entry with an
    invalid call is listed in errors. An elevated call the entry is not
    allowed to make sets needs_review on it and is listed in review; an
    entry that is fine (or allowed) loses any stale needs_review. Entries
    without Home Assistant calls pass untouched. A slot without a 'slot'
    field is identified by its position in the list."""
    src = list(entries or [])
    out = copy.deepcopy(src)
    errors, review = [], []
    for i, entry in enumerate(src):
        if not isinstance(entry, dict):
            continue
        # Resolve on the original objects so a slot finds its own position.
        judged = _judge(entry, slots)
        if not judged["calls"]:
            continue
        ref = _entry_ref(entry, i)
        if judged["verdict"] == "invalid":
            errors.append(dict(ref, message=ha_policy.MSG_INVALID))
            continue
        if judged["needs_review"]:
            out[i]["needs_review"] = True
            for ent, svc, msg in judged["_review"]:
                review.append(dict(ref, entity_id=ent, service=svc, message=msg))
        else:
            out[i].pop("needs_review", None)
    return out, errors, review


def token_calls(token, settings) -> list:
    """[(entity_id, service, entry)] a Start Page or button token makes.

    An ha_N slot token resolves through its slot, a custom key id through
    its override (a macro child carries its slot as the entry), and any
    other token, such as a timer, gives []."""
    from . import start_actions
    name = str(token or "").strip()
    slots = getattr(settings, "streamdeck_ha_slots", None) or []
    call = start_actions.resolve_ha_slot(name, slots)
    if call:
        idx = int(name.rsplit("_", 1)[1]) - 1
        return [(call[0], call[1], _dict_slots(slots)[idx])]
    # Timer keys win over a custom key of the same id, as they do on a press.
    timer_tokens = set(getattr(start_actions, "_CYCLE_TIMER_LABELS", {}) or {})
    timer_tokens |= set(getattr(start_actions, "MACRO_TIMER_PRESETS", {}) or {})
    if name in timer_tokens:
        return []
    override = start_actions.find_override(
        name, getattr(settings, "streamdeck_key_overrides", None) or [])
    if not override:
        return []
    return _owned_calls(override, slots)
