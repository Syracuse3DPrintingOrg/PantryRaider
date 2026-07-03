import secrets
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from typing import Optional

from ..config import settings
from ..passwords import verify_secret, looks_hashed
from ..database import get_db
from ..ingress import ingress_redirect
from ..models.db_models import ExpiryDefault
from ..services.grocy import GrocyClient
from ..storage_categories import all_categories, OTHER
from ..templating import templates

router = APIRouter(prefix="/ui", tags=["ui"])


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not settings.auth_password or request.session.get("authed"):
        return ingress_redirect(request, "/ui/")
    if request.session.get("totp_pending"):
        return templates.TemplateResponse(request, "login.html",
            {"request": request, "error": None, "step": "totp"})
    return templates.TemplateResponse(request, "login.html",
        {"request": request, "error": None, "step": "password"})


@router.post("/login")
def login(request: Request, password: str = Form(None), totp_code: str = Form(None)):
    # Step 2: TOTP verification (password already accepted in this session)
    if request.session.get("totp_pending"):
        import pyotp
        totp = pyotp.TOTP(settings.totp_secret)
        if totp_code and totp.verify(totp_code.strip(), valid_window=1):
            request.session.pop("totp_pending", None)
            request.session["authed"] = True
            return ingress_redirect(request, "/ui/")
        return templates.TemplateResponse(request, "login.html",
            {"request": request, "error": "Invalid code: try again.", "step": "totp"},
            status_code=401)

    # Step 1: password check (hashed at rest, FoodAssistant-ufwz)
    if not (settings.auth_password and password and
            verify_secret(password, settings.auth_password)):
        return templates.TemplateResponse(request, "login.html",
            {"request": request, "error": "Incorrect password.", "step": "password"},
            status_code=401)
    # Upgrade a legacy plaintext password to a hash on the next good login.
    if not looks_hashed(settings.auth_password):
        try:
            settings.save({"auth_password": password})
        except Exception:
            pass

    if settings.totp_secret:
        request.session["totp_pending"] = True
        return templates.TemplateResponse(request, "login.html",
            {"request": request, "error": None, "step": "totp"})

    request.session["authed"] = True
    return ingress_redirect(request, "/ui/")


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return ingress_redirect(request, "/ui/login")


@router.get("/pin", response_class=HTMLResponse)
def pin_page(request: Request):
    # No PIN configured (or not a satellite): nothing to unlock.
    if not settings.pin_lock_active() or request.session.get("pin_ok"):
        return ingress_redirect(request, "/ui/")
    return templates.TemplateResponse(request, "pin.html",
        {"request": request, "error": None})


@router.post("/pin/verify")
def pin_verify(request: Request, pin: str = Form(None)):
    if not settings.pin_lock_active():
        return ingress_redirect(request, "/ui/")
    if pin and verify_secret(pin.strip(), settings.kiosk_pin):
        request.session["pin_ok"] = True
        if not looks_hashed(settings.kiosk_pin):
            try:
                settings.save({"kiosk_pin": pin.strip()})
            except Exception:
                pass
        return ingress_redirect(request, "/ui/")
    return templates.TemplateResponse(request, "pin.html",
        {"request": request, "error": "Incorrect PIN."}, status_code=401)


@router.get("/", response_class=HTMLResponse)
async def ui_index(request: Request):
    """Land on whatever page leads the nav menu, not a hardcoded one
    (Pantry Raider). When the user moves another page (e.g. the Start Page) to
    the top, /ui shows that instead of the inventory dashboard. Only redirects to
    an internal ui/ page, and never to the inventory root, to avoid a loop."""
    from ..navigation import first_visible_href
    href = (first_visible_href() or "ui/").strip()
    target = href.rstrip("/")
    if target and target != "ui" and target.startswith("ui/"):
        return ingress_redirect(request, "/" + target)
    return await inventory_page(request)


@router.get("/inventory", response_class=HTMLResponse)
async def inventory_page(request: Request):
    categories = all_categories()
    return templates.TemplateResponse(request, "inventory.html", {
        "request": request,
        "active": "inventory",
        "message": request.query_params.get("msg"),
        "message_type": request.query_params.get("msg_type", "success"),
        # Movable categories (built-in + custom) and the always-on "other" panel
        "categories": categories,
        "panels": categories + [{**OTHER, "custom": False}],
        "grocy_url": settings.grocy_link_url(),
    })


