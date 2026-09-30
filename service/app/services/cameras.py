"""Resolve a configured camera to a fetchable upstream feed.

Home Assistant camera proxy endpoints (``/api/camera_proxy`` for a still,
``/api/camera_proxy_stream`` for an MJPEG stream) authenticate with an
``Authorization: Bearer <token>`` header, NOT a long-lived token in the query
string. A browser ``<img>``/``<video>`` cannot send that header, so HA cameras
must be fetched server-side (or, on the deck, by a client that can set headers).

A camera entry in ``streamdeck_cameras`` is a dict. HA-backed cameras carry an
``ha_entity`` (the ``camera.*`` entity id); the URL is built from the stored HA
base URL and token at fetch time. Manual cameras carry direct ``stream_url`` /
``snapshot_url`` instead. For backward compatibility we also recover the entity
from an older entry whose URL still has the (non-working) token baked in.
"""
from __future__ import annotations

import ipaddress
import random
import re
import socket
import time
from urllib.parse import parse_qsl, quote, unquote, urlparse, urlsplit
from typing import Optional

from ..config import settings

# The two HA camera proxy path segments, mapped to the feed kind we want.
_HA_PATHS = {"snapshot": "camera_proxy", "stream": "camera_proxy_stream"}


# A short, user-forward refusal shared by every camera fetch that is pointed at
# an address no real camera lives on (the server itself, or an internal-only
# address). Kept vague on purpose: it should read as "that address will not
# work" without hinting at what is behind it.
BLOCKED_HOST_MESSAGE = (
    "That camera address points at this device or an internal address, so it "
    "cannot be used. Enter the camera's own address on your network.")


def _fetch_host(host_or_url: str) -> str:
    """The bare hostname/IP from a URL or a plain ``host[:port]`` string.

    Accepts what a camera field might hold: a full ``http://host:port/path``
    URL, or just ``host`` / ``host:port``. Returns "" when nothing usable is
    present. Pure, so the block rule below stays unit-testable.
    """
    s = (host_or_url or "").strip()
    if not s:
        return ""
    # Give a bare host[:port] a scheme-less authority so urlparse reads it as a
    # netloc rather than a path (urlparse("host:80") treats "host" as a scheme).
    if "://" not in s:
        s = "//" + s
    try:
        return urlparse(s).hostname or ""
    except Exception:
        return ""


def _is_blocked_ip(addr: str) -> bool:
    """True when a single resolved IP is one no real camera would use.

    Blocks loopback (127.0.0.0/8, ::1), link-local (169.254.0.0/16, which
    covers the cloud metadata address 169.254.169.254, and fe80::/10), the
    unspecified address (0.0.0.0, ::), and multicast/reserved ranges. It does
    NOT block private LAN ranges (192.168/16, 10/8, 172.16/12): those are where
    real cameras live, so they stay allowed alongside ordinary public hosts.
    """
    a = (addr or "").split("%", 1)[0]  # drop an IPv6 zone id like %eth0
    try:
        ip = ipaddress.ip_address(a)
    except ValueError:
        return True
    # An IPv4 address wrapped as ::ffff:127.0.0.1 must be judged as its IPv4.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_loopback or ip.is_link_local or ip.is_unspecified
                or ip.is_multicast or ip.is_reserved)


def is_blocked_fetch_host(host_or_url: str, fail_closed: bool = True) -> bool:
    """True when a server-side camera fetch to this host/URL must be refused.

    Resolves the hostname to every IP it maps to and blocks the fetch if ANY
    resolved address is disallowed (see ``_is_blocked_ip``), so a name that
    points partly at loopback (a rebinding-style trick) is caught. Ordinary LAN
    cameras and normal public addresses are allowed, so Home Assistant, Reolink,
    Frigate, and manual IP cameras keep working.

    ``fail_closed`` decides what to do with a host that cannot be resolved. The
    arbitrary-URL preview passes True (block it: an unresolvable host is
    unreachable anyway, and refusing is the safe default). A saved camera passes
    False, so a momentary DNS hiccup does not turn a real camera into a blocked
    one: it is only refused when it actively resolves to a disallowed address,
    otherwise the normal "camera unreachable" handling takes over.
    """
    host = _fetch_host(host_or_url)
    if not host:
        return fail_closed
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return fail_closed
    addrs = {info[4][0] for info in infos if info and info[4]}
    if not addrs:
        return fail_closed
    return any(_is_blocked_ip(addr) for addr in addrs)


