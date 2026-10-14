"""Forager pairing, sign-in, status, and cloud backup and restore."""

import re
import secrets
import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel

from ...config import settings, APP_VERSION, device_hostname
from ...dependencies import reset_providers
from ...ingress import ingress_redirect
from ._common import router, _safe_error


# ---- Forager pairing (docs/design/cloud-platform.md) ----------
# The install always dials out: a pairing code minted on the cloud portal is
# typed here, redeemed for a long-lived instance token, and stored. All three
# routes degrade honestly when the cloud is unreachable; none of them can
# break the settings page.

_CLOUD_TIMEOUT = httpx.Timeout(8.0, connect=5.0)
# Backups can be large and cross a real LLM-free but still slow upload path, so
# the push/pull gets a generous read timeout while the connect stays tight.
_CLOUD_BACKUP_TIMEOUT = httpx.Timeout(120.0, connect=6.0)


def _cloud_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.cloud_instance_token}",
        "X-Device-Version": APP_VERSION,
        "X-Device-Mode": settings.deployment_mode or "server",
    }


def _cloud_client() -> httpx.AsyncClient:
    """A client for an ordinary Forager call, with the standard cloud timeout.

    Backup upload and restore move whole archives and keep their own longer
    _CLOUD_BACKUP_TIMEOUT client."""
    return httpx.AsyncClient(timeout=_CLOUD_TIMEOUT)


class CloudLinkPayload(BaseModel):
    code: str = ""


class CloudSigninPayload(BaseModel):
    email: str = ""
    password: str = ""
    device_name: str = ""
    # A code from the authenticator app (or a recovery code). Sent only when
    # the account has two-factor sign-in on and the cloud asked for it.
    totp: str = ""


def _apply_cloud_link(body: dict, session=None) -> dict:
    """Finish a fresh Forager link the same way for every sign-in path
    (password, Google, pairing code from the website).

    Stores the credential, and if no other AI provider is set up yet makes
    Forager the scanning and enrichment provider so scanning just works; an
    install with a working provider keeps it (the settings card offers a
    one-click switch instead). When the platform already knows this kitchen's
    public web address it is applied to qr_public_url so the local and
    outside addresses match; null or absent (no tunnel yet) leaves the
    stored value untouched.

    Signing in on the wizard's AI step happens before there is any login on this
    device, so a marker goes into that browser's session and alongside the
    credential: the save that finishes setup keeps the account connection only
    when it comes from the same browser (see save_setup).
    """
    to_save: dict = {"cloud_instance_token": body.get("instance_token", "")}
    if session is not None and not settings.is_configured():
        marker = secrets.token_hex(16)
        session["cloud_link_nonce"] = marker
        to_save["cloud_link_session"] = marker
    providers_set = False
    if not settings.ai_configured():
        to_save["vision_provider"] = "cloud"
        to_save["enrich_provider"] = "cloud"
        providers_set = True
    public_url = (body.get("suggested_public_url") or "").strip().rstrip("/")
    if public_url:
        to_save["qr_public_url"] = public_url
    settings.save(to_save)
    reset_providers()
    return {"providers_set": providers_set, "public_url": public_url}


