"""Stream Deck action catalog, install, and saved profiles."""

import json
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...config import settings
from ...database import SessionLocal
from ...hardware import is_raspberry_pi
from ...models.db_models import StreamDeckProfile
from ...services.bridge import bridge_client
from ._common import router, _HOST_BRIDGE, _carry_elevated_optin


@router.get("/streamdeck/actions")
async def streamdeck_actions():
    """List assignable Stream Deck actions for the grid editors.

    On a Pi the live catalog comes from the host bridge. Everywhere else (and
    when the bridge is unreachable) the bundled generated catalog is served, so
    the Start Page editor on a plain server shows the same full palette a Pi
    gets."""
    if is_raspberry_pi():
        try:
            async with bridge_client(timeout=12.0) as c:
                r = (await c.get(f"{_HOST_BRIDGE}/streamdeck/actions")).json()
            if r.get("ok") and isinstance(r.get("actions"), list):
                return r
        except Exception:
            pass
    from ...services.start_page import bundled_catalog
    actions = bundled_catalog()
    if actions:
        return {"ok": True, "actions": actions, "source": "bundled"}
    return {"ok": False, "error": "Action catalog unavailable."}


@router.post("/streamdeck/install")
async def streamdeck_install():
    """Provision the Stream Deck service for a deck attached after first install."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with bridge_client(timeout=15.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/streamdeck/install")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


class ProfileSavePayload(BaseModel):
    name: str
    deck_size: int
    key_overrides: list = []


def _profile_to_dict(p: StreamDeckProfile) -> dict:
    try:
        overrides = json.loads(p.key_overrides or "[]")
    except Exception:
        overrides = []
    return {
        "name": p.name,
        "deck_size": p.deck_size,
        "key_overrides": overrides,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def _profile_key_error(errs: list) -> str:
    # The current editor escapes this text itself; a Settings tab opened
    # before 0.21 puts it in the page as HTML. Dropping angle brackets from
    # the labels keeps both safe without showing escape codes in the new one.
    labels = ", ".join(
        str(e.get("label") or e.get("id") or "a key").replace("<", "").replace(">", "")
        for e in errs if isinstance(e, dict))
    return ("The profile has a Home Assistant key that is not valid: "
            f"{labels}. Fix it and save again.")


@router.get("/streamdeck/profiles")
def streamdeck_profiles_list():
    """List saved Stream Deck profiles."""
    db = SessionLocal()
    try:
        rows = db.query(StreamDeckProfile).order_by(StreamDeckProfile.name).all()
        return {"ok": True, "profiles": [_profile_to_dict(r) for r in rows]}
    finally:
        db.close()


@router.post("/streamdeck/profiles")
def streamdeck_profiles_save(payload: ProfileSavePayload):
    """Save or replace a named Stream Deck profile."""
    from datetime import datetime, timezone
    name = (payload.name or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Profile name required."}, status_code=400)
    if payload.deck_size not in (6, 15, 32):
        return JSONResponse({"ok": False, "error": "deck_size must be 6, 15, or 32."}, status_code=400)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = SessionLocal()
    try:
        row = db.query(StreamDeckProfile).filter(StreamDeckProfile.name == name).first()
        overrides = list(payload.key_overrides or [])
        if row:
            try:
                stored = json.loads(row.key_overrides or "[]")
            except (TypeError, ValueError):
                stored = []
            slots = settings.streamdeck_ha_slots or []
            overrides = _carry_elevated_optin(
                overrides, stored if isinstance(stored, list) else [], slots, slots,
                match="id")
        from ...services import ha_allowlist
        overrides, errs, _review = ha_allowlist.review_entries(
            overrides, settings.streamdeck_ha_slots or [])
        if errs:
            return JSONResponse({"ok": False, "error": _profile_key_error(errs)},
                                status_code=400)
        overrides_json = json.dumps(overrides)
        if row:
            row.deck_size = payload.deck_size
            row.key_overrides = overrides_json
            row.updated_at = now
        else:
            row = StreamDeckProfile(
                name=name,
                deck_size=payload.deck_size,
                key_overrides=overrides_json,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
        db.commit()
        return {"ok": True, "profile": _profile_to_dict(row)}
    finally:
        db.close()


@router.delete("/streamdeck/profiles/{name}")
def streamdeck_profiles_delete(name: str):
    """Delete a named Stream Deck profile."""
    db = SessionLocal()
    try:
        row = db.query(StreamDeckProfile).filter(StreamDeckProfile.name == name).first()
        if not row:
            return JSONResponse({"ok": False, "error": "Profile not found."}, status_code=404)
        db.delete(row)
        db.commit()
        return {"ok": True}
    finally:
        db.close()