@router.get("/add", response_class=HTMLResponse)
async def add_page(request: Request):
    # Manage Pantry: one page, four scanner-mode tabs (stock up, use stock,
    # shopping list, audit). The tabs are the shared scanner mode itself, so
    # the page, the USB scanner routing, and the Stream Deck key always agree.
    return templates.TemplateResponse(request, "add.html", {
        "request": request,
        "active": "add",
        "mealie_configured": settings.mealie_configured(),
    })


@router.get("/pending", response_class=HTMLResponse)
async def pending_page(request: Request):
    return templates.TemplateResponse(request, "pending.html", {
        "request": request,
        "active": "pending",
    })


@router.get("/recipes", response_class=HTMLResponse)
async def recipes_page(request: Request):
    return templates.TemplateResponse(request, "recipes.html", {
        "request": request,
        "active": "recipes",
        "mealie_configured": settings.mealie_configured(),
        "mealie_url": settings.mealie_link_url(),
    })


@router.get("/cook", response_class=HTMLResponse)
async def cook_page(request: Request):
    return templates.TemplateResponse(request, "cook.html", {
        "request": request,
        "active": "cook",
        "mealie_configured": settings.mealie_configured(),
        "mealie_url": settings.mealie_link_url(),
    })


@router.get("/current-recipe", response_class=HTMLResponse)
async def current_recipe_page(request: Request):
    return templates.TemplateResponse(request, "current-recipe.html", {
        "request": request,
        "active": "current_recipe",
        "mealie_configured": settings.mealie_configured(),
        "mealie_url": settings.mealie_link_url(),
    })


@router.get("/recipes-in-progress")
async def recipes_in_progress_page(request: Request):
    """The In Progress view was merged into the 'On the Line' page (current
    recipe), which now shows every recipe in progress with a course selector.
    Kept as a redirect so old links/bookmarks still land somewhere (i8hz)."""
    return RedirectResponse(url="ui/current-recipe", status_code=307)


@router.get("/mealplan", response_class=HTMLResponse)
async def mealplan_page(request: Request):
    return templates.TemplateResponse(request, "mealplan.html", {
        "request": request,
        "active": "mealplan",
        "mealie_configured": settings.mealie_configured(),
        "mealie_url": settings.mealie_link_url(),
    })


@router.get("/shopping", response_class=HTMLResponse)
async def shopping_page(request: Request):
    return templates.TemplateResponse(request, "shopping.html", {
        "request": request,
        "active": "shopping",
        "mealie_configured": settings.mealie_configured(),
        "mealie_url": settings.mealie_link_url(),
    })


@router.get("/expiring", response_class=HTMLResponse)
async def expiring_page(request: Request, days: int = 7):
    grocy = GrocyClient()
    try:
        items = await grocy.get_expiring(days)
    except Exception:
        items = []
    return templates.TemplateResponse(request, "expiring.html", {
        "request": request,
        "items": items,
        "days": days,
        "active": "expiring",
        "mealie_configured": settings.mealie_configured(),
        "message": request.query_params.get("msg"),
        "message_type": request.query_params.get("msg_type", "success"),
    })


@router.post("/consume/{product_id}")
async def consume_item(request: Request, product_id: int, amount: float = Form(1.0),
                       name: str = Form("")):
    grocy = GrocyClient()
    try:
        await grocy.consume_stock(product_id, amount)
        # Name the product in the toast so it is clear what just left stock
        # (an icon-only button plus a vague toast read wrong, FoodAssistant-w0kh).
        msg = f"{name} marked as consumed." if name.strip() else "Item marked as consumed."
        msg_type = "success"
    except Exception as e:
        msg = f"Error: {e}"
        msg_type = "danger"
    from urllib.parse import quote
    return ingress_redirect(request, f"/ui/expiring?msg={quote(msg)}&msg_type={msg_type}")


@router.get("/journal", response_class=HTMLResponse)
async def journal_page(request: Request):
    return templates.TemplateResponse(request, "journal.html", {
        "request": request,
        "active": "inventory",
    })