@router.post("/cloud/signin")
async def cloud_signin(request: Request, payload: CloudSigninPayload):
    """Sign in with a Forager account and provision this install.

    One POST to the cloud's provision endpoint trades the account email and
    password for this install's own long-lived credential. The password is
    forwarded to the cloud and nowhere else: it is never saved, never logged
    (nothing in this app logs request bodies), and the error paths below
    scrub it so it cannot leak into a message.

    On success _apply_cloud_link finishes the link: providers when unset,
    the kitchen's public web address when the platform provides one.

    When the account has two-factor sign-in on, the cloud accepts the password
    but answers 401 with a machine-readable error: "totp_required" the first
    time (no code sent) and "totp_invalid" for a wrong code. Those come back
    with a totp_prompt flag so the page can ask for the code and resubmit.
    """
    email = payload.email.strip()
    password = payload.password
    totp = payload.totp.strip()
    if not email or not password:
        return {"ok": False, "error": "Enter your Forager email and password."}
    name = payload.device_name.strip() or device_hostname() or "Pantry Raider"
    base = settings.cloud_base_url.rstrip("/")
    body_out = {"email": email, "password": password, "device_name": name}
    if totp:
        body_out["totp"] = totp
    try:
        async with _cloud_client() as client:
            r = await client.post(f"{base}/v1/instances/provision",
                                  json=body_out)
    except httpx.HTTPError as e:
        return {"ok": False, "error": (
            f"Forager could not be reached ({e.__class__.__name__}). "
            "Check the internet connection and try again.")}
    if r.status_code == 401:
        # A 2FA account answers with {"error": "totp_required"|"totp_invalid"};
        # a genuine bad password answers with {"detail": ...}. Tell them apart
        # so the page prompts for a code instead of blaming the password.
        try:
            gate = r.json().get("error", "")
        except ValueError:
            gate = ""
        if gate == "totp_required":
            return {"ok": False, "totp_prompt": True,
                    "error": ("Enter the code from your authenticator app to "
                              "finish signing in.")}
        if gate == "totp_invalid":
            return {"ok": False, "totp_prompt": True,
                    "error": ("That code did not match. Enter the current code "
                              "from your authenticator app (or a recovery "
                              "code) and try again.")}
        return {"ok": False, "error": ("That email and password did not match "
                                       "a Forager account. Check them and try "
                                       "again.")}
    if r.status_code == 429:
        return {"ok": False, "error": ("Too many sign-in attempts right now. "
                                       "Wait a minute and try again.")}
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", "")
        except ValueError:
            detail = ""
        detail = detail if isinstance(detail, str) else ""
        return {"ok": False, "error": _safe_error(detail, password)
                or f"Forager answered with status {r.status_code}."}
    body = r.json() or {}
    if not body.get("instance_token"):
        return {"ok": False, "error": ("The sign-in worked but the reply was "
                                       "incomplete. Try again.")}
    applied = _apply_cloud_link(body, session=request.session)
    return {"ok": True,
            "account_email": body.get("account_email", email),
            "plan": body.get("plan", ""),
            **applied}


@router.get("/cloud/meta")
async def cloud_meta():
    """Forager feature discovery for the sign-in card, proxied so the browser
    never talks to the cloud directly.

    Drives the "Continue with Google" button: it renders only when the cloud
    is reachable and says Google sign-in is on. Unreachable or malformed
    replies degrade to every feature off, no button and no error, so the
    page never breaks over this.
    """
    base = settings.cloud_base_url.rstrip("/")
    try:
        async with _cloud_client() as client:
            r = await client.get(f"{base}/v1/meta")
        body = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        body = {}
    body = body if isinstance(body, dict) else {}
    return {"ok": True, "oauth_google": bool(body.get("oauth_google")),
            "google_start_url": f"{base}/auth/google/start"}


@router.get("/cloud/oauth-return")
async def cloud_oauth_return(request: Request, code: str = "", flow: str = ""):
    """Land the browser after "Continue with Google" on the Forager site.

    Forager redirects here with a one-time code; redeeming it against the
    pairing endpoint yields this install's credential, and the link then
    finishes exactly like a password sign-in (_apply_cloud_link). The user
    always ends up back where they started, the settings AI pane or the
    setup wizard's AI step, with any failure carried as a friendly message
    in the cloud_error query parameter.
    """
    # flow=wizard came from the setup wizard (the hint rides through the
    # return_url the button built); everything else returns to settings.
    if flow == "wizard":
        dest, err_dest = "/setup?cloud=done", "/setup?cloud_error={msg}"
    else:
        dest = "/setup#pane-scanning"
        err_dest = "/setup?cloud_error={msg}#pane-scanning"

    def fail(msg: str):
        from urllib.parse import quote
        return ingress_redirect(request, err_dest.format(msg=quote(msg)))

    code = code.strip()
    if not code:
        return fail("Google sign-in did not finish. Try again.")
    base = settings.cloud_base_url.rstrip("/")
    name = device_hostname() or "Pantry Raider"
    try:
        async with _cloud_client() as client:
            r = await client.post(f"{base}/v1/pairing/redeem",
                                  json={"code": code, "name": name})
    except httpx.HTTPError:
        return fail("Forager could not be reached. Check the internet "
                    "connection and try signing in again.")
    if r.status_code != 200:
        return fail("The Google sign-in expired or was already used. "
                    "Try signing in again.")
    body = r.json() or {}
    if not body.get("instance_token"):
        return fail("The sign-in worked but the reply was incomplete. "
                    "Try again.")
    _apply_cloud_link(body, session=request.session)
    return ingress_redirect(request, dest)


