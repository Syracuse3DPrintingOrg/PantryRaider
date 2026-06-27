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
        else:
            out.append({
                "name": cam.get("name", ""),
                "stream_src": cam.get("stream_url", "") or "",
                "snapshot_src": cam.get("snapshot_url", "") or "",
                "is_ha": False,
            })
    return out
