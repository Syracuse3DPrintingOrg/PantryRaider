import time

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
from urllib.parse import quote as _quote, urlencode as _urlencode

from ..config import settings, BUYMEACOFFEE_URL, APP_VERSION, GITHUB_REPO
from ..passwords import verify_secret, looks_hashed
from .. import totp as local_totp
from ..database import get_db
from ..ingress import ingress_path, ingress_redirect
from ..models.db_models import ExpiryDefault
from ..services.grocy import GrocyClient
from ..services.request_origin import is_internet_origin, has_forwarding_headers
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
    """Whether this login should be treated as arriving from the internet, so a
    second factor is required.

    Not just the built-in Forager tunnel: a request that came through ANY
    reverse proxy (a forwarding header is present) or straight from a public
    address counts too, so a bare password is never accepted as a single factor
    from outside the home network, whatever front-end is used (FoodAssistant-
    7svb). Thin wrapper over the pure helper so the rule stays unit-testable."""
    return is_internet_origin(
        request.headers.get("host", ""), settings.qr_public_url,
        settings.tunnel_enabled,
        request.client.host if request.client else None,
        has_forwarding_headers(request.headers))


def _render_login(request, *, step="password", status_code=200, **ctx):
    base = {
        "request": request, "step": step, "error": None,
        "cloud_linked": settings.cloud_linked(),
        "google_unlock": _google_unlock_available(),
        "forager_error": None, "forager_notice": None,
        "forager_need_code": False, "forager_email": "", "forager_password": "",
    }
    base.update(ctx)
    return templates.TemplateResponse(request, "login.html", base,
                                      status_code=status_code)


# --- "Sign in with Google" on the login page (FoodAssistant-cd34) -----------
#
# A Forager account created with Google has no password, so the email+password
# form above cannot unlock the device for its owner. The login page therefore
# offers the cloud's Google sign-in, gated on the same capability source the
# setup wizard uses (/v1/meta), fetched server-side into the template context.
# Both legs of the flow ride the /ui/login path itself (?google=start leaves
# for the cloud, ?code=... is the cloud's return), which is already public and
# PIN-exempt, so no middleware changes are needed.
#
# The meta answer is cached briefly: the GET login page refreshes it (with a
# short timeout so a dead cloud cannot hang the page), and every re-render
# after a failed POST reads the cached value without touching the network.
_GOOGLE_META_TTL = 60.0
_GOOGLE_META_TIMEOUT = httpx.Timeout(3.0, connect=2.0)
# ts is monotonic-clock based; it starts at -inf (never fetched) because a
# plain 0.0 would read as "fresh" on a machine whose uptime is under the TTL,
# which is exactly a Pi kiosk booting straight to the login page.
_google_meta_cache = {"ts": float("-inf"), "ok": False}

# Friendly refusals for the Google unlock legs. User-forward, and none of them
# ever echoes the code or anything else from the request.
_GOOGLE_MISMATCH_MSG = (
    "That Google account is not the one this kitchen is connected to. Sign in "
    "with the Google account you use for Forager, or use your device password.")
_GOOGLE_EXPIRED_MSG = (
    "That Google sign-in expired or was already used. Try again, or use your "
    "device password.")
_GOOGLE_NO_ACCOUNT_MSG = (
    "No Forager account uses that Google address. Use your device password, "
    "or sign in with your Forager email and password.")


def _google_unlock_available() -> bool:
    """Whether the login page should offer "Sign in with Google": the install
    is linked AND the cloud's last meta answer offered the unlock flow."""
    return bool(settings.cloud_linked() and _google_meta_cache["ok"])


async def _refresh_google_unlock() -> None:
    """Refresh the cached cloud capability answer when it has gone stale.

    Degrades to "no button" on an unreachable or older cloud (one that does
    not answer google_unlock yet), never to an error. The timestamp is set
    before the call so a dead cloud is retried once per TTL, not per render.
    """
    if not settings.cloud_linked():
        return
    now = time.monotonic()
    if now - _google_meta_cache["ts"] < _GOOGLE_META_TTL:
        return
    _google_meta_cache["ts"] = now
    ok = False
    try:
        async with httpx.AsyncClient(timeout=_GOOGLE_META_TIMEOUT) as client:
            r = await client.get(f"{settings.cloud_base_url.rstrip('/')}/v1/meta")
        body = r.json() if r.status_code == 200 else {}
        body = body if isinstance(body, dict) else {}
        ok = bool(body.get("oauth_google")) and bool(body.get("google_unlock"))
    except (httpx.HTTPError, ValueError):
        ok = False
    _google_meta_cache["ok"] = ok


