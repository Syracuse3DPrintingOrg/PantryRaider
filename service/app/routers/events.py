"""On-screen Home Assistant event channel (notifications + camera pop-ups).

Home Assistant pushes events here with an automation's ``rest_command`` (sending
the X-API-Key), and the kiosk / web UI polls ``/events/poll`` and shows them.

  POST /events/notify         {message, title?, level?, timeout?}
  POST /events/camera-popup   {camera?, seconds?}   (camera by name; default first)
  POST /events/camera-detect  {camera?, detection_type, seconds?} (FoodAssistant-akd0)
  POST /events/test           queue a sample notification (used by the setup UI)
  GET  /events/poll?since=<id> events newer than <id>, plus the current last id

Notifications and pop-ups target the screen of the instance HA posts to. On a
satellite point the HA automation at the satellite (the device with the display).

``/events/camera-detect`` (FoodAssistant-akd0) is the "let a detection decide"
sibling of ``/events/camera-popup``: a single Home Assistant automation can post
every detection type it sees (person, vehicle, animal, a doorbell visitor) and
this endpoint only pops the camera up when that type is one the user turned on
for that camera (each camera's "pop up on" setting on the Cameras page), so the
on/off logic lives here once instead of in a `choose:` block in every
automation. The same per-camera setting also drives the Reolink AI-detection
poller (``services.camera_detect`` + ``services.cameras.fetch_reolink_ai_state``)
for cameras that need no Home Assistant automation at all.
"""
from __future__ import annotations


from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import settings
from ..services import access, camera_detect, ha_events
from ..services.cameras import resolve_ha_entity
# The one URL policy lives in services/url_safety; re-exported so existing
# imports of routers.events.safe_nav_path keep working.
from ..services.url_safety import nav_page_path, safe_nav_path  # noqa: F401

router = APIRouter(prefix="/events", tags=["events"])


class NotifyPayload(BaseModel):
    message: str = ""
    title: str = ""
    level: str = "info"          # info | success | warning | error
    timeout: int = 0             # seconds on screen; 0 = the client default
    screen: str = ""             # one screen's id; empty = every screen here


class CameraPopupPayload(BaseModel):
    camera: str = ""             # camera name; empty = the first configured camera
    seconds: int = 0             # 0 = the configured default
    screen: str = ""


class CameraDetectPayload(BaseModel):
    camera: str = ""             # camera name; empty = the first configured camera
    detection_type: str = ""     # person | vehicle | animal | visitor
    seconds: int = 0             # 0 = the configured default
    screen: str = ""


class NavigatePayload(BaseModel):
    path: str = ""               # app-relative path, e.g. "ui/cook"
    page: str = ""               # or a page by name: home, recipe, timers
    screen: str = ""


class ConfirmPayload(BaseModel):
    message: str = ""
    title: str = ""
    screen: str = ""


_NO_SCREEN = "There is no screen with that id, or it cannot be sent to yet."


async def _to_screen(screen_id: str, event: dict, **extra):
    """Send one event to one screen instead of the shared ring. The reply
    names the screen, or is a 404 when there is none to send to."""
    from fastapi.concurrency import run_in_threadpool
    from ..services import screens
    sent = await run_in_threadpool(screens.send, screen_id.strip(), event)
    if sent is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": _NO_SCREEN})
    return {"ok": True, "screen": sent, **extra}


def _camera_index(name: str) -> int:
    """Resolve a camera name to its list index, or -1 when none match/exist.

    Matches by camera name (case-insensitive); an empty/unknown name falls back
    to the first configured camera so a pop-up always shows something.
    """
    cams = settings.streamdeck_cameras or []
    want = (name or "").strip().lower()
    if want:
        for i, cam in enumerate(cams):
            if isinstance(cam, dict) and str(cam.get("name", "")).strip().lower() == want:
                return i
    from ..services.cameras import is_reolink
    for i, cam in enumerate(cams):
        if isinstance(cam, dict) and (cam.get("snapshot_url") or cam.get("ha_entity")
                                      or is_reolink(cam) or resolve_ha_entity(cam)[0]):
            return i
    return -1