@router.get("/defaults", response_class=HTMLResponse)
def defaults_page(
    request: Request,
    db: Session = Depends(get_db),
):
    rows = db.query(ExpiryDefault).order_by(
        ExpiryDefault.category, ExpiryDefault.name_pattern
    ).all()
    categories = sorted(set(r.category for r in rows))
    return templates.TemplateResponse(request, "defaults.html", {
        "request": request,
        "defaults": rows,
        "categories": categories,
        "active": "defaults",
        "message": request.query_params.get("msg"),
        "message_type": request.query_params.get("msg_type", "success"),
    })


@router.post("/defaults/create")
def create_default(
    request: Request,
    category: str = Form(...),
    name_pattern: str = Form(...),
    storage_type: str = Form(...),
    default_days: int = Form(...),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    row = ExpiryDefault(
        category=category,
        name_pattern=name_pattern,
        storage_type=storage_type,
        default_days=default_days,
        notes=notes or None,
        priority=1,
    )
    db.add(row)
    db.commit()
    return ingress_redirect(request, "/ui/defaults?msg=Rule+added.")


@router.post("/defaults/{default_id}/update")
def update_default(
    request: Request,
    default_id: int,
    category: str = Form(...),
    name_pattern: str = Form(...),
    storage_type: str = Form(...),
    default_days: int = Form(...),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    row = db.query(ExpiryDefault).filter(ExpiryDefault.id == default_id).first()
    if row:
        row.category = category
        row.name_pattern = name_pattern
        row.storage_type = storage_type
        row.default_days = default_days
        row.notes = notes or None
        db.commit()
    return ingress_redirect(request, "/ui/defaults?msg=Rule+updated.")


@router.get("/convert", response_class=HTMLResponse)
async def convert_page(request: Request):
    return templates.TemplateResponse(request, "convert.html", {
        "request": request,
        "active": "convert",
        "convert_custom_rows": settings.convert_custom_rows,
    })


@router.get("/kitchen-guide", response_class=HTMLResponse)
async def kitchen_guide_page(request: Request):
    """Static reference: safe cooking temperatures, doneness, substitutions,
    and technique tips (FoodAssistant-95ad). No backend data required."""
    return templates.TemplateResponse(request, "kitchen_guide.html", {
        "request": request,
        "active": "guide",
    })


@router.get("/timers", response_class=HTMLResponse)
async def timers_page(request: Request):
    return templates.TemplateResponse(request, "timers.html", {
        "request": request,
        "active": "timers",
    })


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request):
    # Pantry audit: pick a storage location, start, then scan items there. The
    # page polls /audit/status and highlights matched, missing, and unexpected
    # items. Read-only: nothing is written to Grocy (FoodAssistant-ugku).
    return templates.TemplateResponse(request, "audit.html", {
        "request": request,
        "active": "audit",
    })


@router.get("/camera", response_class=HTMLResponse)
async def camera_page(request: Request, cam: str = ""):
    # ``cam`` selects which camera to open on load (by index or name); a Stream
    # Deck camera key passes it so pressing the key pulls up the requested feed
    # rather than always camera 0 (FoodAssistant-f230). It falls back to the
    # first camera when empty, out of range, or unknown.
    from ..services.cameras import camera_sources, resolve_camera_index
    cams = settings.streamdeck_cameras
    resp = templates.TemplateResponse(request, "camera.html", {
        "request": request,
        "active": "camera",
        "cameras": camera_sources(cams),
        "initial_index": resolve_camera_index(cams, cam),
    })
    # Never let a kiosk serve a stale copy of this page: the camera list and the
    # display logic change, and a cached page would keep showing an old (broken)
    # version after an update.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/camera/diag")
async def camera_diag():
    """One-shot camera diagnostics (JSON), for troubleshooting a blank feed.

    Reports what the server sees (how many cameras, whether HA is configured)
    and, per camera, the resolved entity and the result of actually fetching its
    snapshot from Home Assistant. Tokens are never included.
    """
    import httpx
    from ..services.cameras import resolve_ha_entity, ha_feed
    cams = settings.streamdeck_cameras or []
    out = {
        "camera_count": len(cams),
        "ha_base_url_set": bool(settings.streamdeck_ha_base_url),
        "ha_token_set": bool(settings.streamdeck_ha_token),
        "ha_base_url": settings.streamdeck_ha_base_url or "",
        "cameras": [],
    }
    for idx, cam in enumerate(cams):
        if not isinstance(cam, dict):
            continue
        entity, base = resolve_ha_entity(cam)
        info = {
            "index": idx,
            "name": cam.get("name", ""),
            "is_ha": bool(entity),
            "entity": entity,
            "has_direct_snapshot": bool(cam.get("snapshot_url")),
        }
        url, headers = ha_feed(cam, "snapshot")
        if url:
            # Show the upstream path without the token (it is in the header).
            info["upstream"] = url
            try:
                async with httpx.AsyncClient(timeout=8.0) as c:
                    r = await c.get(url, headers=headers)
                info["fetch_status"] = r.status_code
                info["fetch_bytes"] = len(r.content) if r.status_code == 200 else 0
                info["content_type"] = r.headers.get("content-type", "")
            except Exception as e:
                info["fetch_error"] = str(e)
        out["cameras"].append(info)
    return out


@router.get("/camera/preview")
async def camera_preview(entity: str = "", snapshot_url: str = ""):
    """Proxy a still frame for a not-yet-added camera, so the Cameras setup page
    can preview a discovered camera before adding it (FoodAssistant-kval).

    An HA ``entity`` is fetched server-side with the bearer token; a direct
    ``snapshot_url`` (an IP camera) is redirected to so the browser fetches it."""
    import httpx
    from fastapi.responses import Response, RedirectResponse
    from ..services.cameras import ha_feed
    entity = (entity or "").strip()
    snapshot_url = (snapshot_url or "").strip()
    if entity:
        url, headers = ha_feed({"ha_entity": entity}, "snapshot")
        if url:
            try:
                async with httpx.AsyncClient(timeout=8.0) as c:
                    r = await c.get(url, headers=headers)
            except Exception as e:
                return JSONResponse({"detail": f"Camera unreachable: {e}"}, status_code=502)
            if r.status_code != 200:
                return JSONResponse({"detail": f"Camera returned HTTP {r.status_code}."}, status_code=502)
            return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))
    if snapshot_url:
        return RedirectResponse(snapshot_url)
    return JSONResponse({"detail": "Nothing to preview."}, status_code=404)