def _request_origin(request: Request) -> str:
    """The scheme://host the browser used for this request, for building the
    address the cloud sends the browser back to. A proxy's forwarded scheme
    wins when present, so a tunneled https page is not sent back to http."""
    scheme = (request.headers.get("x-forwarded-proto")
              or request.url.scheme or "http").split(",")[0].strip()
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


def _google_unlock_start(request: Request):
    """Leave for the cloud's Google sign-in (flow=unlock). The destination is
    built only from the configured cloud address, never from user input, so
    this public leg cannot be turned into an open redirect."""
    if not settings.cloud_linked():
        return ingress_redirect(request, "/ui/login")
    return_url = _request_origin(request) + ingress_path(request) + "/ui/login"
    base = settings.cloud_base_url.rstrip("/")
    query = _urlencode({"flow": "unlock", "return_url": return_url})
    return RedirectResponse(f"{base}/auth/google/start?{query}",
                            status_code=303)


async def _google_unlock_return(request: Request, code: str):
    """The cloud's return leg: spend the single-use unlock code at
    verify-unlock, which confirms the Google sign-in belongs to the SAME
    account this install is linked to, then open the normal session.

    Unlike the mode=forager form this endpoint accepts an attacker-suppliable
    ?code= directly (the cloud's per-instance throttle is only reached after a
    round trip), so failures feed the same local limiter as bad passwords.
    The code is never logged and never echoed into the page."""
    from ..services.rate_limit import login_guard, client_key
    key = client_key(request)
    if login_guard.blocked(key):
        wait = login_guard.retry_after(key)
        return _render_login(
            request, status_code=429,
            error=f"Too many attempts. Wait about {max(1, wait // 60)} minute(s) "
                  "and try again.")
    if not settings.cloud_linked():
        # The top-level error slot: the Forager block (and its own message
        # slot) only renders on a linked install.
        return _render_login(request, status_code=400,
                             error="This device is not connected to Forager yet.")
    base = settings.cloud_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.cloud_instance_token}",
               "X-Device-Version": APP_VERSION,
               "X-Device-Mode": settings.deployment_mode or "server"}
    try:
        async with httpx.AsyncClient(timeout=_CLOUD_TIMEOUT) as client:
            r = await client.post(f"{base}/v1/instance/verify-unlock",
                                  headers=headers,
                                  json={"code": code.strip()})
    except httpx.HTTPError:
        return _render_login(
            request, status_code=502,
            forager_error=("Forager could not be reached. Check the internet "
                           "connection and try again, or use your device "
                           "password."))
    if r.status_code == 200 and (r.json() or {}).get("ok"):
        login_guard.reset(key)
        request.session["authed"] = True
        request.session["role"] = "admin"
        return ingress_redirect(request, "/ui/")
    err = ""
    try:
        err = (r.json() or {}).get("error", "")
    except ValueError:
        err = ""
    login_guard.record(key)
    msg = _GOOGLE_MISMATCH_MSG if err == "account_mismatch" else _GOOGLE_EXPIRED_MSG
    return _render_login(request, status_code=401, forager_error=msg)


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
async def login_page(request: Request, google: str = "", code: str = "",
                     error: str = ""):
    if not settings.auth_password or request.session.get("authed"):
        return ingress_redirect(request, "/ui/")
    if request.session.get("totp_pending"):
        return _render_login(request, step="totp")
    # The Google unlock legs ride this already-public path: ?google=start
    # leaves for the cloud's sign-in, ?code=... is the cloud's return with the
    # single-use unlock code, and ?error=no-account is its friendly refusal
    # when the Google address has no Forager account.
    if google == "start":
        return _google_unlock_start(request)
    if code:
        return await _google_unlock_return(request, code)
    await _refresh_google_unlock()
    if error == "no-account":
        return _render_login(request, forager_error=_GOOGLE_NO_ACCOUNT_MSG)
    return _render_login(request, step="password")


