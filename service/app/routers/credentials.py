"""API keys: list, create, revoke, narrow, and swap an older key (/api/credentials).

Everything here is on the admin surface except POST /api/credentials/exchange,
which a Home Assistant integration holding an older full-access key calls to
get a key of its own. A new key is shown once, with Cache-Control: no-store,
and only its sha256 is kept.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..passwords import verify_secret
from ..services import access, action_items, credentials
from ..services.rate_limit import RateLimiter

router = APIRouter(prefix="/api/credentials", tags=["credentials"])

# At most 5 swaps an hour for each older key.
exchange_guard = RateLimiter(max_attempts=5, window=3600.0)

_MINT_HOME_MSG = "Create keys from a device on your home network."
_NAME_MSG = "Give the key a name of up to 60 characters."
_SCOPE_BAD_MSG = "Choose what the key can do."
_UNKNOWN_MSG = "That key was not found."
_USED_SINCE_MSG = "{name} has connected since, so its key was kept."
_SHARED_AGAIN_MSG = "{name} gets service keys again on its next sync."
_SHARED_BUT_OFF_MSG = ("{name} can get service keys again, but Share service keys with "
                       "older satellites is set to Never, so it gets none until you "
                       "change that.")
_NO_STORE = {"Cache-Control": "no-store"}


class MintBody(BaseModel):
    scope: str = ""
    name: str = ""
    password: str = ""


class ScopeBody(BaseModel):
    scope: str = ""


class ExchangeBody(BaseModel):
    kind: str = ""


def _current_legacy_ids() -> set:
    ids = set()
    if settings.api_key:
        ids.add(credentials.legacy_id(settings.api_key))
    for k in settings.extra_api_keys if isinstance(settings.extra_api_keys, list) else []:
        if isinstance(k, str) and k and not k.startswith("pr1."):
            ids.add(credentials.legacy_id(k))
    return ids


def _item(rec: dict) -> dict:
    holders = rec.get("holders") or []
    fams = sorted({f.get("family") for f in rec.get("families") or [] if f.get("family")})
    return {
        "id": rec.get("id"),
        "name": credentials.display_name(rec.get("id")),
        "scope": rec.get("scope"),
        "scope_label": access.SCOPE_LABELS.get(rec.get("scope"), rec.get("scope")),
        "kind": rec.get("kind"),
        "status": rec.get("status"),
        "source": rec.get("source", ""),
        "created": rec.get("created"),
        "last_used": rec.get("last_used"),
        "last_client": rec.get("last_client", ""),
        "last_ip": rec.get("last_ip", ""),
        "families": fams,
        "expires": rec.get("expires"),
        "suggested_scope": credentials.suggested_scope(rec.get("id")),
        "holders": {"count": len(holders), "moved": sum(1 for h in holders if h.get("moved"))},
        "refusals_count": len(rec.get("refusals") or []),
        "service_keys_withheld": credentials.service_keys_withheld(rec),
    }


@router.get("")
async def list_credentials():
    credentials.ensure_legacy_records()
    live_legacy = _current_legacy_ids()
    items = []
    for rec in credentials.all_records():
        if rec.get("status") == "revoked":
            continue
        if rec.get("kind") == "legacy" and rec.get("id") not in live_legacy:
            continue
        items.append(_item(rec))
    items.sort(key=lambda i: (i["kind"] != "legacy", i.get("created") or ""))
    return {"ok": True, "credentials": items, "legacy_mirror": bool(settings.legacy_mirror),
            "open": not settings.auth_password}


def _valid_name(name: str) -> bool:
    return 1 <= len(name) <= 60 and all(ch.isprintable() for ch in name)


@router.post("")
async def mint(body: MintBody, request: Request):
    p = access.principal(request)
    scope = (body.scope or "").strip()
    name = (body.name or "").strip()
    if scope not in credentials.MINTABLE_SCOPES:
        return JSONResponse({"ok": False, "error": _SCOPE_BAD_MSG}, status_code=400)
    if not _valid_name(name):
        return JSONResponse({"ok": False, "error": _NAME_MSG}, status_code=400)
    if settings.auth_password:
        if p.scope != "admin" or not body.password or not verify_secret(
                body.password, settings.auth_password):
            return JSONResponse({"ok": False, "error": access.MINT_PASSWORD_MSG},
                                status_code=403)
    else:
        if p.origin not in ("lan_direct", "plain_loopback", "ingress"):
            return JSONResponse({"ok": False, "error": _MINT_HOME_MSG}, status_code=403)
        if scope == "admin":
            return JSONResponse({"ok": False, "error": access.ADMIN_KEY_OPEN_MSG},
                                status_code=403)
    rec, secret = credentials.mint(scope, name, "manual", created_by="settings")
    return JSONResponse({"ok": True, "id": rec["id"], "key": secret, "name": rec["name"],
                         "scope": rec["scope"], "expires": rec.get("expires")},
                        headers=_NO_STORE)


@router.post("/exchange")
async def exchange(body: ExchangeBody, request: Request):
    p = access.principal(request)
    if (body.kind or "").strip() != "ha":
        return JSONResponse({"ok": False, "error": "Only a Home Assistant key can be swapped here."},
                            status_code=400)
    rec = credentials.get(p.credential_id) if p.kind == "key" else None
    if rec is None or rec.get("kind") != "legacy":
        return JSONResponse({"ok": False, "error": access.EXCHANGE_LEGACY_MSG}, status_code=403)
    if p.origin not in ("lan_direct", "plain_loopback"):
        return JSONResponse({"ok": False, "error": access.EXCHANGE_LAN_MSG}, status_code=403)
    if exchange_guard.blocked(rec["id"]):
        return JSONResponse({"ok": False, "error": access.EXCHANGE_RATE_MSG}, status_code=429)
    exchange_guard.record(rec["id"])
    meta = {
        "ip": access.unwrap_peer(request.client.host if request.client else None) or "",
        "client": p.client,
        "ua": request.headers.get("user-agent", ""),
        "device_id": request.headers.get("x-device-id", ""),
    }
    new, secret = credentials.exchange(rec, "ha", meta)
    # This call is the holder moving off the old key; recording it as a use
    # of the old key would mark the holder as unmoved again.
    request.state.pr_skip_use = True
    return JSONResponse({"ok": True, "id": new["id"], "key": secret}, headers=_NO_STORE)


@router.post("/{cid}/revoke")
async def revoke(cid: str):
    try:
        credentials.revoke(cid)
    except KeyError:
        return JSONResponse({"ok": False, "error": _UNKNOWN_MSG}, status_code=404)
    except credentials.CredentialError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True}


@router.post("/{cid}/remove-unused")
async def remove_unused(cid: str, db: Session = Depends(get_db)):
    """Remove a paired key that was never used: the Remove button on its
    inbox offer (FoodAssistant-x60df). A key whose device has connected since
    is kept. Either way the offer is closed."""
    name = credentials.display_name(cid)
    try:
        removed = credentials.remove_if_never_used(cid)
    except KeyError:
        return JSONResponse({"ok": False, "error": _UNKNOWN_MSG}, status_code=404)
    except credentials.CredentialError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    await run_in_threadpool(action_items.settle_paired_key_offer, db, cid)
    if not removed:
        return JSONResponse({"ok": False, "used": True,
                             "error": _USED_SINCE_MSG.format(name=name)}, status_code=409)
    return {"ok": True}


@router.post("/{cid}/share-service-keys")
async def share_service_keys(cid: str):
    """Let this key receive service keys again (FoodAssistant-xym8y).

    For a Bandit put back on a version before 0.21 after its key stopped
    getting the AI, recipe and Home Assistant keys: the key gets them again
    until every device using it has updated."""
    name = credentials.display_name(cid)
    try:
        changed = credentials.clear_relay_ratchet(cid)
    except KeyError:
        return JSONResponse({"ok": False, "error": _UNKNOWN_MSG}, status_code=404)
    except credentials.CredentialError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    off = getattr(settings, "satellite_legacy_credentials", "auto") == "off"
    msg = _SHARED_BUT_OFF_MSG if off else _SHARED_AGAIN_MSG
    return {"ok": True, "changed": changed, "message": msg.format(name=name)}


@router.post("/{cid}/scope")
async def set_scope(cid: str, body: ScopeBody):
    try:
        previous = credentials.set_scope(cid, (body.scope or "").strip())
    except KeyError:
        return JSONResponse({"ok": False, "error": _UNKNOWN_MSG}, status_code=404)
    except credentials.CredentialError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "scope": body.scope.strip(), "previous": previous}