@router.get("/camera/{idx}/snapshot")
async def camera_snapshot(idx: int):
    """Proxy a still frame for camera ``idx``.

    Home Assistant cameras are fetched server-side with the bearer token (a
    browser cannot send that header), then handed back as an image. Manual
    cameras redirect to their own snapshot URL, which the browser can fetch.
    """
    import httpx
    from fastapi.responses import Response, RedirectResponse
    from ..services.cameras import ha_feed
    cams = settings.streamdeck_cameras or []
    if idx < 0 or idx >= len(cams):
        return JSONResponse({"detail": "Unknown camera."}, status_code=404)
    entry = cams[idx]
    url, headers = ha_feed(entry, "snapshot")
    if url:
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(url, headers=headers)
        except Exception as e:
            return JSONResponse({"detail": f"Camera unreachable: {e}"}, status_code=502)
        if r.status_code != 200:
            return JSONResponse({"detail": f"Camera returned HTTP {r.status_code}."}, status_code=502)
        media = r.headers.get("content-type", "image/jpeg")
        return Response(content=r.content, media_type=media)
    direct = (entry.get("snapshot_url") or "").strip() if isinstance(entry, dict) else ""
    if direct:
        return RedirectResponse(direct)
    return JSONResponse({"detail": "Camera has no snapshot."}, status_code=404)


