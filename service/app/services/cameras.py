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

from urllib.parse import quote, urlparse
from typing import Optional

from ..config import settings

# The two HA camera proxy path segments, mapped to the feed kind we want.
_HA_PATHS = {"snapshot": "camera_proxy", "stream": "camera_proxy_stream"}


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


def camera_sources(cameras: list) -> list[dict]:
    """View model for the kiosk camera page: per-camera display sources.

    Each item is {name, stream_src, snapshot_src, is_ha}. HA cameras point at the
    app proxy (which adds the bearer header); manual cameras use their direct
    URLs. Indexes match ``cameras`` so the proxy routes line up.
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
            out.append({
                "name": cam.get("name", ""),
                "stream_src": cam.get("stream_url", "") or "",
                "snapshot_src": cam.get("snapshot_url", "") or "",
                "is_ha": False,
            })
    return out


def proxied_snapshot(entry: dict) -> tuple[Optional[str], Optional[dict]]:
    """Upstream URL (and optional auth headers) for a feed the app must fetch
    server-side, so its credentials never reach the browser, or (None, None).

    Covers HA cameras (bearer header) and Reolink cameras (login in the URL).
    Manual/Frigate cameras carry no secret and are handled by the caller as a
    plain redirect to their own snapshot URL, so they return (None, None)."""
    url, headers = ha_feed(entry, "snapshot")
    if url:
        return url, headers
    if is_reolink(entry):
        composed = reolink_snapshot_from_entry(entry)
        if composed:
            return composed, None
    # A manual or Frigate camera carries no secret, but its snapshot is still
    # fetched server-side rather than redirected to: an http camera on an https
    # page would otherwise be blocked as mixed content, and the browser may not
    # even be able to reach a camera the server can (FoodAssistant-p1w5).
    direct = (entry.get("snapshot_url") or "").strip() if isinstance(entry, dict) else ""
    if direct.startswith(("http://", "https://")):
        return direct, None
    return None, None


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