# --- Reolink (FoodAssistant-qft4) -------------------------------------------
# Reolink cameras (and their doorbells) expose an HTTP still via the CGI API and
# an RTSP live stream. The login is carried in those URLs, so a Reolink entry
# keeps its host, channel, and credentials in app settings (server-side) and the
# URLs are composed at fetch time, the same way an HA camera's token never
# leaves the server. The composed snapshot is always proxied by the app so the
# credentials never reach a browser field or the page source.

def is_reolink(entry: object) -> bool:
    """True for a Reolink camera entry (carries ``source == "reolink"``)."""
    return isinstance(entry, dict) and str(entry.get("source", "")) == "reolink"


def _reolink_authority(host: str, port: object = "") -> str:
    """host[:port] for a Reolink camera, dropping a blank/None port."""
    host = (host or "").strip().rstrip("/")
    # People paste a full address (https://192.168.1.221) into the host field;
    # keep only the host so the composed URL does not become http://https://...
    low = host.lower()
    if low.startswith("https://"):
        host = host[len("https://"):]
    elif low.startswith("http://"):
        host = host[len("http://"):]
    p = str(port or "").strip()
    return f"{host}:{p}" if p else host


def reolink_snapshot_url(host: str, channel: object = 0, username: str = "",
                         password: str = "", port: object = "") -> str:
    """The Reolink CGI still-image URL, with the login embedded.

    Built server-side and only ever fetched server-side (the app proxies it), so
    the credentials stay off the page. Returns "" when no host is given."""
    authority = _reolink_authority(host, port)
    if not authority:
        return ""
    try:
        ch = int(channel)
    except (TypeError, ValueError):
        ch = 0
    url = (f"http://{authority}/cgi-bin/api.cgi?cmd=Snap&channel={ch}"
           "&rs=foodassistant")
    if username:
        url += f"&user={quote(username, safe='')}&password={quote(password or '', safe='')}"
    return url


def reolink_rtsp_url(host: str, channel: object = 0, username: str = "",
                     password: str = "", stream: str = "main") -> str:
    """The Reolink RTSP stream URL, with the login embedded.

    ``stream`` is "main" (full quality) or "sub" (a lighter feed). RTSP is not
    playable in a browser, so this is stored for a client that can consume it
    (the physical Stream Deck, or a future RTSP-to-HLS bridge). Returns "" when
    no host is given."""
    host = (host or "").strip().rstrip("/")
    low = host.lower()
    if low.startswith("https://"):
        host = host[len("https://"):]
    elif low.startswith("http://"):
        host = host[len("http://"):]
    if not host:
        return ""
    try:
        ch = int(channel)
    except (TypeError, ValueError):
        ch = 0
    quality = "sub" if str(stream).lower() == "sub" else "main"
    creds = ""
    if username:
        creds = f"{quote(username, safe='')}:{quote(password or '', safe='')}@"
    # Reolink channels are 1-based in the RTSP path (h264Preview_01_...).
    return f"rtsp://{creds}{host}:554/h264Preview_{ch + 1:02d}_{quality}"


def reolink_snapshot_from_entry(entry: dict) -> str:
    """Compose the credentialed Reolink snapshot URL from a stored entry."""
    if not is_reolink(entry):
        return ""
    return reolink_snapshot_url(
        entry.get("host", ""), entry.get("channel", 0),
        entry.get("username", ""), entry.get("password", ""),
        entry.get("port", ""))


def reolink_rtsp_from_entry(entry: dict) -> str:
    """Compose the credentialed Reolink RTSP URL from a stored entry."""
    if not is_reolink(entry):
        return ""
    return reolink_rtsp_url(
        entry.get("host", ""), entry.get("channel", 0),
        entry.get("username", ""), entry.get("password", ""),
        entry.get("stream_quality", "main"))


