import secrets
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from typing import Optional

from ..config import settings
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

    # Step 1: password check
    if not (settings.auth_password and password and
            secrets.compare_digest(password, settings.auth_password)):
        return templates.TemplateResponse(request, "login.html",
            {"request": request, "error": "Incorrect password.", "step": "password"},
            status_code=401)

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
    if pin and secrets.compare_digest(pin.strip(), settings.kiosk_pin):
        request.session["pin_ok"] = True
        return ingress_redirect(request, "/ui/")
    return templates.TemplateResponse(request, "pin.html",
        {"request": request, "error": "Incorrect PIN."}, status_code=401)


@router.get("/", response_class=HTMLResponse)
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
    return templates.TemplateResponse(request, "add.html", {
        "request": request,
        "active": "add",
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
        "message": request.query_params.get("msg"),
        "message_type": request.query_params.get("msg_type", "success"),
    })


@router.post("/consume/{product_id}")
async def consume_item(request: Request, product_id: int, amount: float = Form(1.0)):
    grocy = GrocyClient()
    try:
        await grocy.consume_stock(product_id, amount)
        msg = "Item marked as consumed."
        msg_type = "success"
    except Exception as e:
        msg = f"Error: {e}"
        msg_type = "danger"
    return ingress_redirect(request, f"/ui/expiring?msg={msg}&msg_type={msg_type}")


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


@router.get("/camera", response_class=HTMLResponse)
async def camera_page(request: Request):
    from ..services.cameras import camera_sources
    resp = templates.TemplateResponse(request, "camera.html", {
        "request": request,
        "active": "camera",
        "cameras": camera_sources(settings.streamdeck_cameras),
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
    })


@router.get("/weather/data")
async def weather_data(location: str | None = None):
    """Server-side forecast for the kiosk weather page (FoodAssistant-afqd).

    Fetches wttr.in's reliable JSON API on the server (the same path the Stream
    Deck weather widget uses) so the page does not depend on the flaky PNG
    endpoint or the kiosk's own internet access. ``location`` overrides the saved
    one (handy for diagnosing). Returns {ok, forecast} or {ok: false, error}
    with the reason it failed, so the page can show something actionable."""
    from ..services import weather as weather_svc
    loc = location if location is not None else (settings.streamdeck_weather_location or "")
    forecast, error = await weather_svc.fetch_forecast(
        loc, settings.streamdeck_weather_units or "f",
    )
    if forecast is None:
        return {"ok": False, "error": error, "location": loc}
    return {"ok": True, "forecast": forecast, "location": loc}


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
