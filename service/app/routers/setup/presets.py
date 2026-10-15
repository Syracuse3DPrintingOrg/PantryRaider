"""UI scale, hardware presets, and the NeoKey reset."""

from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from ...config import (
    settings, UI_SCALES, _DEFAULT_UI_SCALE, DISPLAY_ROTATIONS,
    _DEFAULT_DISPLAY_ROTATION, HARDWARE_PRESETS,
)
from ._common import router


class ScalePayload(BaseModel):
    ui_scale: str = _DEFAULT_UI_SCALE
    display_rotation: int = _DEFAULT_DISPLAY_ROTATION


@router.post("/scale")
async def save_scale(payload: ScalePayload):
    scale = payload.ui_scale if payload.ui_scale in UI_SCALES else _DEFAULT_UI_SCALE
    rot = payload.display_rotation if payload.display_rotation in DISPLAY_ROTATIONS else _DEFAULT_DISPLAY_ROTATION
    settings.save({"ui_scale": scale, "display_rotation": rot})
    return {"ok": True}


class HardwarePresetPayload(BaseModel):
    preset: str = ""


@router.post("/preset/apply")
async def apply_hardware_preset(payload: HardwarePresetPayload):
    """Apply a prebuilt hardware preset in one save (FoodAssistant-kl5n).

    Saves only the settings the preset lists (so nothing else is disturbed),
    then triggers the same downstream effect a manual deck save would: on a Pi
    with a Stream Deck it pushes the deck config so a new rotation or key count
    reaches the running controller. Display scale and rotation are picked up by
    the kiosk on its own, exactly as a manual scale save; the caller refreshes
    the form fields from the returned dict.
    """
    preset = HARDWARE_PRESETS.get(payload.preset)
    if not preset:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "Unknown hardware preset."})
    applied = dict(preset.get("settings", {}))
    settings.save(applied)
    # Push the deck config when the preset touches Stream Deck settings and a
    # deck is present, reusing the satellite/appliance push helper rather than
    # duplicating the read-modify-write. Best effort: never fail the apply if
    # the bridge is unreachable or this is not a Pi appliance.
    deck_pushed = False
    if any(k.startswith("streamdeck_") or k == "has_streamdeck" for k in applied):
        try:
            from ...services.satellite import _push_streamdeck_settings
            deck_pushed = await run_in_threadpool(_push_streamdeck_settings)
        except Exception:
            deck_pushed = False
    return {"ok": True, "preset": payload.preset,
            "label": preset.get("label", ""),
            "applied": applied, "deck_pushed": deck_pushed}


@router.post("/neokey/reset")
async def neokey_reset():
    """Put the plug-in key pad back to its out-of-the-box settings
    (FoodAssistant-4pwh): every configured NeoKey returns to the stock key
    order and brightness, and the resting and timer colors return to their
    defaults. The accessories stay registered; only their options move. The
    color defaults come from the Settings model, so this endpoint can never
    drift from what a fresh install gets."""
    from ...services import stemma
    fields = type(settings).model_fields
    rest = fields["neokey_rest_color"].default
    timer = fields["neokey_timer_color"].default
    devices = [dict(d) for d in stemma.configured_devices()]
    for dev in devices:
        dev["options"] = stemma.normalize_options(dev.get("kind"), {})
    settings.save({"stemma_devices": devices,
                   "neokey_rest_color": rest,
                   "neokey_timer_color": timer})
    return {"ok": True, "stemma": devices,
            "neokey_rest_color": rest, "neokey_timer_color": timer}