# --- Reolink token login (FoodAssistant-t893) -------------------------------
# Newer Reolink firmware rejects the login as inline user/password on the Snap
# CGI. The supported flow is a JSON Login that returns a short-lived token, then
# a Snap that carries only that token. The token and the login never leave the
# server: the app signs in and fetches the image, then hands the browser only
# the JPEG bytes. Tokens are cached per camera so the kiosk poll does not sign
# in on every frame (Reolink limits concurrent logins); a rejected or expired
# token is dropped and a fresh sign-in is tried once.

class ReolinkAuthError(Exception):
    """The camera rejected the username or password."""


# authority ("host" or "host:port") -> (token, expires_epoch). Process-local and
# best-effort, like the other small caches in this codebase.
_reolink_tokens: dict[str, tuple[str, float]] = {}


def _reolink_channel(entry: dict) -> int:
    try:
        return int(entry.get("channel", 0))
    except (TypeError, ValueError):
        return 0


def _looks_like_jpeg(status: int, content: bytes, content_type: str) -> bool:
    """True when a Snap reply is an actual image, not an error page."""
    if status != 200:
        return False
    if (content_type or "").lower().startswith("image/"):
        return True
    return content[:2] == b"\xff\xd8"


def _snap_needs_relogin(status: int, content: bytes, content_type: str) -> bool:
    """True when a Snap reply signals a stale/invalid token, so a fresh sign-in
    is worth one retry: an auth status, or a non-image body that mentions the
    login or token."""
    if status in (401, 403):
        return True
    if _looks_like_jpeg(status, content, content_type):
        return False
    body = content[:400].lower()
    return b"login" in body or b"token" in body


def _cached_reolink_token(authority: str) -> str:
    """A still-valid cached token for this camera, or "" when none/expired."""
    entry = _reolink_tokens.get(authority)
    if not entry:
        return ""
    token, expires = entry
    # Refresh a little early so a token does not expire mid-request.
    if time.time() >= expires - 30:
        _reolink_tokens.pop(authority, None)
        return ""
    return token


async def _reolink_login(client, authority: str, username: str,
                         password: str) -> str:
    """Sign in and return a token, caching it with its lease. Raises
    ReolinkAuthError when the camera rejects the credentials."""
    url = f"http://{authority}/cgi-bin/api.cgi?cmd=Login"
    body = [{"cmd": "Login",
             "param": {"User": {"userName": username, "password": password}}}]
    resp = await client.post(url, json=body)
    if resp.status_code in (401, 403):
        _reolink_tokens.pop(authority, None)
        raise ReolinkAuthError("rejected")
    try:
        data = resp.json()
    except Exception:
        _reolink_tokens.pop(authority, None)
        raise ReolinkAuthError("no token in reply")
    item = data[0] if isinstance(data, list) and data else data
    if not isinstance(item, dict) or item.get("code", 1) != 0 or item.get("error"):
        _reolink_tokens.pop(authority, None)
        raise ReolinkAuthError("rejected")
    token_obj = ((item.get("value") or {}).get("Token") or {})
    token = str(token_obj.get("name") or "")
    if not token:
        _reolink_tokens.pop(authority, None)
        raise ReolinkAuthError("no token in reply")
    try:
        lease = float(token_obj.get("leaseTime", 3600))
    except (TypeError, ValueError):
        lease = 3600.0
    _reolink_tokens[authority] = (token, time.time() + lease)
    return token


async def _reolink_snap(client, authority: str, channel: int, token: str):
    """GET the Snap image with a token. Returns (status, content, content_type)."""
    rs = f"{random.random():.6f}"
    url = (f"http://{authority}/cgi-bin/api.cgi?cmd=Snap&channel={channel}"
           f"&rs={rs}&token={quote(token, safe='')}")
    resp = await client.get(url, follow_redirects=True)
    return resp.status_code, resp.content, resp.headers.get("content-type", "")


