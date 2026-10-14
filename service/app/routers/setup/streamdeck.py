"""Stream Deck configuration and the Home Assistant key slot review."""

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...config import settings
from ...services.bridge import bridge_client
from ._common import router, _HOST_BRIDGE, _ha_slot_review


@router.get("/streamdeck/config")
async def streamdeck_config_get():
    """Proxy GET config from host bridge.

    On a satellite the Stream Deck weather is owned by the main server, so the
    returned config's weather fields are overlaid with the synced settings. That
    keeps the setup UI showing the server's location/units (rendered read-only)
    even if the local config.toml still holds older values.

    The page never gets the deck's secrets back, in any deployment mode: the
    Home Assistant token and the deck's own app key come back "" (with
    ha_token_set and api_key_set) and a camera snapshot URL that carries a
    login comes back "" (with relay). key_overrides are the saved settings
    (the source of truth, never the policy-filtered copy the deck holds), with
    key_policy saying how the key policy sees each one.
    """
    from ...services.deck_config import redact_deck_config
    if _HOST_BRIDGE:
        try:
            async with bridge_client(timeout=3.0) as c:
                r = await c.get(f"{_HOST_BRIDGE}/streamdeck/config")
                content = r.json()
                if r.status_code == 200 and settings.is_satellite() and isinstance(content.get("config"), dict):
                    content["config"]["weather_location"] = settings.streamdeck_weather_location
                    content["config"]["weather_units"] = settings.streamdeck_weather_units
                if isinstance(content, dict) and isinstance(content.get("config"), dict):
                    content["config"] = redact_deck_config(content["config"])
                    if r.status_code == 200:
                        import copy as _copy
                        overrides = _copy.deepcopy(list(settings.streamdeck_key_overrides or []))
                        content["config"]["key_overrides"] = overrides
                        content["config"]["key_policy"] = _describe_overrides(overrides)
                return JSONResponse(status_code=r.status_code, content=content)
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
    return JSONResponse(status_code=500, content={"ok": False, "error": "bridge unavailable"})


def _describe_overrides(overrides: list) -> list:
    """How the key policy sees each saved override (the editor shows it)."""
    try:
        from ...services import ha_allowlist
        slots = settings.streamdeck_ha_slots or []
        return [ha_allowlist.describe_entry(o, slots) if isinstance(o, dict) else {}
                for o in overrides]
    except Exception:  # noqa: BLE001 - the grid still loads without verdicts
        return []


class HaSlotDecision(BaseModel):
    action: str = ""


_SLOT_ACTIONS = ("allow", "stop", "remove")


@router.post("/ha-slots/{position}")
async def ha_slot_decide(position: int, body: HaSlotDecision):
    """Allow, stop or remove one of the older HA 1 to HA 5 keys the key rules
    flagged. Allow lets a key that unlocks, opens or disarms something keep
    running; stop keeps the key but it no longer runs that call; remove
    clears its entity and action (the only choice for one that is not
    valid). Either way the key is no longer marked as allowed during the
    update."""
    if settings.is_satellite():
        return JSONResponse({"ok": False, "error": "These keys are managed on the main server."},
                            status_code=400)
    if body.action not in _SLOT_ACTIONS:
        return JSONResponse({"ok": False, "error": "Choose allow, stop or remove."},
                            status_code=400)
    item = next((r for r in _ha_slot_review() if r["position"] == position), None)
    if item is None:
        return JSONResponse({"ok": False, "error": "That key does not need a decision."},
                            status_code=404)
    if body.action != "remove" and item["verdict"] != "elevated":
        return JSONResponse({"ok": False, "error": "That key is not valid, so it can only be removed."},
                            status_code=400)
    raw = list(settings.streamdeck_ha_slots or [])
    where = [i for i, s in enumerate(raw) if isinstance(s, dict)][position]
    slot = dict(raw[where])
    slot.pop("elevated_grandfathered", None)
    slot.pop("needs_review", None)
    if body.action == "allow":
        slot["allow_elevated"] = True
    elif body.action == "stop":
        slot["allow_elevated"] = False
    else:
        for key in ("allow_elevated", "disabled_reason"):
            slot.pop(key, None)
        slot["entity_id"] = ""
        slot["service"] = ""
    raw[where] = slot
    settings.save({"streamdeck_ha_slots": raw})
    try:
        from ...services import deck_config
        await deck_config.apush_to_deck()
    except Exception:  # noqa: BLE001 - the deck catches up on its next push
        pass
    return {"ok": True, "review": _ha_slot_review()}


_DECK_READ_FAILED_MSG = "Could not read the Stream Deck settings, so nothing was saved. Try again."


@router.post("/streamdeck/config")
async def streamdeck_config_set(request: Request):
    """Proxy POST config to host bridge.

    What app settings own (theme, key style, Home Assistant, cameras, custom
    keys, idle timeout, and on a satellite the weather) is stamped from
    settings through deck_config.merge_deck_config whatever the page posted,
    so a refused key never reaches config.toml. The page loads the deck's own
    app key blanked; when it posts it back empty, the stored key is read from
    the deck once and kept, and when that read fails nothing is saved.
    """
    if _HOST_BRIDGE:
        try:
            payload = await request.json()
            from ...services import deck_config
            form = None
            inputs = None
            if isinstance(payload, dict) and isinstance(payload.get("config"), dict):
                # The GET adds page-only flags (ha_token_set, key_policy,
                # api_key_set); they are not deck settings.
                deck_config.strip_page_only_keys(payload["config"])
                if not str(payload["config"].get("api_key") or "").strip():
                    stored = await _read_stored_deck_config()
                    if stored is None:
                        return JSONResponse({"ok": False, "error": _DECK_READ_FAILED_MSG})
                    if "api_key" in stored:
                        payload["config"]["api_key"] = stored.get("api_key")
                    else:
                        payload["config"].pop("api_key", None)
                # Deck rotation is not stamped here: the deck editor posts the
                # rotation it wants, and other savers of this config do a
                # read-modify-write that must keep theirs.
                inputs = deck_config.deck_inputs()
                payload["config"], form = deck_config.merge_deck_config(
                    payload["config"], inputs)
            async with bridge_client(timeout=3.0) as c:
                r = await c.post(f"{_HOST_BRIDGE}/streamdeck/config", json=payload)
                if r.status_code == 200 and form:
                    deck_config.record_merged_push(form, inputs)
                return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
    return JSONResponse(status_code=500, content={"ok": False, "error": "bridge unavailable"})


async def _read_stored_deck_config() -> dict | None:
    """The deck's config.toml as the bridge reads it, or None on any failure."""
    try:
        async with bridge_client(timeout=3.0) as c:
            r = await c.get(f"{_HOST_BRIDGE}/streamdeck/config")
        if r.status_code != 200:
            return None
        body = r.json()
        config = body.get("config") if isinstance(body, dict) else None
        return config if isinstance(config, dict) else None
    except Exception:  # noqa: BLE001
        return None