@router.post("/login")
async def login(request: Request, mode: str = Form(""), password: str = Form(None),
                totp_code: str = Form(None), email: str = Form(None),
                fpass: str = Form(None), fcode: str = Form(None)):
    # Throttle brute force against the local password / TOTP (FoodAssistant-7svb).
    # The Forager path is checked by the cloud (its own throttling), so it is not
    # counted here.
    from ..services.rate_limit import login_guard, client_key
    key = client_key(request)
    if mode != "forager" and login_guard.blocked(key):
        wait = login_guard.retry_after(key)
        step = "totp" if request.session.get("totp_pending") else "password"
        return _render_login(
            request, step=step, status_code=429,
            error=f"Too many attempts. Wait about {max(1, wait // 60)} minute(s) "
                  "and try again.")

    # Step 2: local TOTP challenge (the password was accepted this session).
    if request.session.get("totp_pending"):
        secret = settings.local_totp_effective_secret()
        code = (totp_code or "").strip()
        if code and (local_totp.totp_verify(secret, code) or _consume_local_recovery(code)):
            role = request.session.pop("pending_role", "admin")
            request.session.pop("totp_pending", None)
            request.session["authed"] = True
            request.session["role"] = role
            login_guard.reset(key)
            return ingress_redirect(request, "/ui/")
        login_guard.record(key)
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
        login_guard.reset(key)
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
        login_guard.reset(key)
        request.session["authed"] = True
        request.session["role"] = "viewer"
        return ingress_redirect(request, "/ui/")

    login_guard.record(key)
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
    # A short numeric PIN is the most guessable secret in the app, so throttle it
    # (FoodAssistant-7svb).
    from ..services.rate_limit import pin_guard, client_key
    key = client_key(request)
    if pin_guard.blocked(key):
        wait = pin_guard.retry_after(key)
        return templates.TemplateResponse(request, "pin.html",
            {"request": request, "error": (
                f"Too many attempts. Wait about {max(1, wait // 60)} minute(s).")},
            status_code=429)
    if pin and verify_secret(pin.strip(), settings.kiosk_pin):
        request.session["pin_ok"] = True
        pin_guard.reset(key)
        if not looks_hashed(settings.kiosk_pin):
            try:
                settings.save({"kiosk_pin": pin.strip()})
            except Exception:
                pass
        return ingress_redirect(request, "/ui/")
    pin_guard.record(key)
    return templates.TemplateResponse(request, "pin.html",
        {"request": request, "error": "Incorrect PIN."}, status_code=401)


# First-boot readiness page (FoodAssistant-0m61). On a freshly flashed Pi the
# app serves minutes before its co-hosted inventory service does; this page
# shows honest progress in that gap and hands the browser to the setup wizard
# the moment the backend answers. services/readiness owns the rules: the page
# can only exist on an unconfigured pi_hosted install whose Grocy has never
# answered, and it never comes back once it has.
_first_run_kick_task = None


@router.get("/getting-ready", response_class=HTMLResponse)
async def getting_ready_page(request: Request):
    from ..services import readiness
    kiosk = request.query_params.get("kiosk") == "1"
    if not await readiness.gate_active():
        # Anything but the narrow first-boot window goes straight to setup.
        return ingress_redirect(request, "/setup?kiosk=1" if kiosk else "/setup")
    return templates.TemplateResponse(request, "getting-ready.html", {
        "request": request,
        "kiosk": kiosk,
    })


@router.get("/getting-ready/status")
async def getting_ready_status():
    """Poll target for the readiness page. Once the inventory service answers,
    kick the zero-touch provisioner immediately (instead of waiting for its
    next scheduled retry) so the wizard the page hands off to finds a
    connected inventory as soon as possible."""
    from ..services import readiness
    st = await readiness.status()
    if st["grocy_serving"] and not st["grocy_connected"] \
            and not settings.is_satellite():
        global _first_run_kick_task
        if _first_run_kick_task is None or _first_run_kick_task.done():
            import asyncio
            from ..services.first_run import startup_first_run
            _first_run_kick_task = asyncio.create_task(
                startup_first_run(attempts=10, delay=5.0, initial_delay=0))
    return st


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
        # Carry the query string across (FoodAssistant-r4kt). The appliance's
        # kiosk service always boots http://localhost/ui/?kiosk=1, and that
        # flag is what latches kiosk mode in the browser: the panel's touch
        # CSS and its safe-area insets both hang off it. Dropping the query
        # here meant the page that actually runs the script never saw
        # ?kiosk=1, so a panel whose browser storage had been cleared came up
        # as a plain desktop browser: content off the edges, and the install-
        # the-app bar offering to install the app onto the app. This only
        # started biting when the home page became a redirect (before the nav
        # work /ui rendered the dashboard in place and the query was moot).
        dest = "/" + target
        if request.url.query:
            dest += "?" + request.url.query
        return ingress_redirect(request, dest)
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
    })