@router.get("/camera/{idx}/stream")
async def camera_stream(idx: int):
    """Proxy the live MJPEG stream for camera ``idx``.

    The upstream multipart stream is relayed as-is so an ``<img>`` keeps
    updating. Home Assistant cameras carry the bearer header server-side; manual
    cameras redirect to their own stream URL.
    """
    import httpx
    from fastapi.responses import StreamingResponse, RedirectResponse
    from starlette.background import BackgroundTask
    from ..services.cameras import ha_feed
    cams = settings.streamdeck_cameras or []
    if idx < 0 or idx >= len(cams):
        return JSONResponse({"detail": "Unknown camera."}, status_code=404)
    entry = cams[idx]
    url, headers = ha_feed(entry, "stream")
    if url:
        client = httpx.AsyncClient(timeout=None)
        try:
            req = client.build_request("GET", url, headers=headers)
            upstream = await client.send(req, stream=True)
        except Exception as e:
            await client.aclose()
            return JSONResponse({"detail": f"Camera unreachable: {e}"}, status_code=502)
        if upstream.status_code != 200:
            await upstream.aclose()
            await client.aclose()
            return JSONResponse({"detail": f"Camera returned HTTP {upstream.status_code}."}, status_code=502)

        async def _close():
            await upstream.aclose()
            await client.aclose()

        media = upstream.headers.get("content-type", "multipart/x-mixed-replace")
        return StreamingResponse(upstream.aiter_raw(), media_type=media,
                                 background=BackgroundTask(_close))
    direct = (entry.get("stream_url") or "").strip() if isinstance(entry, dict) else ""
    if direct:
        return RedirectResponse(direct)
    return JSONResponse({"detail": "Camera has no stream."}, status_code=404)


@router.get("/start", response_class=HTMLResponse)
async def start_page(request: Request):
    """Optional full-screen on-screen Start Page (Pantry Raider): a launcher grid
    that works like an on-screen Stream Deck. Off by default; when disabled it
    still renders (so the Settings preview link works) but is plain."""
    from ..services import start_page as sp
    keys = sp.normalize_key_count(settings.start_page_keys)
    cols, rows = sp.GRID_SHAPES[keys]
    catalog = await sp.fetch_deck_catalog()
    layout = sp.resolve_layout(settings.start_page_layout, keys, catalog=catalog)
    return templates.TemplateResponse(request, "start.html", {
        "request": request,
        "enabled": settings.start_page_enabled,
        "keys": keys,
        "cols": cols,
        "rows": rows,
        "layout": layout,
    })


@router.post("/start/fire/{key_name}")
async def start_fire(key_name: str):
    """Execute a deck-only Start Page key server-side (HA toggle, media, macro,
    or a built-in ha_1..ha_5 slot key). Always answers 200 with a toastable
    {ok, detail} so the Start Page can report the outcome inline."""
    from ..services import start_actions
    return JSONResponse(await start_actions.fire_key(key_name))


@router.get("/weather", response_class=HTMLResponse)
async def weather_page(request: Request):
    # Full-screen forecast for the kiosk. Opened by the Stream Deck weather and
    # forecast keys, and reachable from the nav. Uses the same location/units the
    # deck weather widget uses (streamdeck_weather_location/units); a blank
    # location lets the forecast service auto-detect from the device IP.
    return templates.TemplateResponse(request, "weather.html", {
        "request": request,
        "active": "weather",
        "weather_location": settings.streamdeck_weather_location or "",
        "weather_units": settings.streamdeck_weather_units or "f",
        "weather_api_base": settings.weather_api_base or "",
    })


@router.get("/weather/data")
async def weather_data(location: str | None = None, units: str | None = None):
    """Server-side forecast for the kiosk weather page and the Stream Deck
    weather tiles (FoodAssistant-afqd, 34k7).

    Fetches Open-Meteo first (honouring weather_api_base) with wttr.in as the
    fallback, so neither surface depends on a single flaky provider or the
    kiosk's own internet access. ``location`` and ``units`` override the saved
    ones (per-key deck overrides carry their own). Returns {ok, forecast} or
    {ok: false, error} with the reason it failed, so callers can show
    something actionable.

    Results go through a shared in-process TTL cache keyed by (location,
    units), so several deck tiles plus the weather page cost one upstream
    fetch per location per window instead of one each (FoodAssistant-17tb)."""
    from ..services import weather as weather_svc
    loc = location if location is not None else (settings.streamdeck_weather_location or "")
    u = (units or "").strip().lower()
    if u not in ("f", "c"):
        u = settings.streamdeck_weather_units or "f"
    forecast, error = await weather_svc.fetch_forecast_cached(loc, u)
    if forecast is None:
        return {"ok": False, "error": error, "location": loc}
    return {"ok": True, "forecast": forecast, "location": loc}


