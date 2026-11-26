"""GET /status-icons: the status icons a kitchen display shows right now.

One cheap call for every display poll: {enabled, position, items}, where
items lists only the icons whose condition is true, in the saved order, each
as {id, name, icon, detail}. The answer is cached for a few seconds
(services/status_icons.py) and the route never fails: anything it cannot
read is left out.

On a Bandit (pi_remote) the answer is the main server's. The server holds
the Home Assistant connection and the thermometer readings, so the Bandit
asks it (satellite_upstream_get, with the same short cache as the other
forwarded polls) and shows nothing when the server does not answer, rather
than a list it cannot check.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from ..config import settings
from ..services import status_icons

log = logging.getLogger(__name__)

router = APIRouter(tags=["status-icons"])


def _empty() -> dict:
    return {"enabled": bool(getattr(settings, "status_icons_enabled", False)),
            "position": status_icons.normalize_position(
                getattr(settings, "status_icons_position", "")),
            "items": []}


def _clean_upstream(data: dict) -> dict:
    """The server's answer, reduced to the fields a display reads."""
    items = []
    for raw in data.get("items") or []:
        if not isinstance(raw, dict):
            continue
        icon = str(raw.get("icon") or "")
        items.append({
            "id": str(raw.get("id") or "")[:16],
            "name": str(raw.get("name") or "")[:status_icons.NAME_MAX],
            "icon": icon if icon in status_icons.ICONS else "generic",
            "detail": str(raw.get("detail") or "")[:40],
        })
    return {"enabled": bool(data.get("enabled")),
            "position": status_icons.normalize_position(data.get("position")),
            "items": items[:status_icons.MAX_ITEMS]}


@router.get("/status-icons")
async def get_status_icons():
    try:
        if settings.is_satellite():
            if not settings.upstream_linked():
                return _empty()
            from ..main import satellite_upstream_get
            data = await satellite_upstream_get("/status-icons")
            return _clean_upstream(data) if isinstance(data, dict) else _empty()
        return await status_icons.active_items()
    except Exception:  # noqa: BLE001 - a display poll never fails
        log.debug("status icons poll failed", exc_info=True)
        return {"enabled": False, "position": status_icons.DEFAULT_POSITION, "items": []}