@router.post("/cloud/link")
async def cloud_link(payload: CloudLinkPayload):
    """Redeem a pairing code against the cloud and store the instance token."""
    code = payload.code.strip()
    if not code:
        return {"ok": False, "error": "Enter the pairing code from the cloud portal."}
    base = settings.cloud_base_url.rstrip("/")
    name = device_hostname() or "Pantry Raider"
    try:
        async with _cloud_client() as client:
            r = await client.post(f"{base}/v1/pairing/redeem",
                                  json={"code": code, "name": name})
    except httpx.HTTPError as e:
        return {"ok": False, "error": _safe_error(
            f"Forager could not be reached ({e.__class__.__name__}). "
            "Check the internet connection and try again.")}
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", "")
        except ValueError:
            detail = ""
        return {"ok": False, "error": detail or "The pairing code was not accepted."}
    token = (r.json() or {}).get("instance_token", "")
    if not token:
        return {"ok": False, "error": "The cloud reply had no instance token."}
    settings.save({"cloud_instance_token": token})
    reset_providers()
    return {"ok": True}


def cloud_convert_updates(vision_provider: str, enrich_provider: str,
                          tunnel_mode: str, tunnel_enabled: bool) -> dict:
    """Settings changes that switch a Forager-linked kitchen to self-hosted.

    Pure, so the convert flow and its test agree on exactly what changes. The
    Forager link is always forgotten. When Forager was the scanner (vision or
    enrichment provider "cloud"), it stops being the provider so the app does
    not keep claiming AI is ready against a service it just left: scanning is
    turned off until the user picks a self-hosted option (their own key or
    Ollama) rather than silently failing. A Forager web address / remote-access
    mode is turned off too. Nothing here touches inventory, recipes, other
    settings, or the device password: converting moves no data, it only changes
    which services this kitchen leans on.
    """
    updates: dict = {"cloud_instance_token": ""}
    if vision_provider == "cloud":
        # Fall back to the default provider, which has no key of its own, so
        # ai_configured() reads False and the AI-only UI hides until the user
        # sets up a self-hosted provider on the Scanning page.
        updates["vision_provider"] = "gemini"
    if enrich_provider == "cloud":
        updates["enrich_provider"] = ""
    # A legacy "subscription" mode reads as Forager; either way, clear it.
    if tunnel_mode in ("forager", "subscription"):
        updates["tunnel_mode"] = ""
    if tunnel_enabled:
        updates["tunnel_enabled"] = False
    return updates


def cloud_link_survives_setup(stamp: str, browser_marker: str) -> bool:
    """Whether a Forager connection made before setup finished is kept.

    Pure so the rule is testable on its own. No stamp means the account was
    connected by someone who was already signed in to this device, which is
    always kept. A stamp means it was connected while the device still had no
    login, so it survives only for the browser that did it: anyone else on the
    network could have reached that step, and a connection left behind by a
    stranger would let them sign in here afterwards with their own account.
    """
    if not stamp:
        return True
    return bool(browser_marker) and secrets.compare_digest(stamp, browser_marker)


def _settle_pre_setup_cloud_link(session) -> bool:
    """Decide the fate of a pre-setup Forager connection as setup completes.

    Returns True when a connection was dropped. The stamp is always cleared:
    from here on the settings surface needs a login, so a later connection
    carries no stamp and is trusted.
    """
    stamp = settings.cloud_link_session or ""
    if not stamp:
        return False
    keep = cloud_link_survives_setup(stamp, session.get("cloud_link_nonce", ""))
    updates: dict = {"cloud_link_session": ""}
    if not keep:
        updates.update(cloud_convert_updates(
            settings.vision_provider, settings.enrich_provider,
            settings.tunnel_mode, settings.tunnel_enabled))
    try:
        settings.save(updates)
    except OSError:
        return False
    if not keep:
        reset_providers()
    session.pop("cloud_link_nonce", None)
    return not keep


@router.post("/cloud/unlink")
async def cloud_unlink():
    """Switch this kitchen to self-hosted: forget the Forager link and land in
    a working local state.

    The kitchen keeps everything that already lives on the device (inventory,
    recipes, every setting, the device password); only the cloud add-ons go.
    Steps, all best effort so an unreachable Forager never blocks the switch:

      1. If Forager remote access is on, take it down first (while the link is
         still live, so the Forager side is told too).
      2. Ask Forager to revoke this install's credential (DELETE /v1/instance);
         an unreachable or already-revoked service is fine, the account page
         can remove the install too.
      3. Save the local switch: forget the credential, and if Forager was the
         scanner, stop pointing scanning at it so the app does not offer AI
         actions that can no longer work (cloud_convert_updates).
    """
    # Turn off Forager remote access before dropping the link, so the cloud side
    # is notified while the credential still works. tunnel_enabled tracks the
    # Forager WireGuard tunnel specifically; a Cloudflare tunnel is self-hosted
    # and left untouched.
    if settings.tunnel_enabled:
        # Imported here: remote_access registers its routes after this module.
        from .remote_access import tunnel_disable
        try:
            await tunnel_disable()
        except Exception:
            pass
    if settings.cloud_instance_token:
        base = settings.cloud_base_url.rstrip("/")
        try:
            async with _cloud_client() as client:
                await client.delete(f"{base}/v1/instance",
                                    headers=_cloud_headers())
        except httpx.HTTPError:
            pass
    settings.save(cloud_convert_updates(
        settings.vision_provider, settings.enrich_provider,
        settings.tunnel_mode, settings.tunnel_enabled))
    reset_providers()
    return {"ok": True}


