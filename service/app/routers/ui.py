import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from typing import Optional

from ..config import settings, BUYMEACOFFEE_URL, APP_VERSION
from ..passwords import verify_secret, looks_hashed
from .. import totp as local_totp
from ..database import get_db
from ..ingress import ingress_redirect
from ..models.db_models import ExpiryDefault
from ..services.grocy import GrocyClient
from ..services.request_origin import is_internet_request
from ..storage_categories import all_categories, OTHER
from ..templating import templates

router = APIRouter(prefix="/ui", tags=["ui"])

# The honest refusal when a local password login arrives over the internet on a
# device that has no device 2FA to challenge with. The kitchen owner either
# signs in with Forager (the cloud enforces the account's 2FA) or sets up device
# 2FA from the safety of the home network first.
_LOCAL_INTERNET_MSG = (
    "Signing in from outside your home network needs a second factor. Sign in "
    "with Forager below, or set up two-factor authentication for this device "
    "from your home network first.")

_CLOUD_TIMEOUT = httpx.Timeout(8.0, connect=5.0)


def _is_internet_request(request: Request) -> bool:
    """Whether this login arrived over the Forager tunnel (the public web
    address) rather than the LAN or the local kiosk. Thin wrapper over the pure
    helper so the rule stays unit-testable."""
    return is_internet_request(
        request.headers.get("host", ""), settings.qr_public_url,
        settings.tunnel_enabled,
        request.client.host if request.client else None)


def _render_login(request, *, step="password", status_code=200, **ctx):
    base = {
        "request": request, "step": step, "error": None,
        "cloud_linked": settings.cloud_linked(),
        "forager_error": None, "forager_notice": None,
        "forager_need_code": False, "forager_email": "", "forager_password": "",
    }
    base.update(ctx)
    return templates.TemplateResponse(request, "login.html", base,
                                      status_code=status_code)


def _consume_local_recovery(code: str) -> bool:
    """Try a local-2FA recovery code, burning it on a match (persisted)."""
    matched, remaining = local_totp.consume_recovery_code(
        code, settings.local_totp_recovery)
    if matched:
        try:
            settings.save({"local_totp_recovery": remaining})
        except Exception:
            pass
    return matched


async def _forager_login(request: Request, email: str, password: str, code: str):
    """The "Sign in with Forager" path: confirm the account credentials (and the
    account's 2FA, which the cloud enforces) against the linked account, then
    open the normal local session on success."""
    email = (email or "").strip()
    password = password or ""
    if not settings.cloud_linked():
        return _render_login(request, status_code=400,
                             forager_error="This device is not connected to Forager yet.")
    if not email or not password:
        return _render_login(request, status_code=400,
                             forager_email=email,
                             forager_error="Enter your Forager email and password.")
    base = settings.cloud_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.cloud_instance_token}",
               "X-Device-Version": APP_VERSION,
               "X-Device-Mode": settings.deployment_mode or "server"}
    try:
        async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as client:
            r = await client.post(f"{base}/v1/instance/verify-login",
                                  headers=headers,
                                  json={"email": email, "password": password,
                                        "totp": (code or "").strip()})
    except httpx.HTTPError:
        # Honest degraded error: keep what was typed so a retry is one click.
        return _render_login(
            request, status_code=502, forager_email=email,
            forager_password=password, forager_need_code=bool(code),
            forager_error=("Forager could not be reached. Check the internet "
                           "connection and try again, or use your device password."))
    if r.status_code == 200 and (r.json() or {}).get("ok"):
        request.session["authed"] = True
        request.session["role"] = "admin"
        return ingress_redirect(request, "/ui/")
    err = ""
    try:
        err = (r.json() or {}).get("error", "")
    except ValueError:
        err = ""
    if err == "totp_required":
        return _render_login(
            request, status_code=401, forager_email=email,
            forager_password=password, forager_need_code=True,
            forager_notice="Enter the code from your authenticator app to finish.")
    if err == "totp_invalid":
        return _render_login(
            request, status_code=401, forager_email=email,
            forager_password=password, forager_need_code=True,
            forager_error="That code did not match. Try again.")
    return _render_login(request, status_code=401, forager_email=email,
                        forager_error="That email or password was not right.")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not settings.auth_password or request.session.get("authed"):
        return ingress_redirect(request, "/ui/")
    if request.session.get("totp_pending"):
        return _render_login(request, step="totp")
    return _render_login(request, step="password")