@router.get("/pending", response_class=HTMLResponse)
async def pending_page(request: Request):
    return templates.TemplateResponse(request, "pending.html", {
        "request": request,
        "active": "pending",
    })


@router.get("/recipes", response_class=HTMLResponse)
async def recipes_page(request: Request):
    from ..services import recipe_source
    backend = recipe_source.active_backend()
    return templates.TemplateResponse(request, "recipes.html", {
        "request": request,
        "active": "recipes",
        "mealie_configured": settings.mealie_configured(),
        "mealie_url": settings.mealie_link_url(),
        # Native recipe store (FoodAssistant-zwwe): the page works with no
        # Mealie at all when the backend is native, and offers the one-click
        # Mealie migration while a Mealie is still connected.
        "recipes_backend": backend,
        "recipes_enabled": backend == "native" or settings.mealie_configured(),
        "library_name": "Mealie" if backend == "mealie" else "My Recipes",
        # Whether the Share-to-community action and the community source show
        # (FoodAssistant-l2hk): the install must be linked to a Forager account.
        "forager_linked": settings.cloud_linked(),
        "forager_recipes_active": settings.forager_recipes_active(),
        # Pre-fill "who to credit" in the share hub with the user-chosen device
        # name when one is set; auto-detected hostnames make poor credits, so
        # only the explicit setting is used and the field stays editable.
        "share_attribution_default": (settings.device_hostname or "").strip(),
    })


@router.get("/cook", response_class=HTMLResponse)
async def cook_page(request: Request):
    from ..services import recipe_source
    backend = recipe_source.active_backend()
    return templates.TemplateResponse(request, "cook.html", {
        "request": request,
        "active": "cook",
        # The recipe library is built in, so the page always works; the Mealie
        # link and "save to" labels follow the active backend.
        "recipes_backend": backend,
        "library_name": "Mealie" if backend == "mealie" else "My Recipes",
        "mealie_url": settings.mealie_link_url() if backend == "mealie" else None,
    })


@router.get("/current-recipe", response_class=HTMLResponse)
async def current_recipe_page(request: Request):
    from ..services import recipe_source
    backend = recipe_source.active_backend()
    return templates.TemplateResponse(request, "current-recipe.html", {
        "request": request,
        "active": "current_recipe",
        "recipes_backend": backend,
        "mealie_url": settings.mealie_link_url() if backend == "mealie" else None,
    })


@router.get("/recipes-in-progress")
async def recipes_in_progress_page(request: Request):
    """The In Progress view was merged into the 'On the Line' page (current
    recipe), which now shows every recipe in progress with a course selector.
    Kept as a redirect so old links/bookmarks still land somewhere (i8hz)."""
    return RedirectResponse(url="ui/current-recipe", status_code=307)


@router.get("/mealplan", response_class=HTMLResponse)
async def mealplan_page(request: Request):
    from ..services import recipe_source
    return templates.TemplateResponse(request, "mealplan.html", {
        "request": request,
        "active": "mealplan",
        # Where the plan lives: "native" (Pantry Raider's own table, the
        # default) or "mealie". Follows the recipe backend, since plan entries
        # reference the recipe library.
        "mealplan_backend": recipe_source.active_backend(),
        "mealie_url": settings.mealie_link_url(),
    })


