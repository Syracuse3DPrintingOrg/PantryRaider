"""Access settings and trust actions (/setup/security, admin surface).

These settings are saved only here, never through /setup/save: the home
network ranges, other names for this kitchen, which keyless device families
need a key on an install with no password, whether the kitchen screen can
open Settings, the key mirror, automatic retirement of older keys and
whether older satellites still receive service keys.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..config import settings
from ..services import access, auth_state, credentials, lan, local_agents

router = APIRouter(prefix="/setup/security", tags=["security"])

_BOOL_FIELDS = ("kiosk_screen_is_admin", "legacy_mirror", "legacy_key_auto_retire")
_SAT_CHOICES = ("auto", "allow", "off")
# Where Home networks may be changed from: a device at home, this device
# itself, or Home Assistant's sidebar (the add-on, which is home too). Never
# through a proxy or from the internet, so a session from outside cannot
# widen what counts as home.
_HOME_NETWORK_EDITORS = ("lan_direct", "plain_loopback", "ingress")
_AGENT_LABELS = {"screen": "The kitchen screen", "streamdeck": "The Stream Deck",
                 "gadgets": "The sensor reader"}


def _satellites_needing_update() -> list[dict]:
    out = []
    try:
        from ..services import devices
        rows = devices.list_devices()
    except Exception:
        return out
    cutoff = 30
    for d in rows:
        if not isinstance(d, dict) or d.get("deployment_mode") != "pi_remote":
            continue
        if d.get("relay"):
            continue
        seen = auth_state.parse_ts(d.get("last_seen"))
        if seen is None:
            continue
        if time.time() - seen > cutoff * 86400:
            continue
        out.append({"name": d.get("label") or d.get("hostname") or d.get("device_id") or "",
                    "version": d.get("version") or "", "last_seen": d.get("last_seen")})
    return out


def _programs() -> dict:
    hw = local_agents.last_hardware_status() or {}

    def _inst(key):
        node = hw.get(key) if isinstance(hw, dict) else None
        if isinstance(node, dict) and "installed" in node:
            return bool(node.get("installed"))
        return None

    doc = auth_state.load()
    other = (doc.get("legacy_use") or {}).get("other") or {}
    return {
        "bridge": local_agents.bridge_present(),
        "screen": {"installed": _inst("kiosk"), "proven": local_agents.screen_proven()},
        "streamdeck": {"installed": _inst("streamdeck"), "proven": bool(local_agents.deck_proven(days=7))},
        "gadgets": {"installed": _inst("gadgets"), "proven": local_agents.gadgets_proven()},
        "other_callers": {"last": other.get("last"), "count": other.get("count", 0),
                          "ua": other.get("ua", "")},
        "loopback": {k: auth_state.loopback_trust().get(k)
                     for k in ("state", "since", "by", "retrust_until", "confirm_needed")},
    }


_PANE_COPY = ("TWO_FACTOR_NEXT_MSG", "OUTSIDE_ORDER_MSG", "KEYLESS_DEVICES_WARNING",
              "KEYLESS_REFUSED_WARNING", "SAME_HOST_PROXY_WARNING", "LOCAL_TRUST_ON",
              "LOCAL_TRUST_OFF", "LOCAL_TRUST_WAITING", "LOCAL_TRUST_CONFIRM",
              "LAN_CIDRS_REMOTE_MSG", "KEY_SHOWN_ONCE", "OPEN_NOTICE")


def _pane_copy() -> dict:
    out = {name: getattr(access, name) for name in _PANE_COPY}
    out["SCOPE_LABELS"] = dict(access.SCOPE_LABELS)
    out["FAMILY_LABELS"] = dict(access.FAMILY_LABELS)
    return out


@router.get("/state")
async def security_state(request: Request):
    p = access.principal(request)
    required = access.effective_required_families(settings, auth_state)
    doc = auth_state.load()
    keyless = doc.get("keyless_families") or {}
    seen, other_count = auth_state.open_sightings()
    kiosk = local_agents.kiosk_url_status()
    password_set = bool(settings.auth_password)
    return {
        "ok": True,
        "open": not password_set,
        "origin": p.origin,
        # Whether a password sign-in from where this page was opened would
        # need two-factor (the sign-in page's own rule).
        "sign_in_outside": access.sign_in_from_outside(request),
        "lan_cidrs": settings.lan_cidrs,
        "lan_cidrs_editable": p.origin in _HOME_NETWORK_EDITORS,
        "home_host_names": settings.home_host_names,
        "families": {f: {"required": bool(required.get(f)), "label": access.FAMILY_LABELS[f],
                         "keyless": keyless.get(f) or None}
                     for f in access.FAMILIES},
        "kiosk_screen_is_admin": bool(settings.kiosk_screen_is_admin),
        "legacy_mirror": bool(settings.legacy_mirror),
        "legacy_key_auto_retire": bool(settings.legacy_key_auto_retire),
        "satellite_legacy_credentials": settings.satellite_legacy_credentials,
        "satellites_needing_update": _satellites_needing_update(),
        "local_programs": _programs(),
        "open_mode": {"grace_until": auth_state.grace_until() if auth_state.grace_active() else None,
                      "seen": seen, "other_count": other_count},
        "keyless_ha": doc.get("keyless_ha_callers") or None,
        "gateway_hint": access.gateway_hint() if auth_state.gateway_peer_recent() else "",
        "password_set": password_set,
        "two_factor_on": bool(settings.local_2fa_active()),
        "kiosk_url_warning": (access.KIOSK_URL_WARNING.format(host=kiosk["host"])
                              if kiosk.get("local") is False else ""),
        "keyless_devices": auth_state.keyless_device_labels(30),
        "keyless_refused": auth_state.keyless_device_labels(
            7, since=auth_state.password_set_at()) if password_set else [],
        "same_host_proxy": auth_state.same_host_proxy_recent(),
        "copy": _pane_copy(),
    }


@router.post("/settings")
async def save_security_settings(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "errors": {"": "Send the settings as JSON."}},
                            status_code=400)
    p = access.principal(request)
    errors: dict = {}
    changes: dict = {}
    for name in _BOOL_FIELDS:
        if name in body:
            if isinstance(body[name], bool):
                changes[name] = body[name]
            else:
                errors[name] = "Choose on or off."
    if "satellite_legacy_credentials" in body:
        value = body["satellite_legacy_credentials"]
        if value in _SAT_CHOICES:
            changes["satellite_legacy_credentials"] = value
        else:
            errors["satellite_legacy_credentials"] = "Choose Auto, Always or Never."
    if "lan_cidrs" in body:
        value = body["lan_cidrs"]
        if not isinstance(value, str):
            errors["lan_cidrs"] = "List the networks as text."
        else:
            _nets, rejected = lan.parse_cidrs(value)
            if rejected:
                errors["lan_cidrs"] = "These are not home networks this kitchen can use: " + ", ".join(rejected[:10])
            else:
                changes["lan_cidrs"] = value.strip()
    if "home_host_names" in body:
        value = body["home_host_names"]
        if not isinstance(value, str):
            errors["home_host_names"] = "List the names as text."
        else:
            _names, rejected = lan.parse_host_names(value)
            if rejected:
                errors["home_host_names"] = "These are not names this kitchen can use: " + ", ".join(rejected[:10])
            else:
                changes["home_host_names"] = value.strip()
    if "require_key_families" in body:
        value = body["require_key_families"]
        if not isinstance(value, dict) or any(
                k not in access.FAMILIES or not isinstance(v, bool) for k, v in value.items()):
            bad = ", ".join(str(k) for k in (value or {}) if k not in access.FAMILIES) \
                if isinstance(value, dict) else ""
            errors["require_key_families"] = ("Unknown device kinds: " + bad) if bad else "Choose on or off for each kind of device."
        else:
            stored = settings.require_key_families if isinstance(settings.require_key_families, dict) else {}
            changes["require_key_families"] = {**stored, **value}
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    if "lan_cidrs" in changes and p.origin not in _HOME_NETWORK_EDITORS:
        return JSONResponse({"ok": False, "errors": {"lan_cidrs": access.LAN_CIDRS_REMOTE_MSG},
                             "error": access.LAN_CIDRS_REMOTE_MSG}, status_code=403)
    if changes:
        settings.save(changes)
        access.clear_name_cache()
        if changes.get("legacy_mirror") is False:
            credentials.remove_all_mirrors()
    return {"ok": True, "saved": sorted(changes)}


@router.post("/local-trust")
async def local_trust(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = (body or {}).get("action") if isinstance(body, dict) else None
    if action == "retire":
        auth_state.retire("admin")
    elif action == "retrust":
        auth_state.retrust(7)
    else:
        return JSONResponse({"ok": False, "error": "Choose retire or retrust."}, status_code=400)
    warnings = [f"{_AGENT_LABELS[a]} has not signed in with its own key yet."
                for a in local_agents.unproven_agents()]
    return {"ok": True, "loopback": auth_state.loopback_trust(), "warnings": warnings}


@router.post("/signout-everywhere")
async def signout_everywhere(request: Request):
    """Sign out every browser signed in to this kitchen except this one."""
    auth_state.sign_out_others(request.session)
    return {"ok": True}


@router.post("/kiosk-signout")
async def kiosk_signout():
    gen = auth_state.bump_kiosk_gen()
    return {"ok": True, "kiosk_gen": gen}