def _camera_src(name: str) -> tuple[str, str]:
    """Return (resolved_name, proxy_snapshot_src) for a camera by name, or ("","").

    The src is the same-origin proxy path the kiosk already uses, so HA
    cameras work without the kiosk handling the bearer token.
    """
    idx = _camera_index(name)
    if idx < 0:
        return "", ""
    cams = settings.streamdeck_cameras or []
    cam = cams[idx]
    return (cam.get("name", "") if isinstance(cam, dict) else ""), f"ui/camera/{idx}/snapshot"


@router.post("/notify")
async def notify(payload: NotifyPayload):
    """Queue a notification toast for the display."""
    if not payload.message.strip() and not payload.title.strip():
        return {"ok": False, "error": "message or title is required"}
    if payload.screen.strip():
        lvl = payload.level if payload.level in ("info", "success", "warning", "error") else "info"
        return await _to_screen(payload.screen, {
            "type": "notification", "message": payload.message, "title": payload.title,
            "level": lvl, "timeout": max(0, int(payload.timeout or 0))})
    eid = ha_events.add_notification(
        payload.message, title=payload.title, level=payload.level, timeout=payload.timeout
    )
    return {"ok": True, "id": eid}


@router.post("/camera-popup")
async def camera_popup(payload: CameraPopupPayload):
    """Queue a camera pop-up for the display (for example on person detected)."""
    name, src = _camera_src(payload.camera)
    if not src:
        return {"ok": False, "error": "No matching camera is configured."}
    seconds = payload.seconds or int(settings.ha_camera_popup_seconds or 20)
    if payload.screen.strip():
        return await _to_screen(payload.screen, {
            "type": "camera", "name": name, "src": src, "seconds": seconds}, camera=name)
    eid = ha_events.add_camera(name=name, src=src, seconds=seconds)
    return {"ok": True, "id": eid, "camera": name}


@router.post("/camera-detect")
async def camera_detect_event(payload: CameraDetectPayload):
    """Queue a camera pop-up only if this detection type is one the camera has
    turned on (FoodAssistant-akd0).

    Lets a single Home Assistant automation post every detection type it sees
    (person, vehicle, animal, a doorbell visitor) without a `choose:` block per
    type in HA: the enable/disable decision lives in each camera's "pop up on"
    setting on the Cameras page instead. Returns ``popped: false`` (not an
    error) when the type is disabled or unrecognised, so a chatty automation
    posting every detection never shows as failing.
    """
    idx = _camera_index(payload.camera)
    if idx < 0:
        return {"ok": False, "error": "No matching camera is configured."}
    cams = settings.streamdeck_cameras or []
    cam = cams[idx] if isinstance(cams[idx], dict) else {}
    enabled = cam.get("popup_types") or []
    if not camera_detect.should_popup(payload.detection_type, enabled):
        return {"ok": True, "popped": False}
    name, src = _camera_src(payload.camera)
    seconds = payload.seconds or int(settings.ha_camera_popup_seconds or 20)
    if payload.screen.strip():
        return await _to_screen(payload.screen, {
            "type": "camera", "name": name, "src": src, "seconds": seconds},
            popped=True, camera=name)
    eid = ha_events.add_camera(name=name, src=src, seconds=seconds)
    return {"ok": True, "popped": True, "id": eid, "camera": name}


@router.post("/navigate")
async def navigate(payload: NavigatePayload):
    """Queue a page-change for the display so HA can drive which page is shown
    (for example, jump the kitchen screen to the Cook page on a voice command,
    FoodAssistant-i4rs). Only the app's own pages are accepted
    (``url_safety.NAV_PAGES``), or a page's plain name such as "expiring": an
    API path or a missing page would leave the screen on a bare reply with no
    app script to move it along, so those get a 400 (FoodAssistant-ye112)."""
    if payload.screen.strip():
        return await _navigate_screen(payload)
    if payload.page.strip() and not payload.path.strip():
        from ..services import screens
        payload.path = screens.page_path("server", payload.page)
    if not safe_nav_path(payload.path):
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "a valid same-origin path is required (e.g. ui/cook)"})
    path = nav_page_path(payload.path)
    if not path:
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": "not a page in the app; use a page path such as ui/cook or "
                     "ui/expiring, or a page name such as expiring",
        })
    eid = ha_events.add_navigate(path)
    return {"ok": True, "id": eid, "path": path}


