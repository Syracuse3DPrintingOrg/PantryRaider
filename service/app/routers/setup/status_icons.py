"""Settings, Home Assistant, Status icons: the entity picker.

The items themselves are saved through /setup/save (status_icons,
status_icons_enabled, status_icons_position). This lists the Home Assistant
entities an item can watch: doors and other binary sensors, switches and
plugs, climate and water heaters, fans, and power sensors.
"""
from __future__ import annotations

from fastapi.responses import JSONResponse

from ._common import router, settings
from .cameras import _HA_ON_SERVER_MSG, _ha_relayed_here


@router.get("/status-icons/entities")
async def status_icon_entities():
    """{ok, configured, entities: [{entity_id, name, domain, device_class,
    state, unit, match}]}, sorted by name. A satellite answers 409: its
    status icons are set up on the main server."""
    if settings.is_satellite() or _ha_relayed_here():
        return JSONResponse({"ok": False, "error": _HA_ON_SERVER_MSG}, status_code=409)
    from ...services import ha_client, status_icons
    if ha_client.mode() != "direct":
        return {"ok": True, "configured": False, "entities": []}
    try:
        rows = await ha_client.list_states()
    except Exception:  # noqa: BLE001 - an unreachable Home Assistant lists nothing
        rows = []
    entities = [e for e in (status_icons.picker_entity(r) for r in rows) if e]
    entities.sort(key=lambda e: (e["domain"] != "binary_sensor", e["name"].lower()))
    return {"ok": True, "configured": True, "entities": entities}
