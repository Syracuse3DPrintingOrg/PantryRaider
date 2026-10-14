"""The kitchen screen signs itself in (/ui/kiosk/launch, /ui/kiosk/relaunch).

The screen's launcher (foodassistant-kiosk-launch on the device) opens
/ui/kiosk/launch?ticket=...&next=... with a single-use ticket only root on
this device can make. A good ticket from this device's own address opens a
kiosk session; anything else just lands on the page without one, so the
worst a bad ticket can do is show the sign-in page.

The sign-in page calls POST /ui/kiosk/relaunch when the screen finds itself
signed out, so the launcher runs again with a new ticket (limited to once per
10 minutes and three times a day).
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from ..services import access, auth_state, local_agents
from ..services.url_safety import safe_nav_path

router = APIRouter(prefix="/ui/kiosk", tags=["kiosk"])

_DEFAULT_TARGET = "/ui/?kiosk=1"


def _target(next_path: str) -> str:
    clean = safe_nav_path(next_path or "")
    if not clean:
        return _DEFAULT_TARGET
    path = "/" + clean
    base, _, query = path.partition("?")
    base, _, _frag = base.partition("#")
    params = [q for q in query.split("&") if q] if query else []
    if not any(q == "kiosk=1" or q.startswith("kiosk=") for q in params):
        params.append("kiosk=1")
    return base + "?" + "&".join(params)


@router.get("/launch")
async def launch(request: Request, ticket: str = "", next: str = "", probe: str = ""):
    if probe == "1":
        return Response(status_code=204)
    target = _target(next)
    if ticket and access.classify_origin(request) == "plain_loopback":
        if local_agents.verify_ticket(ticket):
            # A Bandit's kiosk PIN, once unlocked on this screen, stays
            # unlocked across the screen's own restarts, as its long-lived
            # cookie kept it before the screen signed itself in.
            pin_ok = request.session.get("pin_ok") is True
            request.session.clear()
            request.session["authed"] = True
            request.session["role"] = "kiosk"
            request.session["kiosk_day"] = date.today().isoformat()
            request.session["kiosk_gen"] = auth_state.kiosk_gen()
            if pin_ok:
                request.session["pin_ok"] = True
            try:
                auth_state.record_ticket_redeemed()
            except Exception:
                pass
    return RedirectResponse(target, status_code=303)


@router.post("/relaunch")
async def relaunch(request: Request):
    if access.classify_origin(request) != "plain_loopback":
        return JSONResponse({"ok": False, "error": access.RELAUNCH_LOCAL_MSG}, status_code=403)
    restarting = await local_agents.request_kiosk_relaunch("login")
    return {"ok": True, "restarting": bool(restarting)}