async def _navigate_screen(payload: NavigatePayload):
    """Move one named screen. A page name is turned into that screen's own
    address for it; a path is checked the same way an untargeted one is."""
    from fastapi.concurrency import run_in_threadpool
    from ..services import screens
    sid = payload.screen.strip()
    page = payload.page.strip().lower()
    if page:
        if page not in screens.PAGES:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "page is one of home, recipe, timers or app."})
        sent = await run_in_threadpool(lambda: screens.send_navigate(sid, page=page))
        path = ""
    else:
        path = nav_page_path(payload.path) if safe_nav_path(payload.path) else ""
        if not path:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "not a page in the app; send a page name "
                                      "(home, recipe, timers) or a page path such as ui/cook"})
        sent = await run_in_threadpool(lambda: screens.send_navigate(sid, path=path))
    if sent is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": _NO_SCREEN})
    return {"ok": True, "screen": sent, "path": path, "page": page}


class ClosePayload(BaseModel):
    screen: str = ""


@router.post("/close")
async def close(payload: ClosePayload, request: Request):
    """Put one screen back where it was before it was sent a recipe or a
    timer ("close the recipe"). A tablet app drops the Pantry Raider page and
    shows its own dashboard again; a kitchen display goes to its home page."""
    from fastapi.concurrency import run_in_threadpool
    from ..services import screens
    sid = payload.screen.strip()
    if not sid:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "Say which screen to close."})
    # A screen may put itself away (its timers are done, nobody is using it),
    # never another screen.
    p = access.principal(request)
    if p.scope in ("screen", "screen_app") and screens.owner_of(sid) != p.credential_id:
        return JSONResponse(status_code=403, content={
            "ok": False, "error": "That screen belongs to another key."})
    sent = await run_in_threadpool(screens.send_close, sid)
    if sent is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": _NO_SCREEN})
    return {"ok": True, "screen": sent}


@router.post("/confirm")
async def confirm(payload: ConfirmPayload):
    """Queue a deck-action confirmation for the display (FoodAssistant-rdlo).

    A Stream Deck press that does not change the on-screen page (a shopping
    quick-add, a scanner-mode switch) posts here so the kiosk shows a brief
    "it worked" toast. Unlike Home Assistant notifications, this shows even
    when on-screen HA events are turned off: it is local action feedback, not
    Home Assistant traffic."""
    if not payload.message.strip() and not payload.title.strip():
        return {"ok": False, "error": "message or title is required"}
    if payload.screen.strip():
        return await _to_screen(payload.screen, {
            "type": "confirm", "message": payload.message, "title": payload.title,
            "level": "success", "timeout": 5})
    eid = ha_events.add_confirmation(payload.message, title=payload.title)
    return {"ok": True, "id": eid}


@router.post("/test")
async def test_event():
    """Queue a sample notification so the user can confirm the channel works."""
    eid = ha_events.add_notification(
        "If you can read this, Home Assistant notifications are wired up.",
        title="Pantry Raider test", level="success",
    )
    return {"ok": True, "id": eid}


@router.post("/warning-test")
async def warning_test():
    """Queue a sample device-health warning toast (FoodAssistant-h28s).

    A hardware-free way to confirm the on-screen power/thermal alert shows on
    the kiosk, without having to force a real under-voltage. It renders exactly
    like the real thing: an amber/red toast that appears even when on-screen
    Home Assistant events are turned off."""
    from ..services import pi_health
    title, message = pi_health.warning_toast_copy({"key": "undervoltage"})
    eid = ha_events.add_warning(message, title=title, key="undervoltage",
                                level=pi_health.warning_level("undervoltage"))
    return {"ok": True, "id": eid}


@router.get("/poll")
async def poll(request: Request, since: int = 0, wait: float = 0, display: int = 0):
    """New events since ``since``, plus the current last id.

    With ``wait`` > 0 this long-polls: the request is held (up to ``wait``
    seconds, capped) and returns the instant an event arrives, so a physical
    control like a NeoKey moves the kiosk with no fixed-tick lag. Without it the
    old immediate answer is returned, for callers that just want a snapshot."""
    if wait and wait > 0:
        return await ha_events.wait_for_events(since, timeout=min(float(wait), 30.0),
                                               is_gone=request.is_disconnected,
                                               display=bool(display))
    return ha_events.poll(since, display=bool(display))


@router.get("/count")
async def count():
    """Number of live on-screen events, for the Start Page Glance "Alerts" pill
    (FoodAssistant-gg33). Same auth as /events/poll (no setup bypass): it
    reflects device state and is not needed before the app is configured."""
    return {"count": ha_events.active_count()}