@router.get("/shopping", response_class=HTMLResponse)
async def shopping_page(request: Request):
    from ..services import shopping_source
    return templates.TemplateResponse(request, "shopping.html", {
        "request": request,
        "active": "shopping",
        # Where the list lives: "grocy" (next to the inventory, the default)
        # or "mealie" (installs still running their recipes there).
        "shopping_backend": shopping_source.active_backend(),
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
async def timers_page(request: Request, view: str = ""):
    # Time & Temp is one page in three views (FoodAssistant-gg33): the header
    # sub-pill switches via ?view=timers|thermometers (default both). The active
    # nav key follows the view so the right sub-pill lights up.
    view = view if view in ("timers", "thermometers") else "both"
    active = {"timers": "tt_timers", "thermometers": "tt_thermo"}.get(view, "tt_both")
    return templates.TemplateResponse(request, "timers.html", {
        "request": request,
        "active": active,
        "initial_view": view,
    })


@router.get("/cubs", response_class=HTMLResponse)
async def cubs_page(request: Request):
    """The Bandit Cubs flasher page (docs/design/bandit-cub.md). Flashes a Cub
    over USB from the browser with ESP Web Tools (Web Serial), and always offers
    a per-profile firmware download plus a copyable esptool command so a browser
    without Web Serial, or a page opened over an insecure LAN address, still has
    a way through. The ESP Web Tools bundle is self-hosted under static/vendor,
    so the page makes no external requests at runtime."""
    from ..services import cub as cub_svc
    profiles = [
        {
            "id": pid,
            "label": meta["label"],
            "board": meta["board"],
            "chip_family": meta["chip_family"],
            "esptool_chip": meta["esptool_chip"],
            "asset": cub_svc.firmware_asset_name(pid, APP_VERSION),
            "esptool": cub_svc.esptool_command(pid, APP_VERSION),
        }
        for pid, meta in cub_svc.CUB_PROFILES.items()
    ]
    return templates.TemplateResponse(request, "cubs.html", {
        "request": request,
        "active": "cubs",
        "cub_profiles": profiles,
        "cub_version": APP_VERSION,
        # The escape hatch points at the same ESPHome project the prebuilt
        # images are built from, in the public repo.
        "esphome_repo_url": f"https://github.com/{GITHUB_REPO}/tree/main/esphome",
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
                from ..services import egress
                async with egress.guarded_async_client(allow_private=True, timeout=8.0) as c:
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
    from fastapi.responses import Response
    from ..services.cameras import ha_feed, is_blocked_fetch_host, BLOCKED_HOST_MESSAGE
    from ..services import egress
    entity = (entity or "").strip()
    snapshot_url = (snapshot_url or "").strip()
    if entity:
        url, headers = ha_feed({"ha_entity": entity}, "snapshot")
        if url:
            if is_blocked_fetch_host(url, fail_closed=False):
                return JSONResponse({"detail": BLOCKED_HOST_MESSAGE}, status_code=400)
            try:
                # allow_private: HA and LAN cameras live on the local network.
                # The guarded client pins the resolved address for the actual
                # connection, so a short-TTL name cannot pass the check above and
                # then connect to loopback (DNS rebinding, FoodAssistant-wrib).
                async with egress.guarded_async_client(allow_private=True, timeout=8.0) as c:
                    r = await c.get(url, headers=headers)
            except egress.BlockedHostError:
                return JSONResponse({"detail": BLOCKED_HOST_MESSAGE}, status_code=400)
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
        # (FoodAssistant-p1w5). Pinned connection re-checks the resolved address.
        try:
            async with egress.guarded_async_client(allow_private=True, timeout=8.0) as c:
                r = await c.get(snapshot_url)
        except egress.BlockedHostError:
            return JSONResponse({"detail": BLOCKED_HOST_MESSAGE}, status_code=400)
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
        # camera addresses stay allowed. The guarded client pins the resolved
        # address for the connection so it cannot rebind (FoodAssistant-wrib).
        from ..services import egress
        if is_blocked_fetch_host(url, fail_closed=False):
            return JSONResponse({"detail": BLOCKED_HOST_MESSAGE}, status_code=400)
        try:
            async with egress.guarded_async_client(allow_private=True, timeout=8.0) as c:
                r = await c.get(url, headers=headers)
        except egress.BlockedHostError:
            return JSONResponse({"detail": BLOCKED_HOST_MESSAGE}, status_code=400)
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
    from fastapi.responses import StreamingResponse, RedirectResponse
    from starlette.background import BackgroundTask
    from ..services.cameras import ha_feed, is_blocked_fetch_host, BLOCKED_HOST_MESSAGE
    from ..services import egress
    cams = settings.streamdeck_cameras or []
    if idx < 0 or idx >= len(cams):
        return JSONResponse({"detail": "Unknown camera."}, status_code=404)
    entry = cams[idx]
    url, headers = ha_feed(entry, "stream")
    if url:
        # Never relay a server-side stream from the app's own address or an
        # internal-only one (SSRF, FoodAssistant-e9al). The guarded client pins
        # the resolved address for the connection (FoodAssistant-wrib).
        if is_blocked_fetch_host(url, fail_closed=False):
            return JSONResponse({"detail": BLOCKED_HOST_MESSAGE}, status_code=400)
        client = egress.guarded_async_client(allow_private=True, timeout=None)
        try:
            req = client.build_request("GET", url, headers=headers)
            upstream = await client.send(req, stream=True)
        except egress.BlockedHostError:
            await client.aclose()
            return JSONResponse({"detail": BLOCKED_HOST_MESSAGE}, status_code=400)
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
    from ..navigation import glance_pages
    # Glance (the default) is a preset of the same layout engine Custom uses
    # (FoodAssistant-7598): the top-level nav pages seed the grid, sized to fit
    # them, and both modes render through the one .start-grid template path.
    # The glance flag only gates the notification-pills row now.
    glance = (getattr(settings, "start_page_mode", "glance") or "glance") != "custom"
    catalog = await sp.fetch_deck_catalog()
    if glance:
        layout = sp.glance_layout(glance_pages(), catalog=catalog)
        keys = len(layout)
    else:
        keys = sp.normalize_key_count(settings.start_page_keys)
        if settings.start_page_layout:
            layout = sp.resolve_layout(settings.start_page_layout, keys, catalog=catalog)
        else:
            # Custom with nothing arranged yet: start from the Glance preset
            # instead of an all-blank grid, so switching modes never lands on
            # an empty home (FoodAssistant-7598).
            layout = sp.glance_layout(glance_pages(), keys=keys, catalog=catalog)
    cols, rows = sp.GRID_SHAPES[keys]
    ctx = {
        "request": request,
        "enabled": settings.start_page_enabled,
        "glance": glance,
        "keys": keys, "cols": cols, "rows": rows, "layout": layout,
        # The live weather/forecast faces show the temperature unit symbol; use
        # the same saved units the deck and the weather page use.
        "weather_units": (settings.streamdeck_weather_units or "f").lower(),
    }
    return templates.TemplateResponse(request, "start.html", ctx)


@router.get("/start/glance-seed")
async def start_glance_seed():
    """The Glance arrangement as stored-layout tokens (plus its grid size), so
    the Settings Custom editor can start from the current home instead of a
    blank grid when there is no saved layout yet (FoodAssistant-7598)."""
    from ..services import start_page as sp
    from ..navigation import glance_pages
    catalog = await sp.fetch_deck_catalog()
    pages = glance_pages()
    keys = sp.glance_key_count(len(pages))
    return JSONResponse({"ok": True, "keys": keys,
                         "layout": sp.glance_seed_tokens(pages, keys, catalog=catalog)})


@router.post("/start/fire/{key_name}")
async def start_fire(key_name: str, long: bool = False):
    """Execute a deck-only Start Page key server-side (HA toggle, media, macro,
    or a built-in ha_1..ha_5 slot key). Always answers 200 with a toastable
    {ok, detail} so the Start Page can report the outcome inline."""
    from ..services import start_actions
    return JSONResponse(await start_actions.fire_key(key_name, long=long))


@router.get("/start/suggested")
async def start_suggested(request: Request, db: Session = Depends(get_db)):
    """Suggested Custom Buttons (FoodAssistant-1a8h): quick-add buttons the
    Start Page and Stream Deck editors can offer based on what the user
    actually does (groceries bought often, recipes cooked often). Shared by
    both editors, so a suggestion accepted or dismissed in one disappears from
    the other too. Never fails the page: Grocy/Mealie being down, or any
    lookup error, degrades to an empty list rather than a 500."""
    from ..services import cook_counts, suggested_actions as sa

    grocery_signals: list = []
    try:
        rows = await GrocyClient().get_stock_log(limit=500)
        grocery_signals = sa.rank_grocery_purchases(rows)
    except Exception:
        grocery_signals = []

    cook_signals: list = []
    try:
        cook_signals = sa.rank_cook_counts(cook_counts.top_counts(db, min_count=2))
    except Exception:
        cook_signals = []

    existing_shopping_items = {
        str(ov.get("item") or "").strip()
        for ov in (settings.streamdeck_key_overrides or [])
        if isinstance(ov, dict) and ov.get("type") == "shopping_add" and ov.get("item")
    }
    # The physical deck's own saved layout (built-in tokens like "cook") lives
    # in the controller's config.toml on a Pi, not in settings, so only the
    # Start Page layout is checked here; a "cook" button already on a physical
    # deck but not the Start Page is still offered once, harmlessly (accepting
    # it just adds a second shortcut to it).
    existing_layout_tokens = {str(tok) for tok in (settings.start_page_layout or []) if tok}

    suggestions = sa.build_suggestions(
        grocery_signals, cook_signals,
        existing_shopping_items=existing_shopping_items,
        existing_layout_tokens=existing_layout_tokens,
        dismissed_ids=settings.start_page_suggestions_dismissed or [],
    )
    return JSONResponse({"ok": True, "suggestions": suggestions})


@router.post("/start/suggested/dismiss")
async def start_suggested_dismiss(payload: dict = None):
    """Hide a suggestion by id. Persisted in settings so it stays dismissed
    across restarts and shows the same in both editors (FoodAssistant-1a8h)."""
    sid = str((payload or {}).get("id") or "").strip()
    if not sid:
        return JSONResponse({"ok": False, "detail": "Missing suggestion id."}, status_code=400)
    current = list(settings.start_page_suggestions_dismissed or [])
    if sid not in current:
        current.append(sid)
        settings.save({"start_page_suggestions_dismissed": current})
    return JSONResponse({"ok": True, "dismissed": current})


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
    cur = forecast.get("current") or {}
    # A light, food-themed line under the current conditions. It rotates by
    # day-of-year, so the same forecast reads a little differently tomorrow.
    forecast["note"] = weather_svc.forecast_insight(cur)
    return {"ok": True, "forecast": forecast, "location": loc}


@router.get("/screensaver/photos")
async def screensaver_photos():
    """Photo list for the screensaver slideshow (FoodAssistant-5w4m, af1l).

    Returns {ok, photos, urls}: `urls` is the list of ready-to-use image src
    strings the saver plays, from the configured photo source (a folder, an
    Immich album, or direct links); `photos` keeps the legacy bare-name shape
    for the built-in USB source. With the built-in source on a Pi appliance
    the attached flash drive is only visible to the host, so names come from
    the bridge (GET /usb/photos). Never raises; any failure is an empty list
    and the screensaver falls back to the bouncing logo."""
    from ..services import photo_source as ps
    photos: list = []
    source = ps.normalize_photo_source(settings.photo_source)
    if source != "built-in":
        urls = await ps.list_photos(settings)
    else:
        urls = []
        if settings.is_pi_appliance():
            from ..services.usb_backup import _bridge_get
            data = await _bridge_get("/usb/photos")
            got = data.get("photos") if isinstance(data, dict) else None
            if isinstance(got, list):
                photos = [n for n in got if isinstance(n, str)]
                urls = ["ui/screensaver/photo?name=" + _quote(n) for n in photos]
    # A short cache keeps repeated saver starts cheap without hiding a newly
    # plugged-in drive or freshly dropped photo for long.
    return JSONResponse({"ok": True, "photos": photos, "urls": urls},
                        headers={"Cache-Control": "private, max-age=60"})


@router.get("/screensaver/photo/local")
async def screensaver_photo_local(name: str = ""):
    """Serve one image from the configured photos folder (FoodAssistant-af1l).

    Traversal-guarded: only a bare image file name directly inside the
    folder is served (safe_photo_path rejects separators, dot-dot, hidden
    files, and non-image extensions), so the route can never read outside
    the photos folder."""
    from fastapi.responses import FileResponse
    from ..services import photo_source as ps
    if ps.normalize_photo_source(settings.photo_source) != "folder":
        return JSONResponse({"detail": "Photo not found."}, status_code=404)
    target = ps.safe_photo_path(ps.effective_photo_folder(settings), name)
    if target is None:
        return JSONResponse({"detail": "Photo not found."}, status_code=404)
    return FileResponse(target,
                        headers={"Cache-Control": "private, max-age=3600"})


@router.get("/screensaver/photo/immich")
async def screensaver_photo_immich(id: str = ""):
    """Proxy one Immich asset preview for the slideshow (FoodAssistant-af1l).

    The app fetches the preview-size thumbnail with the stored API key, so
    the key never reaches the browser and the kiosk never pulls full-size
    originals over the LAN. Cached an hour like the USB photo proxy."""
    from ..services import photo_source as ps
    base = (settings.immich_base_url or "").strip().rstrip("/")
    key = settings.immich_api_key or ""
    if (ps.normalize_photo_source(settings.photo_source) != "immich"
            or not base or not key
            or not ps._IMMICH_ID_RE.match(id or "")):
        return JSONResponse({"detail": "Photo not found."}, status_code=404)
    import httpx
    from fastapi.responses import Response
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(f"{base}/api/assets/{_quote(id)}/thumbnail",
                            params={"size": "preview"},
                            headers=ps.immich_headers(key))
    except Exception as e:
        return JSONResponse({"detail": f"Photo unavailable: {e.__class__.__name__}"},
                            status_code=502)
    if r.status_code != 200:
        return JSONResponse({"detail": "Photo not found."}, status_code=404)
    return Response(content=r.content,
                    media_type=r.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "private, max-age=3600"})


class PhotoSourceTest(BaseModel):
    """Candidate photo-source values from the settings form (unsaved)."""
    photo_source: str = ""
    photo_folder: str = ""
    immich_base_url: str = ""
    immich_api_key: str = ""   # blank = use the stored key
    immich_album_id: str = ""
    photo_urls: str = ""


@router.post("/screensaver/photos/test")
async def screensaver_photos_test(payload: PhotoSourceTest):
    """Try the photo-source values currently in the form (FoodAssistant-af1l).

    Counts what the source would play without saving anything, so a wrong
    folder path or Immich key shows up before it ever reaches the kiosk.
    Returns {ok, count, sample}: `sample` is a src usable as a thumbnail
    when one can work pre-save (a direct link always can; folder and Immich
    previews go through routes that read SAVED settings, so the sample is
    only returned when the tested values match the stored ones)."""
    from pathlib import Path
    from ..services import photo_source as ps
    source = ps.normalize_photo_source(payload.photo_source)
    count, sample, detail = 0, "", ""
    if source == "urls":
        urls = ps.parse_photo_urls(payload.photo_urls)
        count = len(urls)
        sample = urls[0] if urls else ""
        if not count:
            detail = "No usable links found. One direct http(s) image link per line."
    elif source == "folder":
        folder = Path(payload.photo_folder.strip()) if payload.photo_folder.strip() \
            else Path(settings.data_dir) / "photos"
        names = ps.list_folder_photos(folder)
        count = len(names)
        if not folder.is_dir():
            detail = f"Folder does not exist yet: {folder}"
        elif not count:
            detail = f"No images found in {folder}."
        elif (ps.normalize_photo_source(settings.photo_source) == "folder"
                and folder == ps.effective_photo_folder(settings)):
            sample = ps.folder_photo_src(names[0])
    elif source == "immich":
        key = payload.immich_api_key or settings.immich_api_key or ""
        ids = await ps.immich_album_asset_ids(
            payload.immich_base_url, key, payload.immich_album_id)
        ps.invalidate_immich_cache()   # a test must never serve stale results
        count = len(ids)
        if not count:
            detail = ("No photos found. Check the server URL, the API key, "
                      "and the album id (the last part of the album's URL).")
        elif (ps.normalize_photo_source(settings.photo_source) == "immich"
                and not payload.immich_api_key
                and payload.immich_base_url.strip().rstrip("/")
                == (settings.immich_base_url or "").strip().rstrip("/")):
            sample = ps.immich_photo_src(ids[0])
    else:
        detail = "The built-in source plays the USB drive on a Pi appliance; nothing to test here."
    return {"ok": count > 0, "source": source, "count": count,
            "sample": sample, "detail": detail}


@router.get("/screensaver/photo")
async def screensaver_photo(name: str = ""):
    """Proxy one slideshow image from the bridge (GET /usb/photo).

    The bridge does the path-safety and size checks; this just relays the
    bytes. Cached for an hour so the kiosk browser does not refetch the same
    photo on every slideshow cycle."""
    if not settings.is_pi_appliance() or not name:
        return JSONResponse({"detail": "Photo not found."}, status_code=404)
    from fastapi.responses import Response
    from ..services.usb_backup import _BRIDGE
    from ..services.bridge import bridge_client
    try:
        # Token-carrying client so the photo proxy survives token enforcement
        # (FoodAssistant-ow4f).
        async with bridge_client(timeout=20.0) as c:
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