async def fetch_reolink_snapshot(entry: dict, timeout: float = 8.0):
    """Sign in (or reuse a cached token) and fetch a Reolink still image.

    Returns ``(status, content, content_type)`` from the Snap request so the
    caller can decide whether the bytes are a usable image. Raises
    ``ReolinkAuthError`` when the camera rejects the login, and lets httpx
    transport errors propagate so the caller can report the camera as
    unreachable. The token and credentials stay server-side.
    """
    import httpx

    authority = _reolink_authority(entry.get("host", ""), entry.get("port", ""))
    if not authority:
        raise ReolinkAuthError("no camera address")
    username = entry.get("username", "") or ""
    password = entry.get("password", "") or ""
    channel = _reolink_channel(entry)

    async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
        token = _cached_reolink_token(authority)
        if not token:
            token = await _reolink_login(client, authority, username, password)
        status, content, ctype = await _reolink_snap(client, authority, channel, token)
        if _snap_needs_relogin(status, content, ctype):
            # The token was stale or wrong; drop it and sign in once more.
            _reolink_tokens.pop(authority, None)
            token = await _reolink_login(client, authority, username, password)
            status, content, ctype = await _reolink_snap(
                client, authority, channel, token)
        return status, content, ctype


async def _reolink_cgi_json(client, authority: str, cmd: str, token: str,
                            channel: int | None = None) -> dict:
    """GET a Reolink CGI command that replies with JSON, with a token."""
    url = f"http://{authority}/cgi-bin/api.cgi?cmd={cmd}&token={quote(token, safe='')}"
    if channel is not None:
        url += f"&channel={channel}"
    resp = await client.get(url, follow_redirects=True)
    try:
        return resp.json()
    except Exception:
        return {}


async def _fetch_reolink_json(entry: dict, cmd: str, with_channel: bool,
                              timeout: float) -> dict:
    """Sign in (or reuse a cached token) and GET one JSON CGI command.

    Shared by the AI-state and device-info fetches below: same sign-in flow as
    ``fetch_reolink_snapshot``, one retry after a fresh login if the reply
    looks like a stale-token rejection. Returns ``{}`` on any failure so a
    best-effort poller never raises for a camera that is briefly unreachable
    or does not support the command (an older Reolink model with no AI
    detection, for example)."""
    import httpx

    authority = _reolink_authority(entry.get("host", ""), entry.get("port", ""))
    if not authority:
        return {}
    username = entry.get("username", "") or ""
    password = entry.get("password", "") or ""
    channel = _reolink_channel(entry) if with_channel else None

    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
            token = _cached_reolink_token(authority)
            if not token:
                token = await _reolink_login(client, authority, username, password)
            data = await _reolink_cgi_json(client, authority, cmd, token, channel)
            if not data:
                # Might be a stale token rather than an unsupported command;
                # one retry with a fresh sign-in before giving up.
                _reolink_tokens.pop(authority, None)
                token = await _reolink_login(client, authority, username, password)
                data = await _reolink_cgi_json(client, authority, cmd, token, channel)
            return data
    except ReolinkAuthError:
        return {}
    except Exception:
        return {}


async def fetch_reolink_ai_state(entry: dict, timeout: float = 6.0) -> dict:
    """Best-effort AI-detection state for a Reolink camera (FoodAssistant-akd0).

    Returns the raw ``GetAiState`` reply for ``camera_detect.reolink_ai_detections``
    to parse, or ``{}`` when the camera is unreachable or does not support AI
    detection (an older model, or one without the AI add-on enabled)."""
    return await _fetch_reolink_json(entry, "GetAiState", with_channel=True,
                                     timeout=timeout)


async def fetch_reolink_dev_info(entry: dict, timeout: float = 6.0) -> dict:
    """Best-effort device info for a Reolink camera (FoodAssistant-qft4).

    Returns the raw ``GetDevInfo`` reply for ``camera_detect.reolink_capabilities``
    to parse (doorbell vs. plain camera, two-way-talk capability), or ``{}``
    when the camera is unreachable."""
    return await _fetch_reolink_json(entry, "GetDevInfo", with_channel=False,
                                     timeout=timeout)


