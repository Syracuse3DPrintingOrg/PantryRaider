"""The setup code (/ui/setup-code): continuing from outside the home network.

Three cases use it:

- setup: the install is not configured yet. The code (plus a new password,
  unless the environment set one) opens an admin session and the wizard
  finishes from here.
- open: configured with no password. The code and a new password.
- rescue: a password but no device two-factor, reached from outside (by the
  same rule the sign-in page uses, access.sign_in_from_outside). Since a
  password alone is not accepted from outside, the code plus the current
  password opens one admin session, which lands on turning two-factor on.

Every other case goes to the normal sign-in page. The same module renders
the refusal pages the auth middleware shows (open_refused, name_refused,
setup_code), which carry the setup-code form first for visitors from outside.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Form, Request

from ..config import settings
from ..ingress import ingress_redirect
from ..passwords import verify_secret
from ..services import access, auth_state, lan
from ..services import setup_code as setup_code_svc

router = APIRouter(prefix="/ui", tags=["setup-code"])

_COPY_NAMES = (
    "OPEN_REFUSAL_MSG", "NAME_REFUSAL_MSG", "NAME_ALLOW_HINT", "SETUP_CODE_MSG",
    "SETUP_CODE_INTRO", "SETUP_CODE_INTRO_HAS_PASSWORD", "SETUP_CODE_INTRO_OPEN",
    "SETUP_CODE_INTRO_RESCUE", "SETUP_CODE_WHERE", "RESCUE_2FA", "RESCUE_ENV",
    "RESCUE_LAN_CIDRS", "SETUP_CODE_LINK", "KEYLESS_SETUP_CODE_NOTE", "RELAUNCH_NOTE",
)


def page_copy() -> dict:
    return {name: getattr(access, name) for name in _COPY_NAMES}


def _outside(request: Request) -> bool:
    """The sign-in page's own rule (access.sign_in_from_outside), so a sign-in
    refused from outside always finds the rescue here."""
    return access.sign_in_from_outside(request)


def mode_for(request: Request) -> str | None:
    """'setup', 'open', 'rescue' or None (use the normal sign-in page)."""
    if not settings.is_configured():
        return "setup"
    if not settings.auth_password:
        return "open"
    if not settings.local_2fa_active() and _outside(request):
        return "rescue"
    return None


def home_addresses(request: Request) -> list[str]:
    """Addresses that reach this kitchen from home, for the refusal pages."""
    out: list[str] = []
    try:
        from .. import config as _config
        port = ""
        host = request.headers.get("host", "")
        if host.rsplit(":", 1)[-1].isdigit() and not host.endswith("]"):
            port = ":" + host.rsplit(":", 1)[-1]
        name = _config.device_hostname()
        if name and "." not in name:
            out.append(f"http://{name.lower()}.local{port}/")
        elif name and name.endswith(".local"):
            out.append(f"http://{name.lower()}{port}/")
        if not lan.in_docker_bridge():
            ip = _config._lan_ip()
            if ip:
                out.append(f"http://{ip}{port}/")
    except Exception:
        pass
    return out


def render_page(request: Request, step: str, status_code: int, *, mode: str | None = None,
                error: str | None = None):
    """Render the sign-in template for a setup-code or refusal step."""
    from .ui import _render_login
    outside = _outside(request)
    page_mode = mode or ("open" if step in ("open_refused", "name_refused") else mode_for(request))
    keyless_note = False
    if page_mode == "open":
        try:
            keyless_note = bool(auth_state.keyless_device_labels(30))
        except Exception:
            keyless_note = False
    if page_mode == "setup":
        intro = (access.SETUP_CODE_INTRO if not settings.auth_password
                 else access.SETUP_CODE_INTRO_HAS_PASSWORD)
    elif page_mode == "rescue":
        intro = access.SETUP_CODE_INTRO_RESCUE
    else:
        intro = access.SETUP_CODE_INTRO_OPEN
    needs_password = page_mode == "open" or (page_mode == "setup" and not settings.auth_password)
    return _render_login(
        request, step=step, status_code=status_code, error=error,
        sc_mode=page_mode, sc_outside=outside, sc_intro=intro,
        sc_needs_password=needs_password, sc_keyless_note=keyless_note,
        sc_home_addresses=home_addresses(request),
        sc_in_container=os.path.exists("/.dockerenv"),
        ac=page_copy(),
    )


@router.get("/setup-code")
async def setup_code_page(request: Request):
    mode = mode_for(request)
    if mode is None:
        return ingress_redirect(request, "/ui/login")
    setup_code_svc.ensure("page")
    return render_page(request, "setup_code", 200, mode=mode)


@router.post("/setup-code")
async def setup_code_submit(request: Request, code: str = Form(""), password: str = Form(""),
                            confirm: str = Form("")):
    mode = mode_for(request)
    if mode is None:
        return ingress_redirect(request, "/ui/login")
    key = setup_code_svc.limiter_key(request)
    verdict = setup_code_svc.verify(code, key)
    if verdict == "slow_down":
        return render_page(request, "setup_code", 429, mode=mode, error=access.SETUP_CODE_SLOW_MSG)
    if verdict != "ok":
        return render_page(request, "setup_code", 401, mode=mode, error=access.SETUP_CODE_WRONG_MSG)
    saved_password = False
    if mode == "rescue":
        if not password or not verify_secret(password, settings.auth_password):
            setup_code_svc.record_wrong(key)
            used_up = setup_code_svc.record_wrong_password()
            return render_page(request, "setup_code", 401, mode=mode,
                               error=(access.RESCUE_CODE_USED_UP_MSG if used_up
                                      else access.RESCUE_PASSWORD_WRONG_MSG))
    elif mode == "open" or not settings.auth_password:
        if len(password or "") < 8:
            return render_page(request, "setup_code", 400, mode=mode,
                               error=access.PASSWORD_SHORT_MSG)
        if password != confirm:
            return render_page(request, "setup_code", 400, mode=mode,
                               error=access.PASSWORD_MISMATCH_MSG)
        settings.save({"auth_password": password, "auth_required": True})
        saved_password = True
    setup_code_svc.clear()
    request.session.clear()
    request.session["authed"] = True
    request.session["role"] = "admin"
    if saved_password:
        access.note_password_state_and_notify()
    if not settings.is_configured():
        target = "/setup"
    elif _outside(request) and not settings.local_2fa_active():
        target = "/setup?twofactor=needed#pane-security"
    else:
        target = "/ui/"
    return ingress_redirect(request, target)
