"""Zero-touch first-run provisioning for Grocy and Mealie (FoodAssistant-syxf).

A fresh install should never require the user to log in to Grocy, change its
stock password, or copy API keys around: when Grocy is reachable and still on
its factory sign-in, Pantry Raider signs in itself, creates its own API key,
secures the admin account with a generated password, and saves everything
into settings. Grocy is the required backend, so this path is the heart of
the zero-touch first run.

Mealie gets the same treatment, but strictly as the OPTIONAL connector it
is: provisioning only ever acts on a Mealie that is already up and answering
with its factory sign-in, and nothing here (or in the triggers that call
here) ever suggests installing one. The generated passwords are stored (as
secrets) so the user can still sign in to a backend directly if they ever
want to.

Hands-off rules, in order, per backend:
  1. Already connected (an API key/token is saved in settings): do nothing.
  2. Sign-in is no longer the factory default: the install is someone's, so
     do nothing (never probe repeatedly either; Mealie locks accounts after
     several failed logins).
  3. Unreachable: report a retryable failure so callers can back off and try
     again (a first boot can take minutes to start serving).

Exact API sequences (verified against the pinned images):
  Grocy 4.6.0 (lscr.io/linuxserver/grocy:4.6.0):
    POST /login (form: username, password, stay_logged_in) -> 302 to "/" on
    success ("/login?invalid=true" on failure), sets the grocy_session
    cookie. GET /manageapikeys/new?description=... creates a key and 302s to
    /manageapikeys?key=<id>; the key value is read from that page's HTML
    (data-apikey-id / data-apikey-key attributes). The key is verified via
    GET /api/system/info, then the stock admin password is replaced through
    PUT /api/users/<id> (username/first_name/last_name/password body).
  Mealie v3.19.2 (ghcr.io/mealie-recipes/mealie:v3.19.2):
    POST /api/auth/token (OAuth2 password form) with the factory
    changeme@example.com / MyPassword -> bearer token. POST
    /api/users/api-tokens {name, integrationId} -> five-year API token.
    PUT /api/users/password {currentPassword, newPassword} replaces the
    factory password. GET/POST /api/households/shopping/lists (with the
    /api/groups/... fallback for Mealie 1.x) ensures a default list exists.

Every step is idempotent and the whole run is safe to repeat: a second run
finds the backend configured and skips.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from urllib.parse import urlparse, parse_qs

import httpx

from ..config import settings

logger = logging.getLogger("foodassistant.first_run")

# Factory sign-ins the provisioner is allowed to use. Anything else means the
# install belongs to someone and provisioning keeps its hands off.
GROCY_DEFAULT_USERNAME = "admin"
GROCY_DEFAULT_PASSWORD = "admin"
MEALIE_DEFAULT_EMAIL = "changeme@example.com"
MEALIE_DEFAULT_PASSWORD = "MyPassword"

# What the created credentials are called inside each backend, so a user who
# does open Grocy or Mealie can tell where the key came from.
CREDENTIAL_LABEL = "Pantry Raider"

# The friendly default shopping list created in Mealie when none exists yet.
SHOPPING_LIST_NAME = "Groceries"

_TIMEOUT = 10.0


def generate_password() -> str:
    """A strong random password for the backend's admin account.

    URL-safe base64 of 96 random bits: long enough to be unguessable, short
    enough to type from the settings pane if the user ever signs in by hand.
    """
    return secrets.token_urlsafe(12)


# ── Report plumbing ──────────────────────────────────────────────────────────
# Each provisioning run returns {ok, configured, retryable, message, steps}.
# "steps" is a list of {step, done, skipped, reason} entries so the caller (and
# the tests) can see exactly what happened; "message" is the user-forward
# one-liner the settings pane shows.

def _step(name: str, *, done: bool = False, skipped: bool = False,
          reason: str = "") -> dict:
    return {"step": name, "done": done, "skipped": skipped, "reason": reason}


def _report(*, ok: bool, configured: bool, message: str, steps: list[dict],
            retryable: bool = False) -> dict:
    return {"ok": ok, "configured": configured, "retryable": retryable,
            "message": message, "steps": steps}


# ── Grocy HTML/redirect parsing (pure, unit-tested) ─────────────────────────

def grocy_login_succeeded(status_code: int, location: str) -> bool | None:
    """Interpret Grocy's answer to the login form POST.

    Grocy always answers the form with a redirect: to "/" on success and to
    "/login?invalid=true" on a wrong password. Returns True/False for those,
    and None for anything else (a proxy error page, a still-booting app), so
    the caller can treat it as "not answering properly yet" rather than as a
    changed password.
    """
    if status_code not in (301, 302, 303, 307, 308):
        return None
    if "invalid" in (location or "").lower():
        return False
    return True


def grocy_new_key_id(location: str) -> str:
    """The id of the just-created API key from the /manageapikeys/new redirect.

    Grocy redirects to /manageapikeys?key=<id>. Returns "" when the id cannot
    be read (the caller then falls back to matching by description).
    """
    try:
        qs = parse_qs(urlparse(location or "").query)
        return (qs.get("key") or [""])[0]
    except (ValueError, TypeError):
        return ""


def parse_grocy_api_key(html: str, key_id: str = "",
                        description: str = CREDENTIAL_LABEL) -> str:
    """Extract the created API key's value from the /manageapikeys page.

    The page's delete button carries data-apikey-id, data-apikey-key, and
    data-apikey-description attributes in that order (Grocy 4.6.0,
    views/manageapikeys.blade.php). Prefers the row matching the id from the
    creation redirect; falls back to the newest row carrying our description.
    Grocy keys are 50-char alphanumeric strings.
    """
    html = html or ""
    if key_id:
        m = re.search(
            r'data-apikey-id="' + re.escape(key_id)
            + r'"\s+data-apikey-key="([0-9A-Za-z]{20,80})"', html)
        if m:
            return m.group(1)
    if description:
        matches = re.findall(
            r'data-apikey-key="([0-9A-Za-z]{20,80})"\s+'
            r'data-apikey-description="' + re.escape(description) + '"', html)
        if matches:
            return matches[-1]
    return ""


# ── Grocy provisioning ───────────────────────────────────────────────────────

async def provision_grocy(base_url: str = "") -> dict:
    """Connect this install to Grocy with no manual steps.

    Signs in with the factory admin/admin, creates a Pantry Raider API key,
    verifies it, saves it to settings, and then replaces the factory admin
    password with a generated one (stored as a secret so the user can still
    sign in to Grocy directly). Idempotent and hands-off: see the module
    docstring for the skip rules.
    """
    steps: list[dict] = []
    if settings.grocy_api_key:
        steps.append(_step("connect", skipped=True,
                           reason="An API key is already saved."))
        return _report(ok=True, configured=True, steps=steps,
                       message="Grocy is already connected; nothing to do.")
    base = (base_url or settings.grocy_base_url or "").rstrip("/")
    if not base:
        steps.append(_step("connect", reason="No Grocy address to try."))
        return _report(ok=False, configured=False, steps=steps,
                       message="Enter the Grocy address first.")

    async with httpx.AsyncClient(timeout=_TIMEOUT,
                                 follow_redirects=False) as client:
        # 1. Sign in with the factory login.
        try:
            r = await client.post(f"{base}/login", data={
                "username": GROCY_DEFAULT_USERNAME,
                "password": GROCY_DEFAULT_PASSWORD,
                "stay_logged_in": "",
            })
        except httpx.HTTPError:
            steps.append(_step("sign-in", reason="Grocy is not reachable."))
            return _report(ok=False, configured=False, retryable=True, steps=steps,
                           message="Grocy is not answering yet. It will be "
                                   "set up automatically once it is running, "
                                   "or try again in a moment.")
        outcome = grocy_login_succeeded(r.status_code, r.headers.get("location", ""))
        if outcome is None:
            steps.append(_step("sign-in",
                               reason=f"Unexpected answer (HTTP {r.status_code})."))
            return _report(ok=False, configured=False, retryable=True, steps=steps,
                           message="Grocy is not answering properly yet; "
                                   "try again in a moment.")
        if outcome is False:
            steps.append(_step("sign-in", skipped=True,
                               reason="The admin password has been changed."))
            return _report(ok=True, configured=False, steps=steps,
                           message="This Grocy already has its own sign-in, so "
                                   "it was left untouched. Paste an API key "
                                   "from Grocy to connect it.")
        steps.append(_step("sign-in", done=True))

        # 2. Create the API key through the same page the Grocy UI uses.
        try:
            r = await client.get(f"{base}/manageapikeys/new",
                                 params={"description": CREDENTIAL_LABEL})
            key_id = grocy_new_key_id(r.headers.get("location", ""))
            page = await client.get(f"{base}/manageapikeys")
        except httpx.HTTPError:
            steps.append(_step("create API key",
                               reason="Grocy stopped answering."))
            return _report(ok=False, configured=False, retryable=True, steps=steps,
                           message="Grocy stopped answering while setting up; "
                                   "try again in a moment.")
        api_key = parse_grocy_api_key(page.text, key_id)
        if not api_key:
            steps.append(_step("create API key",
                               reason="Could not read the new key back."))
            return _report(ok=False, configured=False, steps=steps,
                           message="Grocy did not hand back the new API key. "
                                   "Create one in Grocy under Manage API keys "
                                   "and paste it here.")
        steps.append(_step("create API key", done=True))

        # 3. Verify the key actually authenticates before trusting it.
        try:
            r = await client.get(f"{base}/api/system/info",
                                 headers={"GROCY-API-KEY": api_key})
            verified = r.status_code == 200
        except httpx.HTTPError:
            verified = False
        if not verified:
            steps.append(_step("verify", reason="The new key did not authenticate."))
            return _report(ok=False, configured=False, retryable=True, steps=steps,
                           message="The new Grocy key did not work; "
                                   "try again in a moment.")
        steps.append(_step("verify", done=True))

        # 4. Save the working key first, so even a failure below leaves the
        # install connected.
        settings.save({"grocy_api_key": api_key, "grocy_base_url": base})
        steps.append(_step("save settings", done=True))

        # 5. Secure the factory admin account with a generated password.
        # Best-effort: the key above is what matters, and the settings pane
        # says plainly when the stock sign-in is still in place.
        password = generate_password()
        secured = False
        try:
            r = await client.get(f"{base}/api/users",
                                 headers={"GROCY-API-KEY": api_key})
            users = r.json() if r.status_code == 200 else []
            admin = next((u for u in users
                          if u.get("username") == GROCY_DEFAULT_USERNAME), None)
            if admin:
                r = await client.put(
                    f"{base}/api/users/{admin['id']}",
                    headers={"GROCY-API-KEY": api_key},
                    json={
                        "username": GROCY_DEFAULT_USERNAME,
                        "first_name": admin.get("first_name") or "",
                        "last_name": admin.get("last_name") or "",
                        "password": password,
                        "picture_file_name": admin.get("picture_file_name"),
                    })
                secured = r.status_code < 400
        except (httpx.HTTPError, ValueError):
            secured = False
        if secured:
            settings.save({"grocy_admin_password": password})
            steps.append(_step("secure admin sign-in", done=True))
        else:
            steps.append(_step("secure admin sign-in", skipped=True,
                               reason="Password change did not apply; the "
                                      "sign-in is still admin / admin."))

    message = "Grocy is connected. Pantry Raider created its own API key"
    message += (" and secured the admin sign-in (find it under this pane)."
                if secured else
                "; the admin sign-in is still admin / admin, so change it in "
                "Grocy if others can reach it.")
    return _report(ok=True, configured=True, steps=steps, message=message)


# ── Mealie provisioning ──────────────────────────────────────────────────────

async def _mealie_scoped(client: httpx.AsyncClient, base: str, method: str,
                         path: str, headers: dict, **kwargs) -> httpx.Response:
    """A group-scoped Mealie request: /api/households first, /api/groups on 404.

    The same v2-vs-v1 detection services.mealie does, kept local so this
    module works before MealieClient has credentials to exist with.
    """
    r = await client.request(method, f"{base}/api/households{path}",
                             headers=headers, **kwargs)
    if r.status_code == 404:
        r = await client.request(method, f"{base}/api/groups{path}",
                                 headers=headers, **kwargs)
    return r


async def ensure_mealie_shopping_list(client: httpx.AsyncClient, base: str,
                                      token: str) -> dict:
    """Make sure the connected Mealie has a shopping list, creating one named
    SHOPPING_LIST_NAME when none exists at all.

    Deliberately isolated from the rest of provisioning: the default shopping
    list now lives in Grocy, so this only matters for the installs whose list
    stays in Mealie (services/shopping_source.py), and it is the first Mealie
    step to delete once those are gone. Never creates a second list. Returns
    the step entry for the report.
    """
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = await _mealie_scoped(client, base, "GET", "/shopping/lists",
                                 headers, params={"perPage": 1})
        existing = (r.json() or {}).get("items", []) if r.status_code == 200 else None
        if existing:
            return _step("default shopping list", skipped=True,
                         reason="A shopping list already exists.")
        if existing is None:
            return _step("default shopping list",
                         reason="Could not list shopping lists.")
        r = await _mealie_scoped(client, base, "POST", "/shopping/lists",
                                 headers, json={"name": SHOPPING_LIST_NAME})
        return _step("default shopping list", done=r.status_code < 400,
                     reason="" if r.status_code < 400
                            else f"Mealie answered HTTP {r.status_code}.")
    except (httpx.HTTPError, ValueError):
        return _step("default shopping list",
                     reason="Could not reach the shopping lists.")


async def provision_mealie(base_url: str = "") -> dict:
    """Connect this install to Mealie with no manual steps.

    Signs in with the factory changeme@example.com / MyPassword, creates a
    long-lived API token, saves it to settings, replaces the factory password
    with a generated one (stored as a secret so the user can still sign in
    to Mealie directly), and makes sure a default shopping list exists.
    Idempotent and hands-off: see the module docstring for the skip rules.
    A wrong-password answer stops the run immediately and is never retried,
    so provisioning can never trip Mealie's failed-login lockout.
    """
    steps: list[dict] = []
    if settings.mealie_api_key and settings.mealie_base_url:
        steps.append(_step("connect", skipped=True,
                           reason="An API token is already saved."))
        return _report(ok=True, configured=True, steps=steps,
                       message="Mealie is already connected; nothing to do.")
    base = (base_url or settings.mealie_base_url or "").rstrip("/")
    if not base:
        steps.append(_step("connect", reason="No Mealie address to try."))
        return _report(ok=False, configured=False, steps=steps,
                       message="Enter the Mealie address first.")

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        # 1. Sign in with the factory login (OAuth2 password form).
        try:
            r = await client.post(f"{base}/api/auth/token", data={
                "username": MEALIE_DEFAULT_EMAIL,
                "password": MEALIE_DEFAULT_PASSWORD,
            })
        except httpx.HTTPError:
            steps.append(_step("sign-in", reason="Mealie is not reachable."))
            return _report(ok=False, configured=False, retryable=True, steps=steps,
                           message="Mealie is not answering yet. It will be "
                                   "set up automatically once it is running, "
                                   "or try again in a moment.")
        if r.status_code in (401, 403, 423):
            steps.append(_step("sign-in", skipped=True,
                               reason="The factory sign-in no longer works."))
            return _report(ok=True, configured=False, steps=steps,
                           message="This Mealie already has its own sign-in, "
                                   "so it was left untouched. Paste an API "
                                   "token from Mealie to connect it.")
        if r.status_code != 200:
            steps.append(_step("sign-in",
                               reason=f"Unexpected answer (HTTP {r.status_code})."))
            return _report(ok=False, configured=False, retryable=True, steps=steps,
                           message="Mealie is still starting up; "
                                   "try again in a moment.")
        try:
            bearer = {"Authorization": f"Bearer {r.json()['access_token']}"}
        except (ValueError, KeyError):
            steps.append(_step("sign-in", reason="No access token in the reply."))
            return _report(ok=False, configured=False, retryable=True, steps=steps,
                           message="Mealie answered oddly; try again in a moment.")
        steps.append(_step("sign-in", done=True))

        # 2. Create the long-lived API token first: it is the credential the
        # app runs on, so it lands before anything best-effort.
        try:
            r = await client.post(f"{base}/api/users/api-tokens",
                                  headers=bearer,
                                  json={"name": CREDENTIAL_LABEL,
                                        "integrationId": "generic"})
            token = (r.json() or {}).get("token", "") if r.status_code < 400 else ""
        except (httpx.HTTPError, ValueError):
            token = ""
        if not token:
            steps.append(_step("create API token",
                               reason="Mealie did not hand back a token."))
            return _report(ok=False, configured=False, retryable=True, steps=steps,
                           message="Mealie did not hand back an API token; "
                                   "try again in a moment.")
        steps.append(_step("create API token", done=True))

        # 3. Save the working token so even a failure below leaves the
        # install connected, then reset the Mealie client caches.
        settings.save({"mealie_base_url": base, "mealie_api_key": token})
        from .mealie import reset_cache
        reset_cache()
        steps.append(_step("save settings", done=True))

        # 4. Secure the factory account with a generated password.
        # Best-effort, like Grocy above.
        password = generate_password()
        secured = False
        try:
            r = await client.put(f"{base}/api/users/password",
                                 headers=bearer,
                                 json={"currentPassword": MEALIE_DEFAULT_PASSWORD,
                                       "newPassword": password})
            secured = r.status_code < 400
        except httpx.HTTPError:
            secured = False
        if secured:
            settings.save({"mealie_admin_password": password})
            steps.append(_step("secure sign-in", done=True))
        else:
            steps.append(_step("secure sign-in", skipped=True,
                               reason="Password change did not apply; the "
                                      "sign-in is still the factory default."))

        # 5. Make sure a shopping list exists so a Mealie-backed Shopping page
        # works on day one. Kept as its own function so it is one line to
        # delete when the Mealie shopping window closes.
        steps.append(await ensure_mealie_shopping_list(client, base, token))

    message = "Mealie is connected. Pantry Raider created its own API token"
    message += (" and secured the sign-in (find it under this pane)."
                if secured else
                "; the sign-in is still the factory default, so change it in "
                "Mealie if others can reach it.")
    return _report(ok=True, configured=True, steps=steps, message=message)


# ── Background triggers ──────────────────────────────────────────────────────

async def provision_mealie_when_up(base_url: str, attempts: int = 120,
                                   delay: float = 5.0) -> dict:
    """Provision Mealie once it starts answering, for the on-device start.

    The first Mealie start downloads the image and runs migrations, which can
    take minutes; this polls patiently and only keeps retrying while the
    failure is the retryable kind (unreachable / still booting). A hands-off
    or already-configured answer stops immediately, so this can never hammer
    a configured install or trip the login lockout.
    """
    report: dict = {}
    for attempt in range(max(1, attempts)):
        report = await provision_mealie(base_url)
        if report.get("ok") or not report.get("retryable"):
            break
        await asyncio.sleep(delay)
    if report.get("configured"):
        logger.info("Mealie set itself up after its on-device start")
    return report


def _grocy_candidates() -> list[str]:
    """Addresses worth probing for a co-hosted Grocy on first run.

    The compose stack reaches it at the service hostname (the configured
    default); a Pi appliance also gets loopback on the published port, since
    the appliance app cannot resolve compose service names. A candidate that
    does not resolve simply reports unreachable and is skipped.
    """
    candidates = [settings.grocy_base_url] if settings.grocy_base_url else []
    try:
        from ..hardware import is_raspberry_pi
        if is_raspberry_pi() and "http://localhost:9383" not in candidates:
            candidates.append("http://localhost:9383")
    except Exception:
        pass
    return candidates


def _mealie_candidates() -> list[str]:
    """Addresses a Mealie the user ALREADY runs alongside might answer on.

    Mealie is optional: these are only probed, never started or installed,
    and a candidate that does not resolve simply reports unreachable and is
    skipped. The compose stack reaches a with-mealie container at the service
    hostname; a Pi appliance (host networking) at loopback on the published
    port.
    """
    if settings.mealie_base_url:
        return [settings.mealie_base_url]
    candidates = ["http://mealie:9000"]
    try:
        from ..hardware import is_raspberry_pi
        if is_raspberry_pi():
            candidates.append("http://localhost:9285")
    except Exception:
        pass
    return candidates


async def startup_first_run(attempts: int = 90, delay: float = 20.0,
                            initial_delay: float = 5.0) -> None:
    """The app-startup trigger: quietly connect fresh co-hosted backends.

    Runs as a background task so startup never waits on it. It only acts on a
    backend whose setting is empty AND that still answers to the factory
    sign-in; a configured install (key saved, or password changed) is never
    touched. Retries while a backend looks like it is still booting (briskly
    for the first couple of minutes, then at the slower cadence, for about
    half an hour in total by default: a Pi 4 first boot pulls images and runs
    Grocy's own first start, which together can pass the ten-minute mark),
    then gives up silently: the settings pane's "Set up for me" button
    remains for later, and a wizard save re-kicks a fresh run.
    """
    if settings.is_satellite():
        return
    need_grocy = not settings.grocy_api_key
    need_mealie = not (settings.mealie_api_key and settings.mealie_base_url)
    if not (need_grocy or need_mealie):
        return
    # Give co-starting backends a moment before the first probe.
    if initial_delay:
        await asyncio.sleep(initial_delay)
    for attempt in range(max(1, attempts)):
        if not (need_grocy or need_mealie):
            return
        if attempt:
            # Brisk early polling so a backend that comes up quickly is
            # connected within seconds, not at the next long tick.
            await asyncio.sleep(min(delay, 10.0) if attempt <= 12 else delay)
        if need_grocy:
            # Keep retrying only while every candidate address looks like a
            # backend still booting; a definite answer (connected, or
            # hands-off) from any candidate settles Grocy for good.
            for base in _grocy_candidates():
                try:
                    report = await provision_grocy(base)
                except Exception:
                    logger.exception("Grocy first-run provisioning failed")
                    report = {"retryable": False}
                if report.get("configured"):
                    logger.info("Grocy set itself up on first run")
                if not report.get("retryable"):
                    need_grocy = False
                    break
        if need_mealie:
            # Same retry rule as Grocy above. Mealie is optional: a Mealie
            # that is not running simply reports unreachable each attempt and
            # the loop eventually gives up without ever suggesting one.
            for base in _mealie_candidates():
                try:
                    report = await provision_mealie(base)
                except Exception:
                    logger.exception("Mealie first-run provisioning failed")
                    report = {"retryable": False}
                if report.get("configured"):
                    logger.info("Mealie set itself up on first run")
                if not report.get("retryable"):
                    need_mealie = False
                    break