@router.post("/login")
async def login(request: Request, mode: str = Form(""), password: str = Form(None),
                totp_code: str = Form(None), email: str = Form(None),
                fpass: str = Form(None), fcode: str = Form(None)):
    # Step 2: local TOTP challenge (the password was accepted this session).
    if request.session.get("totp_pending"):
        secret = settings.local_totp_effective_secret()
        code = (totp_code or "").strip()
        if code and (local_totp.totp_verify(secret, code) or _consume_local_recovery(code)):
            role = request.session.pop("pending_role", "admin")
            request.session.pop("totp_pending", None)
            request.session["authed"] = True
            request.session["role"] = role
            return ingress_redirect(request, "/ui/")
        return _render_login(request, step="totp", status_code=401,
                            error="That code did not match. Try again.")

    # The "Sign in with Forager" form: email + password (+ code) checked against
    # the linked account by the cloud, which owns that account's 2FA.
    if mode == "forager":
        return await _forager_login(request, email, fpass, fcode)

    internet = _is_internet_request(request)

    # Local password (hashed at rest, FoodAssistant-ufwz). The admin password is
    # tried first; a viewer password opens a kitchen-only session instead.
    if settings.auth_password and password and verify_secret(password, settings.auth_password):
        if not looks_hashed(settings.auth_password):
            try:
                settings.save({"auth_password": password})
            except Exception:
                pass
        if settings.local_2fa_active():
            # A second factor is required next: on the LAN because 2FA is on, and
            # over the internet because it is forced there.
            request.session["totp_pending"] = True
            request.session["pending_role"] = "admin"
            return _render_login(request, step="totp")
        if internet:
            # No device 2FA to challenge with, so a bare password cannot complete
            # over the internet (FoodAssistant-x1ty).
            return _render_login(request, status_code=401, error=_LOCAL_INTERNET_MSG)
        request.session["authed"] = True
        request.session["role"] = "admin"
        return ingress_redirect(request, "/ui/")

    if settings.viewer_password and password and verify_secret(password, settings.viewer_password):
        if not looks_hashed(settings.viewer_password):
            try:
                settings.save({"viewer_password": password})
            except Exception:
                pass
        # A viewer session has no second factor of its own, so it stays a LAN /
        # loopback login: over the internet it is refused like any bare password.
        if internet:
            return _render_login(request, status_code=401, error=_LOCAL_INTERNET_MSG)
        request.session["authed"] = True
        request.session["role"] = "viewer"
        return ingress_redirect(request, "/ui/")

    return _render_login(request, status_code=401, error="Incorrect password.")


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
        # Whether the Share-to-community action and the community source show
        # (FoodAssistant-l2hk): the install must be linked to a Forager account.
        "forager_linked": settings.cloud_linked(),
        "forager_recipes_active": settings.forager_recipes_active(),
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
    grocy_error = None
    try:
        items = await grocy.get_expiring(days)
    except Exception as e:
        # Keep the page shell up during a Grocy outage, but say so honestly:
        # an empty list would read as "all clear" (FoodAssistant-2cmm).
        items = []
        grocy_error = str(e) or "Grocy is not reachable."
    return templates.TemplateResponse(request, "expiring.html", {
        "request": request,
        "items": items,
        "grocy_error": grocy_error,
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


@router.get("/scanner-setup", response_class=HTMLResponse)
async def scanner_setup_page(request: Request, model: str = ""):
    """Barcode-scanner setup wizard (FoodAssistant-udpk).

    Shows a scan-engine module's configuration codes one at a time so the reader
    can program itself off the screen. The `model` query parameter picks which
    reader's code sequence to show; a blank or unknown value falls back to the
    reader the saved scanner_type points at, else the recommended default. The
    whole sequence is rendered and the page steps through it client-side, so a
    kiosk and a phone both work with no extra round trips.
    """
    from ..services import scanner_wizard
    from .qr import lan_url_for

    chosen = model or scanner_wizard.default_model_for(settings.scanner_type)
    active = scanner_wizard.get_model(chosen)
    return templates.TemplateResponse(request, "scanner_setup.html", {
        "request": request,
        "active": "scanner_setup",
        "models": scanner_wizard.MODELS,
        "model": active,
        "model_id": active["id"],
        # LAN link to this same wizard, encoded as the mobile-launch QR so a
        # user can run it on a phone by the reader instead of at the kiosk.
        "wizard_lan_url": lan_url_for(request, f"/ui/scanner-setup?model={active['id']}"),
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
    from fastapi.responses import Response
    from ..services.cameras import ha_feed, is_blocked_fetch_host, BLOCKED_HOST_MESSAGE
    entity = (entity or "").strip()
    snapshot_url = (snapshot_url or "").strip()
    if entity:
        url, headers = ha_feed({"ha_entity": entity}, "snapshot")
        if url:
            if is_blocked_fetch_host(url, fail_closed=False):
                return JSONResponse({"detail": BLOCKED_HOST_MESSAGE}, status_code=400)
            try:
                async with httpx.AsyncClient(timeout=8.0) as c:
                    r = await c.get(url, headers=headers)
            except Exception as e:
                return JSONResponse({"detail": f"Camera unreachable: {e}"}, status_code=502)
            if r.status_code != 200:
                return JSONResponse({"detail": f"Camera returned HTTP {r.status_code}."}, status_code=502)
            return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))
    if snapshot_url.startswith(("http://", "https://")):
        # Never let a previewed URL point the server back at itself or an
        # internal-only address (SSRF, FoodAssistant-e9al): the server would
        # otherwise fetch loopback as a trusted local admin, or reach the cloud
        # metadata address. LAN and public camera addresses stay allowed.
        if is_blocked_fetch_host(snapshot_url):
            return JSONResponse({"detail": BLOCKED_HOST_MESSAGE}, status_code=400)
        # Fetch server-side rather than redirect: a plain http camera (Frigate,
        # an IP cam) previewed from an https page would be blocked as mixed
        # content, and the browser may not reach a camera the server can
        # (FoodAssistant-p1w5).
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(snapshot_url)
        except Exception as e:
            return JSONResponse({"detail": f"Camera unreachable: {e}"}, status_code=502)
        if r.status_code != 200:
            return JSONResponse({"detail": f"Camera returned HTTP {r.status_code}."}, status_code=502)
        return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))
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
    from ..services.cameras import (
        proxied_snapshot, is_reolink, fetch_reolink_snapshot, ReolinkAuthError,
        _looks_like_jpeg, is_blocked_fetch_host, BLOCKED_HOST_MESSAGE)
    cams = settings.streamdeck_cameras or []
    if idx < 0 or idx >= len(cams):
        return JSONResponse({"detail": "Unknown camera."}, status_code=404)
    entry = cams[idx]
    # A Reolink camera signs in for a short-lived token, then fetches the still
    # with that token; both stay server-side so no login reaches the browser.
    if is_reolink(entry):
        host = entry.get("host", "") if isinstance(entry, dict) else ""
        if is_blocked_fetch_host(host, fail_closed=False):
            return JSONResponse({"detail": BLOCKED_HOST_MESSAGE}, status_code=400)
        try:
            status, content, ctype = await fetch_reolink_snapshot(entry)
        except ReolinkAuthError:
            return JSONResponse(
                {"detail": "The camera rejected that username or password."},
                status_code=502)
        except Exception as e:
            return JSONResponse({"detail": f"Camera unreachable: {e}"}, status_code=502)
        if not _looks_like_jpeg(status, content, ctype):
            return JSONResponse(
                {"detail": f"The camera did not return a snapshot (HTTP {status})."},
                status_code=502)
        return Response(content=content, media_type=ctype or "image/jpeg")
    # HA (bearer header) feeds are fetched server-side so no credential ever
    # reaches the browser; a plain manual / Frigate camera carries no secret and
    # is redirected to below.
    url, headers = proxied_snapshot(entry)
    if url:
        # Refuse a saved camera whose address resolves to the server itself or an
        # internal-only address (SSRF, FoodAssistant-e9al). LAN and public
        # camera addresses stay allowed.
        if is_blocked_fetch_host(url, fail_closed=False):
            return JSONResponse({"detail": BLOCKED_HOST_MESSAGE}, status_code=400)
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
    from ..services.cameras import ha_feed, is_blocked_fetch_host, BLOCKED_HOST_MESSAGE
    cams = settings.streamdeck_cameras or []
    if idx < 0 or idx >= len(cams):
        return JSONResponse({"detail": "Unknown camera."}, status_code=404)
    entry = cams[idx]
    url, headers = ha_feed(entry, "stream")
    if url:
        # Never relay a server-side stream from the app's own address or an
        # internal-only one (SSRF, FoodAssistant-e9al).
        if is_blocked_fetch_host(url, fail_closed=False):
            return JSONResponse({"detail": BLOCKED_HOST_MESSAGE}, status_code=400)
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
        # The live weather/forecast faces show the temperature unit symbol; use
        # the same saved units the deck and the weather page use.
        "weather_units": (settings.streamdeck_weather_units or "f").lower(),
    })


@router.post("/start/fire/{key_name}")
async def start_fire(key_name: str, long: bool = False):
    """Execute a deck-only Start Page key server-side (HA toggle, media, macro,
    or a built-in ha_1..ha_5 slot key). Always answers 200 with a toastable
    {ok, detail} so the Start Page can report the outcome inline."""
    from ..services import start_actions
    return JSONResponse(await start_actions.fire_key(key_name, long=long))


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
    # 12/24-hour setting: re-read the hourly strip and sunrise/sunset labels
    # without touching the cached copy, so the page and the deck tiles agree
    # with every other clock in the kitchen.
    forecast = weather_svc.apply_clock_format(forecast, settings.clock_format)
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
        "buymeacoffee_url": BUYMEACOFFEE_URL,
    })


@router.post("/defaults/{default_id}/delete")
def delete_default(request: Request, default_id: int, db: Session = Depends(get_db)):
    row = db.query(ExpiryDefault).filter(ExpiryDefault.id == default_id).first()
    if row:
        db.delete(row)
        db.commit()
    return ingress_redirect(request, "/ui/defaults?msg=Rule+deleted.&msg_type=warning")
