"""Settings for signing in with your own identity provider (OpenID Connect).

Everything under /setup is admin-only already. Linking an account adds a
way in, so it asks for the password (or a current authenticator code)
first, the same proof adding a passkey takes. Changing the issuer or the
client id unlinks the account, since a different provider could name any
account it likes. The sign-in legs live in routers/auth_oidc.py.
"""

import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...config import settings
from ...services import oidc, sessions
from ._common import router
from .totp import _local_2fa_credential_ok

_UNLINKED = {"oidc_subject": "", "oidc_account": "", "oidc_host": "", "oidc_only": False}


class IdentityConfigPayload(BaseModel):
    issuer: str = ""
    client_id: str = ""
    # Blank keeps the saved secret.
    client_secret: str = ""
    name: str = ""


class IdentityLinkPayload(BaseModel):
    credential: str = ""
    remember: bool = True


class IdentityOnlyPayload(BaseModel):
    on: bool = False


def _state(request: Request) -> dict:
    from ..auth_oidc import redirect_uri, request_host
    return {
        "ok": True,
        "configured": oidc.configured(settings),
        "linked": oidc.linked(settings),
        "issuer": settings.oidc_issuer,
        "client_id": settings.oidc_client_id,
        "has_secret": bool(settings.oidc_client_secret),
        "name": settings.oidc_name,
        "display_name": oidc.display_name(settings),
        "account": settings.oidc_account,
        "host": settings.oidc_host,
        "only": bool(settings.oidc_only),
        "callback": redirect_uri(request),
        "here": request_host(request),
        "password_set": bool(settings.auth_password),
    }


@router.get("/oidc")
async def identity_state(request: Request):
    return _state(request)


@router.post("/oidc/config")
async def identity_config(request: Request, payload: IdentityConfigPayload):
    issuer_in = payload.issuer.strip()
    client_id = payload.client_id.strip()
    name = payload.name.strip()[:40]
    if not issuer_in and not client_id:
        # Turning it off: forget the provider and the link together.
        await asyncio.to_thread(settings.save, {
            "oidc_issuer": "", "oidc_client_id": "", "oidc_client_secret": "",
            "oidc_name": name, **_UNLINKED})
        return _state(request)
    try:
        issuer = oidc.normalize_issuer(issuer_in)
    except oidc.OidcError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if not client_id:
        return JSONResponse({"ok": False, "error": "Enter the client id your provider gave you."},
                            status_code=400)
    secret = payload.client_secret.strip() or settings.oidc_client_secret
    if not secret:
        return JSONResponse({"ok": False, "error": "Enter the client secret your provider gave you."},
                            status_code=400)
    try:
        await asyncio.to_thread(oidc.discovery, issuer, True)
    except oidc.OidcError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    changes = {"oidc_issuer": issuer, "oidc_client_id": client_id,
               "oidc_client_secret": secret, "oidc_name": name}
    if issuer != settings.oidc_issuer or client_id != settings.oidc_client_id:
        changes.update(_UNLINKED)
    await asyncio.to_thread(settings.save, changes)
    return _state(request)


@router.post("/oidc/link")
async def identity_link(request: Request, payload: IdentityLinkPayload):
    from ..auth_oidc import redirect_uri, set_state_cookie
    if not oidc.configured(settings):
        return JSONResponse({"ok": False, "error": oidc.NOT_READY_MSG}, status_code=400)
    if not settings.auth_password:
        return JSONResponse({"ok": False, "error": "Set a password for this kitchen first."},
                            status_code=400)
    if not await asyncio.to_thread(_local_2fa_credential_ok, payload.credential):
        return JSONResponse({"ok": False, "error": "Enter your password to link your account."},
                            status_code=401)
    sid = request.session.get(sessions.SID_KEY)
    if not isinstance(sid, str) or not sid:
        return JSONResponse({"ok": False, "error": "Sign in again, then link your account."},
                            status_code=401)
    try:
        url, state = await asyncio.to_thread(oidc.begin, settings, redirect_uri(request),
                                             link_sid=sid)
    except oidc.OidcError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    response = JSONResponse({"ok": True, "url": url})
    set_state_cookie(request, response, state)
    return response


@router.post("/oidc/unlink")
async def identity_unlink(request: Request):
    await asyncio.to_thread(settings.save, dict(_UNLINKED))
    return _state(request)


@router.post("/oidc/only")
async def identity_only(request: Request, payload: IdentityOnlyPayload):
    if payload.on and not oidc.linked(settings):
        return JSONResponse({"ok": False, "error": "Link your account first."}, status_code=400)
    await asyncio.to_thread(settings.save, {"oidc_only": bool(payload.on)})
    return _state(request)
