"""Server-side proxy so satellites can reach Docker-internal Grocy/Mealie.

A satellite (deployment_mode=pi_remote) has no local Docker network, so it
cannot resolve the internal hostnames (http://grocy:80) the main server uses to
reach its backends. Instead the satellite sends its Grocy/Mealie API calls
here, authenticated with the shared X-API-Key, and the main server forwards
each call to its own backend using its own stored credentials.

Only Grocy and Mealie need this: AI providers are public internet services the
satellite reaches directly with the keys it pulls during config sync.
"""
from __future__ import annotations

import secrets

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..config import settings

router = APIRouter(prefix="/api/proxy", tags=["proxy"])

# A long timeout: some Grocy stock calls are slow on large inventories.
_client = httpx.AsyncClient(timeout=20.0)

_BACKENDS = {"grocy", "mealie"}


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
    backend is not configured on this server."""
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


@router.api_route("/{backend}/{path:path}",
                  methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(backend: str, path: str, request: Request):
    err = _auth_error(request)
    if err is not None:
        return err
    if backend not in _BACKENDS:
        return JSONResponse({"detail": f"Unknown backend '{backend}'"}, status_code=404)
    url, headers = _backend_target(backend, path)
    if url is None:
        return JSONResponse(
            {"detail": f"{backend} is not configured on the main server"},
            status_code=503)
    body = await request.body()
    try:
        upstream = await _client.request(
            request.method, url,
            headers=headers,
            params=dict(request.query_params),
            content=body or None,
        )
    except Exception as exc:  # backend unreachable from the server
        return JSONResponse(
            {"detail": f"proxy could not reach {backend}: {exc}"}, status_code=502)
    media = upstream.headers.get("content-type", "application/json")
    return Response(content=upstream.content, status_code=upstream.status_code,
                    media_type=media)
