"""How a satellite talks to its main server.

Every request a satellite (pi_remote) sends upstream goes through
request_kwargs(), so each one carries the same identification: X-PR-Client
names the satellite and its version, X-PR-Caps says what it can do (relay1:
it can have the server run AI, recipe, Home Assistant and camera calls on its
behalf), and X-API-Key carries its key when it has one. A server older than
0.21 ignores the two new headers, so they are always safe to send.

Call sites splat the result, ``client.get(url, **upstream.request_kwargs())``,
and pass any header of their own (Content-Type, a different X-PR-Caps) through
``extra`` so nothing is ever sent twice. Keep the dict shape: a later release
adds request signing under an "auth" entry without touching the callers.

The second half is the one forwarding path every satellite proxy shares:
forward() hands a browser's request to the server, send() makes a request
the satellite builds itself, and both answer with the server's reply verbatim
(or a 502 when the server cannot be reached). path_ok() and path_url() guard
a forwarded path that comes straight from the browser.

Nothing here logs a header value. The pull's extra caps (deckproxy1) are
worked out by services/satellite.py, so the only app module imported here is
app.config.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..config import APP_VERSION, settings

# Sent on every upstream call: this satellite can use the server's relays.
RELAY_CAP = "relay1"
# Sent on the config pull only, when the satellite's Stream Deck no longer uses
# the Home Assistant token itself (decided in services/satellite.py).
DECK_PROXY_CAP = "deckproxy1"


def client_id() -> str:
    """The X-PR-Client value: 'satellite/<version>'."""
    return "satellite/" + APP_VERSION


def headers(extra: dict | None = None) -> dict:
    """Headers for one upstream request.

    The identification headers first, then X-API-Key when this satellite has
    the shared key, then ``extra`` on top (so an extra X-PR-Caps replaces the
    default and a caller's Content-Type appears exactly once)."""
    out = {"X-PR-Client": client_id(), "X-PR-Caps": RELAY_CAP}
    key = settings.upstream_api_key
    if key:
        out["X-API-Key"] = key
    for name, value in (extra or {}).items():
        # Header names are case-insensitive: drop a default spelled
        # differently so the caller's value is the only one sent.
        for existing in [k for k in out if k.lower() == str(name).lower()]:
            del out[existing]
        out[name] = value
    return out


def request_kwargs(extra: dict | None = None) -> dict:
    """Keyword arguments for an httpx call to the main server."""
    return {"headers": headers(extra)}


def linked() -> bool:
    """True when this satellite has a server and a credential for it."""
    return settings.upstream_linked()


def signing_key() -> str:
    """The key the server signs this satellite's config pull with.

    services/satellite.py verifies the pull's HMAC with this and never reads
    the setting directly, so the source of the key can change in one place."""
    return settings.upstream_api_key


# -- Forwarding a request to the main server ---------------------------------------

# What a forward answers when the main server does not. The router forwards
# say "work again"; the forwarding middleware has always said "return".
UNREACHABLE = "The main server is not reachable. This will work again when it is."
UNREACHABLE_RETURN = "The main server is not reachable. This will return when it is."

_UNSET = object()


def unreachable(detail: str = UNREACHABLE) -> JSONResponse:
    """The 502 a forward answers with when the server cannot be reached."""
    return JSONResponse({"detail": detail}, status_code=502)


def mirror(up) -> Response:
    """The server's reply as this device's response: same body and status,
    and its content type (JSON when the server named none)."""
    return Response(content=up.content, status_code=up.status_code,
                    media_type=up.headers.get("content-type", "application/json"))


async def send(client, method: str, url: str, *, content_type: str | None = None,
               params=_UNSET, content=_UNSET, json=_UNSET,
               unreachable_detail: str = UNREACHABLE) -> Response:
    """Send one request to the main server and answer with its reply verbatim.

    ``client`` is the caller's own httpx.AsyncClient, so each caller keeps its
    timeout. Only the keyword arguments a caller passes reach the client:
    params, content and json go out exactly as given, and ``content_type``
    rides in through request_kwargs() so it is sent once. Any failure to get
    an answer (refused, timed out, DNS) becomes unreachable()."""
    kwargs = request_kwargs(
        extra={"Content-Type": content_type} if content_type else None)
    if params is not _UNSET:
        kwargs["params"] = params
    if content is not _UNSET:
        kwargs["content"] = content
    if json is not _UNSET:
        kwargs["json"] = json
    try:
        up = await client.request(method, url, **kwargs)
    except Exception:  # noqa: BLE001 - an unreachable server is a 502, never a crash
        return unreachable(unreachable_detail)
    return mirror(up)


async def forward(client, request: Request, url: str, *, params=None,
                  read_get_body: bool = False,
                  unreachable_detail: str = UNREACHABLE) -> Response:
    """Pass a browser's request to ``url`` on the main server: same method,
    query (or ``params`` when the caller adds to it), body and Content-Type.

    A GET or HEAD has no body to pass on. Reading one anyway is what hung the
    satellite kiosk poll, which runs several forwards on one request at once,
    so the routers never wait on one. The forwarding middleware has always
    read the body on every method and still does (``read_get_body``)."""
    if read_get_body or request.method not in ("GET", "HEAD"):
        body = await request.body()
    else:
        body = b""
    return await send(
        client, request.method, url,
        content_type=request.headers.get("content-type"),
        params=dict(request.query_params) if params is None else params,
        content=body or None,
        unreachable_detail=unreachable_detail,
    )


# A forwarded path must reach the main server as the one checked here. The
# HTTP client folds '.' and '..' segments ('/gadgets/../ha/connect' would leave
# as '/ha/connect'), so such a path, a backslash or a control character is
# refused, and the path is re-encoded so the server decodes it back to exactly
# this string (an encoded '%' stays a '%').
_PATH_UNSAFE_RE = re.compile(r"[\x00-\x1f\x7f\\]")
_PATH_SAFE_CHARS = "/:@!$&'()*+,;="
BAD_PATH_MSG = "That address is not valid."


def path_ok(path: str) -> bool:
    """True when ``path`` is safe to forward as it stands."""
    if not path.startswith("/") or _PATH_UNSAFE_RE.search(path):
        return False
    return not any(seg in (".", "..") for seg in path.split("/"))


def path_url(base: str, path: str) -> str:
    """``base`` plus ``path``, encoded so the server reads back ``path``."""
    return base + quote(path, safe=_PATH_SAFE_CHARS)
