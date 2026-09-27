"""Home Assistant entity discovery for custom action keys (FoodAssistant-yzjr).

The Stream Deck / Start Page custom-key builder lets a user bind a key to a
Home Assistant entity ("ha_action" overrides). Typing raw entity ids is
error-prone, so this module fetches the entities the user can actually act on
from GET {base}/api/states (the same connection gadgets_ha uses:
streamdeck_ha_base_url / streamdeck_ha_token) and shapes them for a picker:
id, friendly name, domain, and current state, grouped by domain, with a
sensible default service per domain so a picked entity works with one press.

Only actionable domains are offered (things a key press can call a service
on); sensors and other read-only domains are filtered out. The parsing,
filtering, grouping, and domain-to-service helpers are pure so they test
without a network; only list_actionable_entities touches HTTP.
"""
from __future__ import annotations

import httpx

# Domains a custom key can usefully act on. Read-only domains (sensor,
# binary_sensor, weather, ...) are excluded: there is no service a key press
# could call on them.
ACTIONABLE_DOMAINS: tuple[str, ...] = (
    "light", "switch", "scene", "script", "fan", "cover", "media_player",
    "input_boolean", "automation", "button", "climate", "lock", "vacuum",
)

# The service a freshly picked entity defaults to, per domain. Stateful
# on/off things toggle (homeassistant.toggle works across those domains);
# one-shot things run their own trigger service.
DEFAULT_SERVICE_BY_DOMAIN: dict[str, str] = {
    "light": "homeassistant.toggle",
    "switch": "homeassistant.toggle",
    "fan": "homeassistant.toggle",
    "input_boolean": "homeassistant.toggle",
    "media_player": "homeassistant.toggle",
    "climate": "homeassistant.toggle",
    "scene": "scene.turn_on",
    "script": "script.turn_on",
    "button": "button.press",
    "automation": "automation.trigger",
    "cover": "cover.toggle",
    "lock": "lock.lock",
    "vacuum": "vacuum.start",
}

# The common services offered in the picker per domain, the default first.
# The service field stays free text, so anything not listed can still be
# typed (e.g. a script by full name).
COMMON_SERVICES_BY_DOMAIN: dict[str, list[str]] = {
    "light": ["homeassistant.toggle", "light.turn_on", "light.turn_off"],
    "switch": ["homeassistant.toggle", "switch.turn_on", "switch.turn_off"],
    "fan": ["homeassistant.toggle", "fan.turn_on", "fan.turn_off"],
    "input_boolean": ["homeassistant.toggle", "input_boolean.turn_on",
                      "input_boolean.turn_off"],
    "media_player": ["homeassistant.toggle", "media_player.media_play_pause",
                     "media_player.turn_on", "media_player.turn_off"],
    "climate": ["homeassistant.toggle", "climate.turn_on", "climate.turn_off"],
    "scene": ["scene.turn_on"],
    "script": ["script.turn_on"],
    "button": ["button.press"],
    "automation": ["automation.trigger", "automation.turn_on",
                   "automation.turn_off"],
    "cover": ["cover.toggle", "cover.open_cover", "cover.close_cover",
              "cover.stop_cover"],
    "lock": ["lock.lock", "lock.unlock"],
    "vacuum": ["vacuum.start", "vacuum.return_to_base", "vacuum.stop"],
}


def entity_domain(entity_id: str) -> str:
    """The domain part of an entity id ("light.kitchen" -> "light")."""
    return str(entity_id or "").strip().split(".", 1)[0].lower()


def default_service(entity_id: str) -> str:
    """The service a key bound to this entity should call by default.

    Pure. Falls back to homeassistant.toggle for anything unmapped, matching
    resolve_ha_call's own no-service default in start_actions."""
    return DEFAULT_SERVICE_BY_DOMAIN.get(entity_domain(entity_id),
                                         "homeassistant.toggle")


def actionable_entity(row: dict) -> dict | None:
    """One GET /api/states row as a picker entry, or None when not actionable.

    Pure. Keeps only entities in ACTIONABLE_DOMAINS and returns the fields the
    picker shows: entity_id, friendly name, domain, current state, plus the
    default service so the builder can prefill it."""
    if not isinstance(row, dict):
        return None
    entity_id = str(row.get("entity_id") or "").strip()
    domain = entity_domain(entity_id)
    if "." not in entity_id or domain not in ACTIONABLE_DOMAINS:
        return None
    attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
    return {
        "entity_id": entity_id,
        "name": str(attrs.get("friendly_name") or entity_id)[:60],
        "domain": domain,
        "state": str(row.get("state") if row.get("state") is not None else ""),
        "default_service": default_service(entity_id),
    }


def group_by_domain(entities: list[dict]) -> list[dict]:
    """Group picker entries by domain, HA-service-picker style.

    Pure. Returns [{"domain", "services", "entities"}] with domains in
    ACTIONABLE_DOMAINS order and each domain's entities sorted by name."""
    by_domain: dict[str, list[dict]] = {}
    for e in entities:
        if isinstance(e, dict) and e.get("domain"):
            by_domain.setdefault(e["domain"], []).append(e)
    out = []
    for domain in ACTIONABLE_DOMAINS:
        rows = by_domain.get(domain)
        if not rows:
            continue
        rows.sort(key=lambda e: str(e.get("name") or "").lower())
        out.append({
            "domain": domain,
            "services": COMMON_SERVICES_BY_DOMAIN.get(domain, []),
            "entities": rows,
        })
    return out


async def list_actionable_entities(client: httpx.AsyncClient | None = None) -> list[dict]:
    """Actionable entities from Home Assistant, ungrouped.

    Returns [] when HA is unconfigured or unreachable, so the picker degrades
    to the plain free-text fields. Same connection and pattern as
    gadgets_ha.list_temperature_entities."""
    from .gadgets_ha import ha_connection
    base, token = ha_connection()
    if not (base and token):
        return []
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=8.0)
    try:
        r = await client.get(f"{base}/api/states",
                             headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            return []
        rows = r.json()
    except Exception:  # noqa: BLE001 - unreachable HA degrades, never raises
        return []
    finally:
        if own_client:
            await client.aclose()
    out = []
    for row in rows if isinstance(rows, list) else []:
        entry = actionable_entity(row)
        if entry:
            out.append(entry)
    return out