def _entity_and_base_from_url(url: str) -> tuple[str, str]:
    """Recover (entity_id, ha_base) from a stored HA camera_proxy URL, or ("","").

    Handles both ``/api/camera_proxy/<entity>`` and
    ``/api/camera_proxy_stream/<entity>`` so an entry saved before cameras moved
    to entity-based proxying still resolves without the user re-adding it.
    """
    if not url or "/api/camera_proxy" not in url:
        return "", ""
    try:
        parsed = urlparse(url)
    except Exception:
        return "", ""
    parts = parsed.path.split("/")
    # .../api/camera_proxy[_stream]/<entity>
    if "camera_proxy" not in " ".join(parts) and "camera_proxy_stream" not in parts:
        return "", ""
    entity = ""
    for i, seg in enumerate(parts):
        if seg in ("camera_proxy", "camera_proxy_stream") and i + 1 < len(parts):
            entity = parts[i + 1]
            break
    if not entity:
        return "", ""
    base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    return entity, base


def resolve_ha_entity(entry: dict) -> tuple[str, str]:
    """Return (entity_id, ha_base) for an HA-backed camera entry, else ("","").

    Prefers an explicit ``ha_entity`` (with the configured HA base URL); falls
    back to parsing a legacy token-baked URL so old entries keep working.
    """
    if not isinstance(entry, dict):
        return "", ""
    base = (settings.streamdeck_ha_base_url or "").rstrip("/")
    entity = str(entry.get("ha_entity", "")).strip()
    if entity:
        return entity, base
    for key in ("snapshot_url", "stream_url"):
        e, b = _entity_and_base_from_url(str(entry.get(key, "")))
        if e:
            return e, (base or b)
    return "", ""


def ha_feed(entry: dict, kind: str) -> tuple[Optional[str], Optional[dict]]:
    """Upstream URL and auth headers for an HA camera feed, or (None, None).

    ``kind`` is "snapshot" or "stream". Returns (None, None) when the entry is
    not HA-backed or the HA connection is not fully configured, so the caller can
    fall back to a direct URL.
    """
    entity, base = resolve_ha_entity(entry)
    token = settings.streamdeck_ha_token or ""
    if not entity or not base or not token:
        return None, None
    path = _HA_PATHS.get(kind, _HA_PATHS["snapshot"])
    url = f"{base}/api/{path}/{quote(entity, safe='')}"
    return url, {"Authorization": f"Bearer {token}"}


# Query keys that carry a login or a token in a camera URL. Besides these exact
# names, any key containing one of _CREDENTIAL_QUERY_PARTS counts too, which
# covers the vendor spellings (passwd, auth_token, client_secret).
_CREDENTIAL_QUERY_KEYS = frozenset({
    "user", "username", "usr", "password", "pwd", "pass", "pw", "token",
    "access_token", "auth", "key", "apikey", "api_key", "loginuse", "loginpas"})
