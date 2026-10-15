"""Steer the kitchen screen's browser through the kiosk launcher's socket.

The kiosk launcher owns the browser's debugging connection and listens on a
small Unix socket. A nav key sends it one request, a path on the app such as
'/ui/add', and the launcher opens that page on the screen's own address, so
the screen keeps its signed-in session and the debugging port can stay
closed.

Keep this file's name: the launcher looks for it to tell a deck that speaks
the socket from an older one that still needs the debugging port.

Wire format, one request per connection: one JSON object and a newline, then
one JSON reply and a newline. {"op": "ping"} answers {"ok": true,
"version": 1}; {"op": "navigate", "path": "/ui/add"} answers {"ok": true} or
{"ok": false, "error": "<code>"}.

Standard library only, so it runs on the deck's Python 3.9.
"""
from __future__ import annotations

import asyncio
import json
import socket
from typing import Optional
from urllib.parse import urlsplit

DEFAULT_SOCKET = "/run/foodassistant-kiosk/nav.sock"

# The launcher reads at most this many bytes of one request.
MAX_REQUEST_BYTES = 4096

# --- nav path rule (shared with the Stream Deck; keep byte-identical) ---
NAV_PATH_MAX = 2048


def valid_nav_path(path):
    """True for a path on the app: a non-empty str of at most NAV_PATH_MAX
    characters that starts with exactly one '/' and holds no backslash and
    no control character (below 0x20, or 0x7f)."""
    if not isinstance(path, str) or not path or len(path) > NAV_PATH_MAX:
        return False
    if not path.startswith("/") or path.startswith("//"):
        return False
    if "\\" in path:
        return False
    return not any(ord(c) < 0x20 or ord(c) == 0x7f for c in path)
# --- end nav path rule ---


def to_nav_path(path) -> str:
    """The deck's key path as a path on the app: one leading '/'.

    'ui/add' becomes '/ui/add', and '//evil.example' becomes '/evil.example',
    which is a page on the app, never another host."""
    return "/" + str(path or "").lstrip("/")


def _bare_hostname(hostname: Optional[str] = None) -> str:
    """This device's host name, lowercased, when it is a single label.

    A dotted name ('kitchen-pi.lan', 'kitchen-pi.local') resolves through DNS
    or mDNS to a LAN address, so it never counts as this device."""
    try:
        name = hostname if hostname is not None else socket.gethostname()
    except OSError:
        return ""
    name = str(name or "").strip().lower()
    if not name or "." in name:
        return ""
    return name


def is_local_host(host, hostname: Optional[str] = None) -> bool:
    """True when ``host`` names this device: 127.0.0.1, localhost, ::1 or
    the bare host name. The kiosk launcher, the host tools and the bridge use
    the same rule."""
    h = str(host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h in ("127.0.0.1", "localhost", "::1"):
        return True
    bare = _bare_hostname(hostname)
    return bool(bare) and h == bare


def _port_of(base_url: str) -> Optional[int]:
    try:
        parts = urlsplit(base_url or "")
        if parts.port:
            return parts.port
        return 443 if parts.scheme == "https" else 80
    except ValueError:
        return None


def _origin(url) -> str:
    """scheme://host[:port] of an http(s) URL with a host, without any
    login part, or ""."""
    try:
        parts = urlsplit(str(url or ""))
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return ""
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.rsplit('@', 1)[-1]}"


def kiosk_origin(page_url, base_url: str, hostname: Optional[str] = None) -> str:
    """Origin to steer the kiosk page to over the debugging endpoint.

    The page's own scheme://host[:port] when its host is this device, so the
    screen keeps the session cookie it signed in with (a hosted Pi often runs
    its kiosk on localhost while the deck talks to 127.0.0.1). Otherwise
    http://localhost:<port of base_url>, never a host the page wandered to.
    A deck whose app is on another machine (base_url's host is not this
    device) keeps base_url's own origin, as it always has, since nothing
    answers on localhost there."""
    page = _origin(page_url)
    if page and is_local_host(urlsplit(page).hostname, hostname):
        return page
    base = _origin(base_url)
    if base and not is_local_host(urlsplit(base).hostname, hostname):
        return base
    scheme = urlsplit(base).scheme if base else "http"
    port = _port_of(base_url)
    if port and port != (443 if scheme == "https" else 80):
        return f"{scheme}://localhost:{port}"
    return f"{scheme}://localhost"


async def request(message: dict, socket_path: str = DEFAULT_SOCKET,
                  timeout: float = 3.0) -> Optional[dict]:
    """Send one request and return the reply, or None.

    None on any failure: no socket, a refused or reset connection, no reply
    within ``timeout`` seconds, or a reply that is not a JSON object."""
    async def _exchange() -> Optional[dict]:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        try:
            writer.write(json.dumps(message).encode("utf-8") + b"\n")
            await writer.drain()
            line = await reader.readline()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - the reply is already read
                pass
        reply = json.loads(line.decode("utf-8")) if line else None
        return reply if isinstance(reply, dict) else None

    try:
        return await asyncio.wait_for(_exchange(), timeout=timeout)
    except Exception:  # noqa: BLE001 - every failure means "no answer"
        return None


async def navigate(path: str, socket_path: str = DEFAULT_SOCKET,
                   timeout: float = 3.0) -> tuple:
    """Ask the launcher to open ``path``. Returns (ok, error).

    The path is sent as given; the launcher answers bad_path for anything
    valid_nav_path() refuses, so callers pass it through to_nav_path() first.
    error is the launcher's code, or 'no_reply' when it did not answer."""
    reply = await request({"op": "navigate", "path": path}, socket_path, timeout)
    if reply is None:
        return False, "no_reply"
    if reply.get("ok") is True:
        return True, ""
    return False, str(reply.get("error") or "unknown")


async def ping(socket_path: str = DEFAULT_SOCKET, timeout: float = 3.0) -> bool:
    """True when the launcher answers {ok: true, version: 1}."""
    reply = await request({"op": "ping"}, socket_path, timeout)
    return bool(reply) and reply.get("ok") is True and reply.get("version") == 1