@router.get("/cloud/status")
async def cloud_status():
    """Linked state plus the account's plan and quota, for the AI pane.

    Proxies GET /v1/instance/me with the stored token so the browser never
    sees the token itself. Unreachable or rejected replies come back as data,
    never as an error status: the settings page must render regardless.
    """
    if not settings.cloud_linked():
        return {"ok": True, "linked": False}
    base = settings.cloud_base_url.rstrip("/")
    try:
        async with _cloud_client() as client:
            r = await client.get(f"{base}/v1/instance/me",
                                 headers=_cloud_headers())
    except httpx.HTTPError as e:
        return {"ok": True, "linked": True, "reachable": False,
                "error": _safe_error(
                    f"Forager could not be reached ({e.__class__.__name__}).",
                    settings.cloud_instance_token)}
    if r.status_code == 401:
        return {"ok": True, "linked": True, "reachable": True, "valid": False,
                "error": ("Forager no longer recognizes this device. "
                          "Disconnect, then sign in again.")}
    if r.status_code != 200:
        return {"ok": True, "linked": True, "reachable": True, "valid": False,
                "error": f"Forager answered with status {r.status_code}."}
    body = r.json() or {}
    return {"ok": True, "linked": True, "reachable": True, "valid": True,
            "instance_id": body.get("instance_id"),
            "name": body.get("name", ""),
            "account_email": body.get("account_email", ""),
            "entitlement": body.get("entitlement", {})}


# ---- Back up to Forager (FoodAssistant-kzjz) ----
# A Premium account can push this install's data-directory zip to Forager cloud
# storage and pull it back. The cloud is the source of truth: it re-checks
# Premium on every call, so these endpoints treat "not Premium" as a normal
# data reply and the UI gate is convenience only. Everything is fail-soft: not
# linked, not Premium, or the cloud being down comes back as a clear message,
# and the local backup download still works.

def _cloud_disabled_message(r: httpx.Response) -> str | None:
    """The cloud's own explanation when it refused because the account is
    disabled (403 with detail.error "account_disabled"), else None. That is
    not a plan problem, so it must not read as the Premium upsell. The text
    is reduced to plain text: it is shown on the settings page."""
    if r.status_code != 403:
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    if not isinstance(detail, dict) or detail.get("error") != "account_disabled":
        return None
    message = detail.get("message")
    if isinstance(message, str):
        message = re.sub(r"<[^>]*>", "", message)
        message = message.replace("<", "").replace(">", "").strip()[:300]
    if not message:
        from ...providers.cloud import _DISABLED_MSG
        message = _DISABLED_MSG
    return message


@router.get("/cloud/backup/status")
async def cloud_backup_status():
    """Whether cloud backup is available here (linked and Premium) and the
    account's stored backups, for the Backups pane.

    Reuses the cloud's own Premium gate: GET /v1/backup/list answers 200 for a
    Premium account and 402 otherwise, so the app never has to trust a client
    guess. Unreachable or rejected replies come back as data, never an error
    status: the settings page must render regardless.
    """
    if not settings.cloud_linked():
        return {"ok": True, "linked": False, "premium": False, "backups": []}
    base = settings.cloud_base_url.rstrip("/")
    try:
        async with _cloud_client() as client:
            r = await client.get(f"{base}/v1/backup/list",
                                 headers=_cloud_headers())
    except httpx.HTTPError as e:
        return {"ok": True, "linked": True, "reachable": False, "premium": False,
                "backups": [], "error": _safe_error(
                    f"Forager could not be reached ({e.__class__.__name__}).",
                    settings.cloud_instance_token)}
    disabled = _cloud_disabled_message(r)
    if disabled:
        return {"ok": True, "linked": True, "reachable": True, "premium": False,
                "disabled": True, "backups": [], "error": disabled}
    if r.status_code in (402, 403):
        # Linked, but the account is not Premium: the feature is locked.
        return {"ok": True, "linked": True, "reachable": True, "premium": False,
                "backups": []}
    if r.status_code == 401:
        return {"ok": True, "linked": True, "reachable": True, "premium": False,
                "backups": [], "error": ("Forager no longer recognizes this "
                                         "device. Disconnect, then sign in "
                                         "again.")}
    if r.status_code != 200:
        return {"ok": True, "linked": True, "reachable": True, "premium": False,
                "backups": [], "error": f"Forager answered with status {r.status_code}."}
    body = r.json() or {}
    return {"ok": True, "linked": True, "reachable": True, "premium": True,
            "backups": body.get("backups", [])}


