"""Adding and removing passkeys for the admin sign-in (FoodAssistant-kc2cy).

Everything under /setup is admin-only already. Adding a passkey also asks for
the password (or a current authenticator code) first, the same proof 2FA
changes take, so a borrowed signed-in browser cannot add its own way in.
The sign-in half lives on /ui/login (routers/ui.py).
"""

import asyncio
import threading

from fastapi import Request
from pydantic import BaseModel

from ...config import settings
from ...services import auth_state
from ...services import passkeys as pk
from ._common import router
from .totp import _local_2fa_credential_ok

# One add or remove at a time, so two tabs cannot each save a list that is
# missing the other's change.
_save_lock = threading.Lock()


class PasskeyBeginPayload(BaseModel):
    name: str = ""
    # The password, a current authenticator code, or a recovery code.
    credential: str = ""


class PasskeyFinishPayload(BaseModel):
    credential: dict = {}


class PasskeyRemovePayload(BaseModel):
    id: str = ""


def _state(request: Request) -> dict:
    return {"ok": True, "passkeys": pk.public_list(settings),
            "here": bool(pk.site_for(request.headers.get("origin", "")
                                     or _page_origin(request))),
            "password_set": bool(settings.auth_password)}


def _page_origin(request: Request) -> str:
    """The origin a GET came from. Browsers leave Origin off same-origin
    GETs, so fall back to the address the request names."""
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return f"{proto.split(',')[0].strip()}://{host.split(',')[0].strip()}" if host else ""


@router.get("/passkeys")
async def passkeys_list(request: Request):
    return _state(request)


@router.post("/passkeys/register/begin")
async def passkeys_register_begin(request: Request, payload: PasskeyBeginPayload):
    if not settings.auth_password:
        return {"ok": False, "error": "Set a password for this kitchen first."}
    if not await asyncio.to_thread(_local_2fa_credential_ok, payload.credential):
        return {"ok": False, "error": "Enter your password to add a passkey."}
    try:
        options = pk.registration_options(settings, request.session,
                                          request.headers.get("origin", ""), payload.name)
    except pk.PasskeyError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "options": options}


def _add(record: dict) -> None:
    with _save_lock:
        current = settings.local_passkeys if isinstance(settings.local_passkeys, list) else []
        settings.save({"local_passkeys": [*current, record]})


@router.post("/passkeys/register/finish")
async def passkeys_register_finish(request: Request, payload: PasskeyFinishPayload):
    try:
        record = pk.finish_registration(settings, request.session,
                                        request.headers.get("origin", ""), payload.credential)
    except pk.PasskeyError as exc:
        return {"ok": False, "error": str(exc)}
    await asyncio.to_thread(_add, record)
    return {**_state(request), "message": "Passkey added. You can sign in with it now."}


def _remove(cred_id: str) -> bool:
    with _save_lock:
        kept = pk.without(settings, cred_id)
        if kept is None:
            return False
        settings.save({"local_passkeys": kept})
        return True


@router.post("/passkeys/remove")
async def passkeys_remove(request: Request, payload: PasskeyRemovePayload):
    if not await asyncio.to_thread(_remove, payload.id):
        return {"ok": False, "error": "That passkey was already removed."}
    # A removed passkey may be on a lost phone: end the other sessions too,
    # the same as a password or 2FA change.
    auth_state.sign_out_others(request.session)
    return {**_state(request), "message": "Passkey removed."}
