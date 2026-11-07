"""Signed-in browsers and trusted devices in Settings, Security
(FoodAssistant-6lbsa, FoodAssistant-qtm5f). Admin only, like all of /setup."""

import asyncio
import time

from fastapi import Request
from pydantic import BaseModel

from ...config import settings
from ...services import sessions
from ._common import router


class SignInRevokePayload(BaseModel):
    id: str = ""


def _ago(ts) -> str:
    try:
        secs = max(0, time.time() - float(ts))
    except (TypeError, ValueError):
        return ""
    if secs < 120:
        return "just now"
    if secs < 7200:
        return f"{int(secs // 60)} minutes ago"
    if secs < 2 * 86400:
        return f"{int(secs // 3600)} hours ago"
    return f"{int(secs // 86400)} days ago"


_ROLE_LABELS = {"admin": "", "viewer": "kitchen access", "kiosk": "kitchen screen"}


def _state(request: Request) -> dict:
    current = request.session.get(sessions.SID_KEY)
    rows = []
    for r in sessions.listing(current):
        rows.append({"id": r["id"], "browser": r.get("browser") or "A browser",
                     "role": _ROLE_LABELS.get(r.get("role", ""), ""),
                     "last_seen": _ago(r.get("last_seen")), "current": r["current"]})
    return {"ok": True, "sessions": rows, "trusted": sessions.trusted_count(),
            "totp_on": settings.local_2fa_active(),
            "totp_scope": settings.local_totp_scope}


@router.get("/signins")
async def signins_list(request: Request):
    return await asyncio.to_thread(_state, request)


@router.post("/signins/revoke")
async def signins_revoke(request: Request, payload: SignInRevokePayload):
    if payload.id == request.session.get(sessions.SID_KEY):
        return {"ok": False, "error": "Use Sign Out to sign this browser out."}
    if not await asyncio.to_thread(sessions.revoke, payload.id):
        return {"ok": False, "error": "That browser was already signed out."}
    return {**(await asyncio.to_thread(_state, request)), "message": "Signed out."}


@router.post("/signins/forget-trusted")
async def signins_forget_trusted(request: Request):
    await asyncio.to_thread(sessions.forget_trusted)
    return {**(await asyncio.to_thread(_state, request)),
            "message": "Every device asks for a code again at its next sign-in."}
