"""Sign in with your identity provider: the two public legs.

GET /auth/oidc/start leaves for the provider (a plain link, so the page's
form-action policy never gets in the way), and GET /auth/oidc/callback is
where the provider sends the browser back. Linking the admin's account
starts from Settings (routers/setup/identity.py) and returns here too.
services/oidc.py holds the checks.
"""

import asyncio

from fastapi import APIRouter, Request

from ..config import settings
from ..ingress import ingress_path, ingress_redirect
from ..services import access, sessions
from ..services import oidc
from ..session_cookie import secure_request

router = APIRouter(prefix="/auth/oidc", tags=["auth"])

_DENIED_MSG = "Your identity provider did not let that account in."


def request_host(request: Request) -> str:
    raw = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return access.host_name(raw.split(",")[0].strip())


def redirect_uri(request: Request) -> str:
    from .ui import _request_origin
    return _request_origin(request) + ingress_path(request) + oidc.CALLBACK_PATH


def set_state_cookie(request: Request, response, state: str) -> None:
    response.set_cookie(oidc.STATE_COOKIE, state, max_age=oidc.FLOW_SECONDS,
                        path="/auth/oidc", httponly=True, samesite="lax",
                        secure=secure_request(request.scope, request.headers))


def _login_error(request: Request, message: str, status_code: int = 400):
    from .ui import _render_login
    response = _render_login(request, status_code=status_code, error=message)
    response.delete_cookie(oidc.STATE_COOKIE, path="/auth/oidc")
    return response


@router.get("/start")
async def start(request: Request, remember: str = "1"):
    if not oidc.linked(settings):
        return ingress_redirect(request, "/ui/login")
    if not oidc.host_matches(settings, request_host(request)):
        return _login_error(request, "Sign in with " + oidc.display_name(settings)
                            + " at https://" + settings.oidc_host + ".")
    try:
        url, state = await asyncio.to_thread(
            oidc.begin, settings, redirect_uri(request), remember=(remember != "0"))
    except oidc.OidcError as exc:
        return _login_error(request, str(exc), 502)
    response = ingress_redirect(request, url)
    set_state_cookie(request, response, state)
    return response


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = "", iss: str = "",
                   error: str = ""):
    from ..services.rate_limit import client_key, login_guard
    from .ui import _open_session
    key = client_key(request)
    if login_guard.blocked(key):
        wait = login_guard.retry_after(key)
        return _login_error(request, f"Too many attempts. Wait about {max(1, wait // 60)} "
                            "minute(s) and try again.", 429)
    try:
        flow = oidc.take(state, request.cookies.get(oidc.STATE_COOKIE, ""))
    except oidc.OidcError as exc:
        return _login_error(request, str(exc))
    linking = bool(flow["link_sid"])
    if error:
        message = _DENIED_MSG if error == "access_denied" else oidc.TURNED_DOWN_MSG
        if linking:
            return _settings_return(request, "failed")
        return _login_error(request, message, 401)
    try:
        claims = await asyncio.to_thread(oidc.finish, settings, flow, code, iss)
    except oidc.OidcError as exc:
        login_guard.record(key)
        if linking:
            return _settings_return(request, "failed")
        return _login_error(request, str(exc), 401)

    if linking:
        session = request.session
        if not (session.get("authed") and session.get("role") == "admin"
                and session.get(sessions.SID_KEY) == flow["link_sid"]):
            return _login_error(request, "Sign in to this kitchen first, then link your account "
                                "from Settings, Security.", 401)
        await asyncio.to_thread(settings.save, {
            "oidc_subject": str(claims["sub"]),
            "oidc_account": oidc.account_label(claims),
            "oidc_host": request_host(request),
        })
        return _settings_return(request, "linked")

    if not oidc.subject_matches(settings, claims):
        login_guard.record(key)
        return _login_error(request, oidc.WRONG_ACCOUNT_MSG, 401)
    login_guard.reset(key)
    _open_session(request, "admin", flow["remember"])
    response = ingress_redirect(request, "/ui/")
    response.delete_cookie(oidc.STATE_COOKIE, path="/auth/oidc")
    return response


def _settings_return(request: Request, outcome: str):
    response = ingress_redirect(request, f"/setup?oidc={outcome}#pane-security")
    response.delete_cookie(oidc.STATE_COOKIE, path="/auth/oidc")
    return response