@router.get("/screensaver/photos")
async def screensaver_photos():
    """Photo names for the screensaver slideshow (FoodAssistant-5w4m).

    On a Pi appliance the attached flash drive is only visible to the host,
    so the list comes from the bridge (GET /usb/photos: images in the drive's
    pictures/ or photos/ folder). Elsewhere there is no drive to read, so the
    list is empty and the screensaver falls back to the bouncing logo. Never
    raises; any failure is an empty list for the same reason."""
    photos: list = []
    if settings.is_pi_appliance():
        from ..services.usb_backup import _bridge_get
        data = await _bridge_get("/usb/photos")
        got = data.get("photos") if isinstance(data, dict) else None
        if isinstance(got, list):
            photos = [n for n in got if isinstance(n, str)]
    # A short cache keeps repeated saver starts cheap without hiding a newly
    # plugged-in drive for long.
    return JSONResponse({"ok": True, "photos": photos},
                        headers={"Cache-Control": "private, max-age=60"})


@router.get("/screensaver/photo")
async def screensaver_photo(name: str = ""):
    """Proxy one slideshow image from the bridge (GET /usb/photo).

    The bridge does the path-safety and size checks; this just relays the
    bytes. Cached for an hour so the kiosk browser does not refetch the same
    photo on every slideshow cycle."""
    if not settings.is_pi_appliance() or not name:
        return JSONResponse({"detail": "Photo not found."}, status_code=404)
    import httpx
    from fastapi.responses import Response
    from ..services.usb_backup import _BRIDGE
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(f"{_BRIDGE}/usb/photo", params={"path": name})
    except Exception as e:
        return JSONResponse({"detail": f"Photo unavailable: {e.__class__.__name__}"},
                            status_code=502)
    if r.status_code != 200:
        return JSONResponse({"detail": "Photo not found."}, status_code=404)
    return Response(content=r.content,
                    media_type=r.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "private, max-age=3600"})


@router.post("/screensaver/state")
async def screensaver_state_post(request: Request):
    """Record the kiosk screensaver's logo position (FoodAssistant-3fdq).

    Posted by the kiosk browser a few times a second while the bouncing-logo
    saver is up (and once with active=false when it hides), in
    panel-normalized units. The reply carries {"dismiss": true} when a Stream
    Deck key press asked for the saver to end since the previous post, so the
    kiosk hides the overlay without any new polling loop of its own."""
    from ..services import screensaver_state
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    def _num(key: str) -> float:
        v = body.get(key, 0.0)
        return float(v) if isinstance(v, (int, float)) else 0.0

    result = screensaver_state.update(
        active=bool(body.get("active")),
        x=_num("x"), y=_num("y"), w=_num("w"), h=_num("h"),
        band=_num("band"),
        layout=str(body.get("layout") or "off"),
    )
    return {"ok": True, **result}


@router.get("/screensaver/state")
async def screensaver_state_get():
    """Current saver state, polled by the Stream Deck controller. A state the
    kiosk has not refreshed recently reads as inactive, so a dead kiosk never
    leaves the deck frozen mid-logo."""
    from ..services import screensaver_state
    return {"ok": True, **screensaver_state.snapshot()}


@router.post("/screensaver/dismiss")
async def screensaver_dismiss():
    """End the saver from another surface (a Stream Deck key press). The mark
    is delivered to the kiosk on its next state post."""
    from ..services import screensaver_state
    screensaver_state.dismiss()
    return {"ok": True}


@router.get("/nutrition", response_class=HTMLResponse)
async def nutrition_page(request: Request):
    """Food-intake / nutrition tracker (FoodAssistant-e6qt)."""
    return templates.TemplateResponse(request, "nutrition.html", {
        "request": request,
        "active": "nutrition",
        "ai_configured": settings.ai_configured(),
    })


@router.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    return templates.TemplateResponse(request, "about.html", {
        "request": request,
        "active": "about",
    })


@router.post("/defaults/{default_id}/delete")
def delete_default(request: Request, default_id: int, db: Session = Depends(get_db)):
    row = db.query(ExpiryDefault).filter(ExpiryDefault.id == default_id).first()
    if row:
        db.delete(row)
        db.commit()
    return ingress_redirect(request, "/ui/defaults?msg=Rule+deleted.&msg_type=warning")
