"""Server-side proxy so satellites can reach Docker-internal Grocy/Mealie,
and so Stream Decks reach Home Assistant without a token of their own.

A satellite (deployment_mode=pi_remote) has no local Docker network, so it
cannot resolve the internal hostnames (http://grocy:80) the main server uses to
reach its backends. Instead the satellite sends its Grocy/Mealie API calls
here with its key (a satellite, legacy or admin key, read through
access.principal), and the main server forwards each call to its own backend
using its own stored credentials.

The Grocy and Mealie hop carries only what Pantry Raider clients use
(services/proxy_policy.py). Account, key and admin paths are refused in every
mode; any other path off the list is refused while settings.proxy_path_policy
is "enforce" (the default) and passed along and recorded under "report". GET
/admin/proxy-paths shows the owner what has come through.

The "ha" backend (/api/proxy/ha/...) carries Home Assistant state reads,
camera pictures and service calls for the keys the owner has set up
(services/ha_proxy.py). This device's Stream Deck uses it with its agent key
(or, while the old trust for programs on this device lasts, with none), and a
satellite with its key. On a satellite whose main server relays Home Assistant
the request goes on to the main server's own hop. Home Assistant calls are
never written to the path record.
"""
from __future__ import annotations

import logging
import time
from datetime import date

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..config import settings
from ..services import ha_proxy, proxy_policy

logger = logging.getLogger("foodassistant.proxy")

router = APIRouter(prefix="/api/proxy", tags=["proxy"])
# Owner-only review of what the hop has carried. The /admin prefix keeps it
# behind the admin gate in main.py.
admin_router = APIRouter(prefix="/admin", tags=["proxy"])

# A long timeout: some Grocy stock calls are slow on large inventories.
_client = httpx.AsyncClient(timeout=20.0)

_BACKENDS = {"grocy", "mealie", "ha"}
# Keys the Grocy and Mealie hop serves.
_KEY_SCOPES = frozenset({"satellite", "legacy", "admin"})
_MAIN_SERVER_UNREACHABLE_MSG = "Could not reach the main server."

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


def _key_error(p):
    """None when ``p`` holds a key that may use the Grocy and Mealie hop (a
    satellite, legacy or admin key), else the JSONResponse to answer with.

    The hop always needed a key, so a signed-in session or a program trusted
    on this device without one is answered like a caller with no key. The
    401 and 503 bodies are the ones every satellite release already knows."""
    from ..services import access, credentials
    if p.kind == "key" and p.scope in _KEY_SCOPES:
        return None
    if p.kind in ("key", "agent"):
        return JSONResponse({"detail": access.SCOPE_MSG}, status_code=403)
    if not credentials.any_credential_exists():
        return JSONResponse({"detail": "Server API key not set"}, status_code=503)
    return JSONResponse({"detail": "Unauthorized"}, status_code=401)


def _credential_label(p) -> str:
    """The key's name as Settings shows it, never the key itself."""
    from ..services import credentials
    try:
        return credentials.display_name(p.credential_id or "") or "unknown"
    except Exception:  # noqa: BLE001 - a name is only for the record
        return "unknown"


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


async def proxy(backend: str, path: str, request: Request):
    from ..services import access
    p = access.principal(request)
    if backend == "ha":
        return await _proxy_ha(path, request, p)
    err = _key_error(p)
    if err is not None:
        return err
    if backend not in _BACKENDS:
        return JSONResponse({"detail": f"Unknown backend '{backend}'"}, status_code=404)
    method = request.method.upper()
    credential = _credential_label(p)
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
             "code": proxy_policy.CODE_DENIED,
             "request": proxy_policy.request_label(method, clean)}, status_code=403)
    if verdict == proxy_policy.VERDICT_OFFLIST:
        if proxy_policy.enforcing(settings.proxy_path_policy):
            _note_refusal(credential, backend, method, template, client)
            return JSONResponse(
                {"detail": proxy_policy.not_allowed_detail(backend, method, clean),
                 "code": proxy_policy.CODE_NOT_ALLOWED,
                 "request": proxy_policy.request_label(method, clean)}, status_code=403)
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
        # The exception text names the backend's internal address, so it goes
        # to the log only; the caller gets a fixed message.
        logger.warning("proxy could not reach %s: %s", backend, exc)
        return JSONResponse(
            {"detail": f"The main server could not reach {backend}."}, status_code=502)
    media = upstream.headers.get("content-type", "application/json")
    return Response(content=upstream.content, status_code=upstream.status_code,
                    media_type=media)


