"""Server-side proxy so satellites can reach Docker-internal Grocy/Mealie.

A satellite (deployment_mode=pi_remote) has no local Docker network, so it
cannot resolve the internal hostnames (http://grocy:80) the main server uses to
reach its backends. Instead the satellite sends its Grocy/Mealie API calls
here, authenticated with the shared X-API-Key, and the main server forwards
each call to its own backend using its own stored credentials.

Only Grocy and Mealie need this: AI providers are public internet services the
satellite reaches directly with the keys it pulls during config sync.

The hop carries only what Pantry Raider clients use (services/proxy_policy.py).
Account, key and admin paths are refused in every mode; any other path off the
list is passed along and recorded while settings.proxy_path_policy is "report"
(the default) and refused under "enforce". GET /admin/proxy-paths shows the
owner what has come through.
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import date

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..config import settings
from ..services import proxy_policy

logger = logging.getLogger("foodassistant.proxy")

router = APIRouter(prefix="/api/proxy", tags=["proxy"])
# Owner-only review of what the hop has carried. The /admin prefix keeps it
# behind the admin gate in main.py.
admin_router = APIRouter(prefix="/admin", tags=["proxy"])

# A long timeout: some Grocy stock calls are slow on large inventories.
_client = httpx.AsyncClient(timeout=20.0)

_BACKENDS = {"grocy", "mealie"}

# An off-list path in report mode logs a warning at most this often per
# (credential, backend, method, template), so a chatty client cannot fill the
# log. The recorder still counts every request.
_WARN_INTERVAL = 3600.0
_warned: dict[tuple, float] = {}
# At most this many distinct off-list paths are warned about per hour, so a
# client inventing new paths cannot fill the log either.
_WARN_MAX = 200
# Refusals already raised in the action inbox today, so a refused client that
# retries does not write to the database on every attempt.
_refusals_noted: set[str] = set()
# Inbox items raised per (day, credential). One misbehaving key gets a few
# items describing what it tried, not one per distinct path.
_REFUSAL_ITEMS_PER_DAY = 10
_refusal_counts: dict[str, int] = {}


def _credential_name(sent: str) -> str | None:
    """The name of the API key ``sent`` matches, or None when it matches none.

    Every accepted key is compared (constant time each), so the answer does not
    leak which key came close. The key itself is never returned or logged:
    the primary key is "primary key", an extra key its label from Settings or
    "extra key N".
    """
    if not sent:
        return None
    match = None
    if settings.api_key and secrets.compare_digest(sent, settings.api_key):
        match = "primary key"
    extras = settings.extra_api_keys if isinstance(settings.extra_api_keys, list) else []
    names = settings.extra_api_key_names if isinstance(settings.extra_api_key_names, list) else []
    for i, k in enumerate(extras):
        if k and secrets.compare_digest(sent, k) and match is None:
            label = names[i] if i < len(names) and isinstance(names[i], str) else ""
            match = label.strip()[:60] or f"extra key {i + 1}"
    return match


def _auth_error(request: Request):
    """Return a JSONResponse if the caller is not an authorized satellite, else None.

    The proxy enforces its own X-API-Key check so it stays safe even when the
    server runs with authentication disabled (an outer layer gating the UI).
    """
    valid = settings.valid_api_keys()
    if not valid:
        return JSONResponse({"detail": "Server API key not set"}, status_code=503)
    sent = request.headers.get("X-API-Key", "")
    if not sent or not any(secrets.compare_digest(sent, k) for k in valid):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return None


def _backend_target(backend: str, path: str):
    """Return (url, headers) for the forwarded call, or (None, None) if the
    backend is not configured on this server. ``path`` is already re-encoded
    by proxy_policy.upstream_path."""
    if backend == "grocy":
        base = settings.grocy_base_url.rstrip("/")
        if not base:
            return None, None
        headers = {"GROCY-API-KEY": settings.grocy_api_key,
                   "Content-Type": "application/json"}
    else:  # mealie
        base = settings.mealie_base_url.rstrip("/")
        if not base:
            return None, None
        headers = {"Authorization": f"Bearer {settings.mealie_api_key}",
                   "Content-Type": "application/json"}
    return f"{base}/{path}", headers


def _client_label(request: Request) -> str:
    """Who is calling, from X-PR-Client ('satellite/0.20.3'). A satellite older
    than this header shows as 'unlabelled'. Trimmed to printable ASCII."""
    raw = request.headers.get("X-PR-Client", "") or ""
    clean = "".join(ch for ch in raw if 32 < ord(ch) < 127)[:64]
    return clean or "unlabelled"


def _warn_offlist(credential: str, backend: str, method: str, template: str,
                  client: str) -> None:
    key = (credential, backend, method, template)
    now = time.monotonic()
    if key in _warned and now - _warned[key] < _WARN_INTERVAL:
        return
    if key not in _warned and len(_warned) >= _WARN_MAX:
        for k in [k for k, t in _warned.items() if now - t >= _WARN_INTERVAL]:
            del _warned[k]
        if len(_warned) >= _WARN_MAX:
            return
    _warned[key] = now
    logger.warning("proxy: passed along a %s request that is not on the list: "
                   "%s /%s (from %s, %s). Enforce mode would refuse it.",
                   backend, method, template, client, credential)


def _note_refusal(credential: str, backend: str, method: str, template: str,
                  client: str, denied: bool = False) -> None:
    """Raise the first refusal per credential and template per day in the
    action inbox, up to _REFUSAL_ITEMS_PER_DAY items per credential. Never
    raises: the refusal itself must still be answered."""
    day = date.today().isoformat()
    dedupe = f"proxy_refused:{day}:{credential}:{backend}:{method}:{template}"
    if dedupe in _refusals_noted:
        return
    budget = f"{day}:{credential}"
    if _refusal_counts.get(budget, 0) >= _REFUSAL_ITEMS_PER_DAY:
        return
    label = proxy_policy.BACKEND_LABELS.get(backend, backend)
    try:
        from ..database import SessionLocal
        from ..services import action_items
        db = SessionLocal()
        try:
            action_items.create(
                db, "proxy_refused", f"A {label} request from a device was refused",
                body=(f'A device using the API key "{credential}" ({client}) asked '
                      f"this server to pass along {method} /{template} to {label}, "
                      "and it refused. " + (
                          "No Pantry Raider device asks for this, since it reaches "
                          f"{label} accounts, keys or admin settings. "
                          if denied else
                          "If this is one of your devices, update it and this "
                          "server to the same version. ")
                      + "If you do not know the device, remove that key in "
                      "Settings, Security."),
                dedupe_key=dedupe,
                level="warning",
                payload={"backend": backend, "method": method,
                         "template": template, "credential": credential,
                         "client": client},
            )
        finally:
            db.close()
    except Exception as exc:
        logger.warning("proxy: could not record a refused request: %s", exc)
        return
    if len(_refusals_noted) > 1000:
        _refusals_noted.clear()
    _refusals_noted.add(dedupe)
    if budget not in _refusal_counts and len(_refusal_counts) > 1000:
        _refusal_counts.clear()
    _refusal_counts[budget] = _refusal_counts.get(budget, 0) + 1


@router.api_route("/{backend}/{path:path}",
                  methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(backend: str, path: str, request: Request):
    err = _auth_error(request)
    if err is not None:
        return err
    if backend not in _BACKENDS:
        return JSONResponse({"detail": f"Unknown backend '{backend}'"}, status_code=404)
    method = request.method.upper()
    credential = _credential_name(request.headers.get("X-API-Key", "")) or "unknown"
    client = _client_label(request)
    try:
        segments = proxy_policy.validate_path(path)
    except proxy_policy.ProxyPathInvalid:
        proxy_policy.record(backend, method, "INVALID", credential, client,
                            proxy_policy.VERDICT_INVALID)
        return JSONResponse({"detail": proxy_policy.invalid_detail(backend),
                             "code": proxy_policy.CODE_INVALID}, status_code=400)
    clean = "/".join(segments)
    verdict, template = proxy_policy.classify(backend, method, clean)
    proxy_policy.record(backend, method, template, credential, client, verdict)
    if verdict == proxy_policy.VERDICT_DENY:
        _note_refusal(credential, backend, method, template, client, denied=True)
        return JSONResponse(
            {"detail": proxy_policy.denied_detail(backend, method, clean),
             "code": proxy_policy.CODE_DENIED}, status_code=403)
    if verdict == proxy_policy.VERDICT_OFFLIST:
        if proxy_policy.enforcing(settings.proxy_path_policy):
            _note_refusal(credential, backend, method, template, client)
            return JSONResponse(
                {"detail": proxy_policy.not_allowed_detail(backend, method, clean),
                 "code": proxy_policy.CODE_NOT_ALLOWED}, status_code=403)
        _warn_offlist(credential, backend, method, template, client)
    url, headers = _backend_target(backend, proxy_policy.upstream_path(segments))
    if url is None:
        return JSONResponse(
            {"detail": f"{backend} is not configured on the main server"},
            status_code=503)
    body = await request.body()
    try:
        upstream = await _client.request(
            method, url,
            headers=headers,
            # multi_items keeps a repeated query key (Grocy's query[]) intact.
            params=list(request.query_params.multi_items()),
            content=body or None,
        )
    except Exception as exc:  # backend unreachable from the server
        return JSONResponse(
            {"detail": f"proxy could not reach {backend}: {exc}"}, status_code=502)
    media = upstream.headers.get("content-type", "application/json")
    return Response(content=upstream.content, status_code=upstream.status_code,
                    media_type=media)


@admin_router.get("/proxy-paths")
async def proxy_paths():
    """What the satellite proxy has carried: one row per backend, method, path
    template and verdict, with counts, first and last seen, and which keys and
    clients sent it."""
    return {"policy": "enforce" if proxy_policy.enforcing(settings.proxy_path_policy)
            else "report",
            "paths": proxy_policy.snapshot()}