@router.post("/cloud/backup/upload")
async def cloud_backup_upload(include_secrets: bool = False):
    """Zip this install's data directory and store it in Forager.

    Builds the archive with the same builder as the local backup download, then
    POSTs it to the cloud. Premium is enforced on the cloud, so a non-Premium
    account gets a clear upgrade message here rather than a stored backup.
    """
    if not settings.cloud_linked():
        return {"ok": False, "error": ("This install is not linked to Forager. "
                                       "Sign in under Settings, AI first.")}
    from ..admin import _build_zip
    zip_bytes, filename = _build_zip(include_secrets=include_secrets)
    base = settings.cloud_base_url.rstrip("/")
    files = {"file": (filename, zip_bytes, "application/zip")}
    try:
        async with httpx.AsyncClient(timeout=_CLOUD_BACKUP_TIMEOUT) as client:
            r = await client.post(f"{base}/v1/backup/upload", files=files,
                                  headers=_cloud_headers())
    except httpx.HTTPError as e:
        return {"ok": False, "error": (
            f"Forager could not be reached ({e.__class__.__name__}). Your "
            "local backup download still works.")}
    disabled = _cloud_disabled_message(r)
    if disabled:
        return {"ok": False, "disabled": True, "error": disabled}
    if r.status_code in (402, 403):
        return {"ok": False, "premium": False, "error": (
            "Back up to Forager is a Premium feature. Upgrade your plan to turn "
            "it on. You can still download a local backup below.")}
    if r.status_code == 413:
        return {"ok": False, "error": ("This backup is too large to store in "
                                       "Forager. Download a local backup "
                                       "instead.")}
    if r.status_code != 200:
        return {"ok": False,
                "error": f"Forager could not store the backup (status {r.status_code})."}
    return {"ok": True, "backup": r.json() or {},
            "message": "Backed up to Forager."}


class CloudRestorePayload(BaseModel):
    # Which stored backup to restore; the newest is used when left unset.
    backup_id: int | None = None
    restore_password: str = ""


@router.post("/cloud/backup/restore")
async def cloud_backup_restore(payload: CloudRestorePayload):
    """Download a backup from Forager and run it through the existing guarded
    restore.

    The device password is required exactly like a local restore (the same gate
    admin.restore uses), so a walk-up at an open Settings page cannot pull the
    cloud backup down over the running data.
    """
    if not settings.cloud_linked():
        return {"ok": False, "error": "This install is not linked to Forager."}
    from ..admin import _require_current_password, _restore_zip
    try:
        _require_current_password(
            payload.restore_password,
            "Enter your current password to restore a backup.")
    except HTTPException as e:
        return {"ok": False, "error": e.detail}
    base = settings.cloud_base_url.rstrip("/")
    path = (f"/v1/backup/download/{int(payload.backup_id)}"
            if payload.backup_id else "/v1/backup/latest")
    try:
        async with httpx.AsyncClient(timeout=_CLOUD_BACKUP_TIMEOUT) as client:
            r = await client.get(f"{base}{path}", headers=_cloud_headers())
    except httpx.HTTPError as e:
        return {"ok": False, "error": (
            f"Forager could not be reached ({e.__class__.__name__}). Your data "
            "was not changed.")}
    disabled = _cloud_disabled_message(r)
    if disabled:
        return {"ok": False, "disabled": True, "error": disabled}
    if r.status_code in (402, 403):
        return {"ok": False, "premium": False, "error": (
            "Restore from Forager is a Premium feature. Upgrade your plan to "
            "turn it on.")}
    if r.status_code == 404:
        return {"ok": False, "error": "No Forager backup was found to restore."}
    if r.status_code != 200:
        return {"ok": False,
                "error": f"Forager could not return the backup (status {r.status_code})."}
    try:
        result = _restore_zip(r.content)
    except HTTPException as e:
        return {"ok": False, "error": e.detail}
    return {"ok": True, **result}