# One route per method, each with its own name, so the OpenAPI schema gets a
# distinct operation ID for every method instead of five copies of "proxy".
for _method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
    router.add_api_route("/{backend}/{path:path}", proxy, methods=[_method],
                         name=f"proxy_{_method.lower()}")


# --- Home Assistant -------------------------------------------------------------


def _ha_profiles() -> list:
    """The saved Stream Deck profiles, whose keys count as set up too. [] when
    the database cannot be read, which can only refuse more, never less."""
    try:
        from ..database import SessionLocal
        from ..services import ha_allowlist
        db = SessionLocal()
        try:
            return ha_allowlist.load_profiles(db)
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.debug("proxy: could not read the Stream Deck profiles", exc_info=True)
        return []


def _ha_refusal(exc, p, path: str):
    from ..services import credentials
    if exc.status == 403 and p.kind == "key" and p.credential_id:
        try:
            credentials.record_refusal(p.credential_id, "/api/proxy/ha/" + path[:150])
        except Exception:  # noqa: BLE001 - the refusal must still be answered
            pass
    return JSONResponse(exc.body(), status_code=exc.status)


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """The request body, or None once it grows past ``limit`` bytes, so a
    body sent without a length is never read whole into memory."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _thermometer_reading_ok(entity: str, state: dict) -> bool:
    """An entity readable only because a thermometer or hygrometer lists it
    is passed on only when its state is a temperature or humidity reading.
    Anything that opens a kitchen page can add a thermometer, so the list
    alone must not open up a person's location or a lock's state."""
    from ..services import gadgets_ha, ha_allowlist
    try:
        if not ha_allowlist.reading_needs_sensor_check(entity, settings, _ha_profiles()):
            return True
        return bool(gadgets_ha.is_temperature_entity(state)
                    or gadgets_ha.is_humidity_entity(state))
    except Exception:  # noqa: BLE001 - when unsure, pass nothing on
        return False


async def _proxy_ha(path: str, request: Request, p):
    if ha_proxy.caller_source(p) is None:
        # Nobody with a credential: 401. A key or agent this hop does not
        # serve: the same 403 as a call that is not set up.
        refused = (ha_proxy.HaNotAllowed() if p.kind in ("key", "agent")
                   else ha_proxy.HaUnauthorized())
        return _ha_refusal(refused, p, path)
    method = request.method.upper()
    try:
        segments = proxy_policy.validate_path(path)
    except proxy_policy.ProxyPathInvalid:
        return _ha_refusal(ha_proxy.HaNotAllowed(), p, "INVALID")
    body = b""
    if method == "POST":
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        if declared > ha_proxy.MAX_BODY_BYTES:
            # Not a body the deck or a satellite sends; not read at all.
            return _ha_refusal(ha_proxy.HaNotAllowed(), p, "/".join(segments))
        body = await _read_capped(request, ha_proxy.MAX_BODY_BYTES)
        if body is None:
            # A chunked body with no length that runs past the cap.
            return _ha_refusal(ha_proxy.HaNotAllowed(), p, "/".join(segments))
    try:
        call = ha_proxy.parse(method, segments, body)
        auth = ha_proxy.authorize(call, p, settings, _ha_profiles())
    except ha_proxy.HaRefused as exc:
        return _ha_refusal(exc, p, "/".join(segments))
    from ..services import ha_client
    try:
        mode = ha_client.mode()
    except Exception:  # noqa: BLE001
        mode = "none"
    if mode == "upstream":
        return await _ha_forward_upstream(call)
    if mode != "direct":
        return JSONResponse({"detail": ha_proxy.NOT_CONNECTED_MSG}, status_code=503)
    unreachable = getattr(ha_client, "UNREACHABLE_MSG", "Could not reach Home Assistant.")
    if call.kind == ha_proxy.KIND_STATE:
        try:
            state = await ha_client.get_state(call.entity_ids[0])
        except Exception:  # noqa: BLE001 - never pass exception text along
            return JSONResponse({"detail": unreachable}, status_code=502)
        if not isinstance(state, dict):
            return JSONResponse({"detail": ha_proxy.NOT_FOUND_MSG}, status_code=404)
        if not _thermometer_reading_ok(call.entity_ids[0], state):
            return _ha_refusal(ha_proxy.HaNotAllowed(), p, "/".join(segments))
        return JSONResponse(ha_proxy.public_state(state), status_code=200)
    if call.kind == ha_proxy.KIND_CAMERA:
        return await _ha_camera(call, unreachable)
    last = 200
    for entity in call.entity_ids:
        try:
            result = await ha_client.call_service(
                entity, call.service_id, source=auth.source,
                entry=auth.entries.get(entity))
        except Exception:  # noqa: BLE001
            result = {"ok": False, "status": 0, "detail": unreachable}
        if not isinstance(result, dict) or not result.get("ok"):
            result = result if isinstance(result, dict) else {}
            status = int(result.get("status") or 0)
            detail = str(result.get("detail") or unreachable)
            return JSONResponse({"ok": False, "detail": detail},
                                status_code=status if 400 <= status < 600 else 502)
        last = int(result.get("status") or 200)
    return JSONResponse({"ok": True}, status_code=last if 200 <= last < 300 else 200)


async def _ha_camera(call, unreachable: str):
    from ..services import ha_client
    try:
        status, content, ctype = await ha_client.camera_snapshot(call.entity_ids[0])
    except Exception:  # noqa: BLE001
        status, content, ctype = 0, b"", ""
    if status == 404:
        return JSONResponse({"detail": ha_proxy.NOT_FOUND_MSG}, status_code=404)
    ctype = str(ctype or "")
    low = ctype.strip().lower()
    # Only a picture is passed along. SVG is an image that can carry script,
    # and this answer comes from the app's own address, so it is refused too.
    if status != 200 or not low.startswith("image/") or low.startswith("image/svg"):
        if status in (401, 403):
            detail = getattr(ha_client, "REFUSED_MSG", unreachable)
        elif status == 200:
            detail = ha_proxy.NO_PICTURE_MSG
        elif status:
            detail = f"Home Assistant answered {status}."
        else:
            detail = unreachable
        return JSONResponse({"detail": detail}, status_code=502)
    return Response(content=content, status_code=200, media_type=ctype,
                    headers={"Cache-Control": "no-store",
                             "X-Content-Type-Options": "nosniff"})


async def _ha_forward_upstream(call):
    """A relay satellite passes the call on to its main server's own hop,
    with its own key. The main server decides again with its own settings."""
    from ..services import upstream
    remote = str(settings.remote_server_url or "").strip().rstrip("/")
    if not remote:
        return JSONResponse({"detail": ha_proxy.NOT_CONNECTED_MSG}, status_code=503)
    body = call.body_bytes()
    extra = {"Content-Type": "application/json"} if body else None
    try:
        r = await _client.request(
            "POST" if body else "GET", f"{remote}/api/proxy/ha/{call.path()}",
            content=body or None, **upstream.request_kwargs(extra=extra))
    except Exception:  # noqa: BLE001 - the URL is never shown
        return JSONResponse({"detail": _MAIN_SERVER_UNREACHABLE_MSG}, status_code=502)
    media = r.headers.get("content-type", "application/json")
    return Response(content=r.content, status_code=r.status_code, media_type=media)


@admin_router.get("/proxy-paths")
async def proxy_paths():
    """What the satellite proxy has carried: one row per backend, method, path
    template and verdict, with counts, first and last seen, and which keys and
    clients sent it."""
    return {"policy": "enforce" if proxy_policy.enforcing(settings.proxy_path_policy)
            else "report",
            "paths": proxy_policy.snapshot()}