_CREDENTIAL_QUERY_PARTS = ("pass", "pwd", "token", "secret")
# The authority of a URL the way a browser finds it: after the scheme, any run
# of slashes or backslashes (a browser accepts "http:host", "http:/host" and
# "http:\\host" for http and https), up to the next slash, backslash, ? or #.
_AUTHORITY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[/\\]*([^/\\?#]*)")


def _credential_key(key: str) -> bool:
    k = key.strip().lower()
    return k in _CREDENTIAL_QUERY_KEYS or any(p in k for p in _CREDENTIAL_QUERY_PARTS)


def is_credentialed_url(url) -> bool:
    """True when a camera URL carries a secret a browser must never see.

    That is a login in the address (``http://user:pass@cam/...``), a query key
    that names a login or token (``?user=..&password=..``, ``?token=..``), or a
    Home Assistant ``/api/camera_proxy`` path, which only works with a token.
    Such a feed is fetched by the server instead. An empty value is False; a
    value that cannot be parsed is treated as credentialed, the safe side.
    """
    s = str(url or "").strip()
    # A browser drops tabs and line breaks anywhere in a URL, so do the same
    # before looking for a login.
    s = s.replace("\t", "").replace("\r", "").replace("\n", "")
    if not s:
        return False
    m = _AUTHORITY_RE.match(s)
    if m and "@" in m.group(1):
        return True
    try:
        parts = urlsplit(s)
        if parts.username is not None or parts.password is not None:
            return True
        if "@" in parts.netloc:
            return True
        for k, _v in parse_qsl(parts.query, keep_blank_values=True):
            if _credential_key(k):
                return True
    except ValueError:
        return True
    return "/api/camera_proxy" in s


# Camera fields a satellite may hold. Everything else is left behind: the
# login (username, password), anything that looks like a secret, the Reolink
# address the login belongs to, and any URL that carries a login or token.
_SATELLITE_CAMERA_FIELDS = ("name", "source", "ha_entity", "channel",
                            "stream_quality", "device_type", "two_way_talk",
                            "popup_types")
_SECRET_KEY_PARTS = ("pass", "token", "secret", "key")


def sanitize_camera_for_satellite(entry) -> dict:
    """A camera entry with its credentials removed, for a satellite.

    Keeps the display fields (name, source, ha_entity, channel, stream_quality,
    device_type, two_way_talk, popup_types) and a snapshot or stream URL only
    when it carries no login or token. ``relay`` is True for a Home Assistant
    or Reolink camera and for one whose URL was left out: the satellite asks
    the server for that feed instead of fetching it itself. ``has_stream``
    says whether a live stream exists to relay.
    """
    if not isinstance(entry, dict):
        return {}
    out = {k: entry[k] for k in _SATELLITE_CAMERA_FIELDS
           if k in entry and not any(p in k.lower() for p in _SECRET_KEY_PARTS)}
    blanked = False
    for field in ("snapshot_url", "stream_url"):
        url = str(entry.get(field) or "").strip()
        if not url:
            continue
        if is_credentialed_url(url):
            blanked = True
        else:
            out[field] = url
    entity, _base = resolve_ha_entity(entry)
    is_ha = bool(entity)
    if is_ha and "ha_entity" not in out:
        out["ha_entity"] = entity
    out["relay"] = bool(is_ha or is_reolink(entry) or blanked)
    out["has_stream"] = bool(is_ha or str(entry.get("stream_url") or "").strip())
    return out


def camera_sources(cameras: list) -> list[dict]:
    """View model for the kiosk camera page: per-camera display sources.

    Each item is {name, stream_src, snapshot_src, is_ha}. HA cameras point at the
    app proxy (which adds the bearer header); manual cameras use their direct
    URLs, except a URL that carries a login or token, which points at the app
    proxy instead so the secret never reaches the page. Indexes match
    ``cameras`` so the proxy routes line up.
    """
    out: list[dict] = []
    for idx, cam in enumerate(cameras or []):
        if not isinstance(cam, dict):
            continue
        entity, _base = resolve_ha_entity(cam)
        if entity:
            out.append({
                "name": cam.get("name", "") or entity,
                "stream_src": f"ui/camera/{idx}/stream",
                "snapshot_src": f"ui/camera/{idx}/snapshot",
                "is_ha": True,
            })
        elif is_reolink(cam):
            # The credentialed feed is fetched server-side, so the page only ever
            # sees the app proxy path (no login in any browser-facing field). The
            # live view is the refreshing snapshot; RTSP is not browser-playable.
            out.append({
                "name": cam.get("name", ""),
                "stream_src": "",
                "snapshot_src": f"ui/camera/{idx}/snapshot",
                "is_ha": False,
            })
        else:
            snap = str(cam.get("snapshot_url", "") or "").strip()
            stream = str(cam.get("stream_url", "") or "").strip()
            if is_credentialed_url(snap):
                snap = f"ui/camera/{idx}/snapshot"
            if is_credentialed_url(stream):
                # Relayed by the app. A login-bearing address the server cannot
                # relay (RTSP) is not browser-playable anyway, so it is dropped.
                stream = (f"ui/camera/{idx}/stream"
                          if stream.lower().startswith(("http://", "https://")) else "")
            out.append({
                "name": cam.get("name", ""),
                "stream_src": stream,
                "snapshot_src": snap,
                "is_ha": False,
            })
    return out


def proxied_snapshot(entry: dict) -> tuple[Optional[str], Optional[dict]]:
    """Upstream URL (and optional auth headers) for a feed the app must fetch
    server-side, so its credentials never reach the browser, or (None, None).

    Covers HA cameras (bearer header). A Reolink camera needs a two-step token
    sign-in, so the caller fetches it through ``fetch_reolink_snapshot`` rather
    than a static URL, and it is not resolved here. Manual/Frigate cameras carry
    no secret and are handled by the caller as a plain redirect to their own
    snapshot URL, so they return (None, None)."""
    url, headers = ha_feed(entry, "snapshot")
    if url:
        return url, headers
    # A manual or Frigate camera carries no secret, but its snapshot is still
    # fetched server-side rather than redirected to: an http camera on an https
    # page would otherwise be blocked as mixed content, and the browser may not
    # even be able to reach a camera the server can (FoodAssistant-p1w5).
    direct = (entry.get("snapshot_url") or "").strip() if isinstance(entry, dict) else ""
    if direct.lower().startswith(("http://", "https://")):
        return direct, None
    return None, None


def _blocked_host_sync(host: str) -> bool:
    """Blocking SSRF host check for the one camera fetch that has no guarded
    client of its own (Reolink). Kept module level and positional so it can be
    handed straight to a worker thread; a saved camera fails open on a DNS
    hiccup, exactly as before."""
    return is_blocked_fetch_host(host, fail_closed=False)


async def snapshot_response(entry):
    """The response for GET /ui/camera/{idx}/snapshot.

    Home Assistant cameras are fetched server-side with the bearer token (a
    browser cannot send that header), Reolink cameras sign in for a short-lived
    token, and a manual camera with an http(s) snapshot URL is fetched by the
    server too, so a login in that URL (``user:pass@``, ``?password=``) never
    reaches the browser. Only a clean manual address the server cannot fetch
    itself is still a redirect. Every server-side fetch goes through the
    guarded egress client, which refuses the server's own and internal-only
    addresses.
    """
    from fastapi.responses import JSONResponse, RedirectResponse, Response
    from . import egress

    # A Reolink camera signs in for a short-lived token, then fetches the still
    # with that token; both stay server-side so no login reaches the browser.
    if is_reolink(entry):
        host = entry.get("host", "") if isinstance(entry, dict) else ""
        # The Reolink fetch uses a plain client, so this is its only egress
        # guard. It resolves DNS, which blocks the event loop, so it runs in a
        # worker thread.
        import anyio
        if await anyio.to_thread.run_sync(_blocked_host_sync, host):
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
    # HA (bearer header) and http(s) manual feeds are fetched server-side, so
    # no credential ever reaches the browser.
    url, headers = proxied_snapshot(entry)
    if url:
        # Refuse a saved camera whose address resolves to the server itself or an
        # internal-only address (SSRF, FoodAssistant-e9al). LAN and public
        # camera addresses stay allowed. The guarded client pins the resolved
        # address for the connection so it cannot rebind (FoodAssistant-wrib),
        # which makes it the whole guard: no event-loop DNS lookup first.
        try:
            async with egress.guarded_async_client(allow_private=True, timeout=8.0) as c:
                r = await c.get(url, headers=headers)
        except egress.BlockedHostError:
            return JSONResponse({"detail": BLOCKED_HOST_MESSAGE}, status_code=400)
        except Exception as e:
            return JSONResponse({"detail": f"Camera unreachable: {_redact(e, url)}"},
                                status_code=502)
        if r.status_code != 200:
            return JSONResponse({"detail": f"Camera returned HTTP {r.status_code}."}, status_code=502)
        media = _feed_media_type(r.headers.get("content-type"), "image/jpeg")
        return Response(content=r.content, media_type=media)
    direct = (entry.get("snapshot_url") or "").strip() if isinstance(entry, dict) else ""
    if direct and not is_credentialed_url(direct):
        return RedirectResponse(direct)
    return JSONResponse({"detail": "Camera has no snapshot."}, status_code=404)


async def stream_response(entry):
    """The response for GET /ui/camera/{idx}/stream.

    The upstream multipart stream is relayed as it arrives (never buffered
    whole) so an ``<img>`` keeps updating. Home Assistant cameras carry the
    bearer header server-side, and a manual http(s) stream URL that carries a
    login or token is relayed the same way so the secret stays on the server.
    A clean manual stream is still a redirect to its own address.
    """
    from fastapi.responses import JSONResponse, RedirectResponse
    url, headers = ha_feed(entry, "stream")
    if url:
        return await _relay_stream(url, headers)
    direct = (entry.get("stream_url") or "").strip() if isinstance(entry, dict) else ""
    if direct and is_credentialed_url(direct):
        if direct.lower().startswith(("http://", "https://")):
            return await _relay_stream(direct, None)
        return JSONResponse({"detail": "Camera has no stream."}, status_code=404)
    if direct:
        return RedirectResponse(direct)
    return JSONResponse({"detail": "Camera has no stream."}, status_code=404)


async def _relay_stream(url: str, headers: Optional[dict]):
    """Relay a live camera stream through the guarded egress client."""
    from fastapi.responses import JSONResponse, StreamingResponse
    from starlette.background import BackgroundTask
    from . import egress

    # Never relay a server-side stream from the app's own address or an
    # internal-only one (SSRF, FoodAssistant-e9al). The guarded client pins
    # the resolved address for the connection (FoodAssistant-wrib) and
    # raises below, so no lookup runs on the event loop first.
    client = egress.guarded_async_client(allow_private=True, timeout=None)
    try:
        req = client.build_request("GET", url, headers=headers)
        upstream = await client.send(req, stream=True)
    except egress.BlockedHostError:
        await client.aclose()
        return JSONResponse({"detail": BLOCKED_HOST_MESSAGE}, status_code=400)
    except Exception as e:
        await client.aclose()
        return JSONResponse({"detail": f"Camera unreachable: {_redact(e, url)}"},
                            status_code=502)
    if upstream.status_code != 200:
        await upstream.aclose()
        await client.aclose()
        return JSONResponse({"detail": f"Camera returned HTTP {upstream.status_code}."}, status_code=502)

    async def _close():
        await upstream.aclose()
        await client.aclose()

    media = _feed_media_type(upstream.headers.get("content-type"),
                             "multipart/x-mixed-replace")
    return StreamingResponse(upstream.aiter_raw(), media_type=media,
                             background=BackgroundTask(_close))


# Content types a camera feed may be served back as. A feed is served from the
# app's own address, so a camera (or anything posing as one) that answers with
# a web page or a script must not have it run as part of the app.
_FEED_MEDIA_PREFIXES = ("image/", "video/", "audio/", "multipart/",
                        "application/vnd.apple.mpegurl", "application/x-mpegurl",
                        "application/octet-stream")


def _feed_media_type(ctype, fallback: str) -> str:
    """The upstream content type when it is an image or stream type, else
    ``fallback``. SVG is an image that can carry script, so it is replaced."""
    media = str(ctype or "").strip()
    low = media.lower()
    if low.startswith(_FEED_MEDIA_PREFIXES) and not low.startswith("image/svg"):
        return media
    return fallback


def _redact(err: Exception, url: str) -> str:
    """An error's text with the camera URL (and so any login in it) removed.
    httpx puts the request URL in some messages, which would hand the login
    to the page that asked for the snapshot."""
    text = str(err)
    if url and url in text:
        text = text.replace(url, "the camera address")
    try:
        parts = urlsplit(url or "")
        secrets = [parts.password or ""] + [
            v for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if _credential_key(k)]
    except ValueError:
        secrets = []
    # Both spellings: the address may hold the login percent-encoded while an
    # error message shows it decoded (or the other way round).
    for secret in secrets + [unquote(s) for s in secrets] + [quote(s, safe="") for s in secrets]:
        if secret:
            text = text.replace(secret, "***")
    return text


def resolve_camera_index(cameras: list, cam: str) -> int:
    """Resolve a requested camera selector to a list index, defaulting to 0.

    ``cam`` may be a zero-based index ("0", "2") or a camera name (matched
    case-insensitively against the ``name`` field). An empty selector, an
    out-of-range index, or an unknown name falls back to the first camera (0),
    so the kiosk always shows something. Returns 0 when no cameras exist. Pure,
    so it is unit-testable without the app or a request.
    """
    cams = cameras or []
    if not cams:
        return 0
    want = (cam or "").strip()
    if not want:
        return 0
    # A bare integer selects by position.
    if want.lstrip("-").isdigit():
        idx = int(want)
        if 0 <= idx < len(cams):
            return idx
        return 0
    low = want.lower()
    for i, entry in enumerate(cams):
        if isinstance(entry, dict) and str(entry.get("name", "")).strip().lower() == low:
            return i
    return 0
