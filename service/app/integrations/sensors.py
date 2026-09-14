"""SensorDecoder inhabitants: the gadget device families (FoodAssistant-pjtq).

POST /gadgets/readings carries entries for several device families, told
apart by a ``kind`` token. The routing used to be an if-chain inside
services.gadgets.ingest; each branch now lives on the family's decoder here,
and ingest walks the registry instead, so adding a family means registering
a decoder. The wire behavior is bit-for-bit what the if-chain did: the same
normalize functions (still in services.gadgets / services.stemma, still
pure), the same state-file buckets, the same prune windows, and the same
fallback (an entry with no or an unknown kind is a thermometer, the original
wire contract).

Two families are deliberately thin here:

  * Buttons ("button" entries) store nothing in the gadgets state; their
    presses are handled, complete with mapping execution and cooldowns, by
    services.gadgets_buttons.handle_payload from the readings route. The
    decoder exists so the registry lists the family and ingest's skip stays
    a registered fact rather than a stray branch.
  * STEMMA heartbeats carry no reading of their own; the decoder stores the
    heartbeat and the bus sweep's finds through services.stemma's
    normalizers, unchanged.

``enabled()`` mirrors each family's settings gate for status surfaces only.
Ingest stores whatever the reader pushes regardless, exactly as it always
has: the gates throttle the reader and the UI, not the ingest.
"""
from __future__ import annotations

from .interfaces import KIND_SENSORS, SensorDecoder


class ThermometerDecoder(SensorDecoder):
    """Cooking probes: the original family and the wire fallback."""

    name = "thermometer"
    label = "Cooking probes"
    wire_kind = "thermometer"
    fallback = True

    def enabled(self) -> bool:
        from ..config import settings
        return bool(settings.gadgets_enabled)

    def configured_ids(self) -> set[str]:
        from ..services import gadgets
        return {gadgets._norm_id(d.get("id")) for d in gadgets.configured_devices()}

    def store_reading(self, state: dict, entry, now: float, source: str) -> None:
        from ..services import gadgets
        reading = gadgets.normalize_reading(entry, now)
        if reading:
            if source:
                reading["via"] = source
            state["devices"][reading["id"]] = reading

    def store_discovered(self, state: dict, entry: dict, dev_id: str,
                         protocol: str, now: float, source: str,
                         configured_ids: set[str]) -> None:
        from ..services import gadgets
        if dev_id in configured_ids:
            return
        state["discovered"][dev_id] = {
            "id": dev_id,
            "name": str(entry.get("name") or "")[:60],
            "protocol": protocol if protocol in gadgets.PROTOCOLS else "",
            "rssi": entry.get("rssi") if isinstance(entry.get("rssi"), int) else None,
            # supported=False marks a probe-looking device we have no
            # decoder for (shown as "seen nearby, not supported yet").
            "supported": entry.get("supported") is not False,
            "ts": now,
        }
        if source:
            state["discovered"][dev_id]["via"] = source

    def prune(self, state: dict, now: float, configured_ids: set[str]) -> None:
        from ..services import gadgets
        state["devices"] = {k: v for k, v in state["devices"].items()
                            if now - v.get("ts", 0) <= gadgets.PRUNE_SECONDS}
        state["discovered"] = {k: v for k, v in state["discovered"].items()
                               if now - v.get("ts", 0) <= gadgets.DISCOVERED_TTL
                               and k not in configured_ids}


class HygrometerDecoder(SensorDecoder):
    """Ambient temperature + humidity sensors (fridge, freezer, pantry)."""

    name = "hygrometer"
    label = "Hygrometers"
    wire_kind = "hygrometer"

    def enabled(self) -> bool:
        from ..config import settings
        return bool(settings.hygrometers_enabled)

    def configured_ids(self) -> set[str]:
        from ..services import gadgets
        return {gadgets._norm_id(d.get("id"))
                for d in gadgets.configured_hygrometers()}

    def store_reading(self, state: dict, entry, now: float, source: str) -> None:
        from ..services import gadgets
        reading = gadgets.normalize_hygro_reading(entry, now)
        if reading:
            if source:
                reading["via"] = source
            state.setdefault("hygrometers", {})[reading["id"]] = reading

    def store_discovered(self, state: dict, entry: dict, dev_id: str,
                         protocol: str, now: float, source: str,
                         configured_ids: set[str]) -> None:
        from ..services import gadgets
        if dev_id in configured_ids:
            return
        seen = state.setdefault("hygro_discovered", {})
        seen[dev_id] = {
            "id": dev_id,
            "name": str(entry.get("name") or "")[:60],
            "protocol": protocol if protocol in gadgets.HYGRO_PROTOCOLS else "",
            "rssi": entry.get("rssi") if isinstance(entry.get("rssi"), int) else None,
            "ts": now,
        }
        if source:
            seen[dev_id]["via"] = source

    def prune(self, state: dict, now: float, configured_ids: set[str]) -> None:
        from ..services import gadgets
        state["hygrometers"] = {k: v for k, v in state.get("hygrometers", {}).items()
                                if now - v.get("ts", 0) <= gadgets.PRUNE_SECONDS}
        state["hygro_discovered"] = {k: v for k, v in state.get("hygro_discovered", {}).items()
                                     if now - v.get("ts", 0) <= gadgets.DISCOVERED_TTL
                                     and k not in configured_ids}


class ContactDecoder(SensorDecoder):
    """Door/window contact sensors, with the open-since bookkeeping the
    left-open alarm measures from."""

    name = "contact"
    label = "Door and window sensors"
    wire_kind = "contact"

    def enabled(self) -> bool:
        from ..config import settings
        return bool(settings.contacts_enabled)

    def configured_ids(self) -> set[str]:
        from ..services import gadgets
        return {gadgets._norm_id(d.get("id")) for d in gadgets.configured_contacts()}

    def store_reading(self, state: dict, entry, now: float, source: str) -> None:
        from ..services import gadgets
        reading = gadgets.normalize_contact_reading(entry, now)
        if not reading:
            return
        if source:
            reading["via"] = source
        contacts = state.setdefault("contacts", {})
        # Track when the door first went open so the left-open alarm
        # measures from the real opening, not from whichever sweep noticed
        # it.
        prev = contacts.get(reading["id"]) or {}
        if reading["open"]:
            reading["open_since"] = (prev.get("open_since")
                                     if prev.get("open") else now) or now
        else:
            reading["open_since"] = None
        contacts[reading["id"]] = reading

    def store_discovered(self, state: dict, entry: dict, dev_id: str,
                         protocol: str, now: float, source: str,
                         configured_ids: set[str]) -> None:
        from ..services import gadgets
        if dev_id in configured_ids:
            return
        seen = state.setdefault("contact_discovered", {})
        seen[dev_id] = {
            "id": dev_id,
            "name": str(entry.get("name") or "")[:60],
            "protocol": protocol if protocol in gadgets.CONTACT_PROTOCOLS else "",
            "rssi": entry.get("rssi") if isinstance(entry.get("rssi"), int) else None,
            "ts": now,
        }
        if source:
            seen[dev_id]["via"] = source

    def prune(self, state: dict, now: float, configured_ids: set[str]) -> None:
        from ..services import gadgets
        # Contacts prune on a longer window than probes: a door that sits
        # still broadcasts rarely, and dropping it would forget open_since.
        window = max(gadgets.PRUNE_SECONDS, 2 * gadgets.CONTACT_STALE_SECONDS)
        state["contacts"] = {k: v for k, v in state.get("contacts", {}).items()
                             if now - v.get("ts", 0) <= window}
        state["contact_discovered"] = {k: v for k, v in state.get("contact_discovered", {}).items()
                                       if now - v.get("ts", 0) <= gadgets.DISCOVERED_TTL
                                       and k not in configured_ids}


class ButtonDecoder(SensorDecoder):
    """BLE shelf buttons. Presses never land in the gadgets state: the
    readings route hands the same payload to
    services.gadgets_buttons.handle_payload, which owns the press state,
    cooldowns, and mapping execution. This decoder registers the family and
    keeps ingest's pass-through explicit."""

    name = "button"
    label = "Shelf buttons"
    wire_kind = "button"

    def enabled(self) -> bool:
        from ..config import settings
        return bool(settings.buttons_enabled)

    def configured_ids(self) -> set[str]:
        from ..services import gadgets, gadgets_buttons
        return {gadgets._norm_id(d.get("id"))
                for d in gadgets_buttons.configured_buttons()}

    def store_reading(self, state: dict, entry, now: float, source: str) -> None:
        return  # handled by gadgets_buttons.handle_payload from the route

    def store_discovered(self, state: dict, entry: dict, dev_id: str,
                         protocol: str, now: float, source: str,
                         configured_ids: set[str]) -> None:
        return  # handled by gadgets_buttons.handle_payload from the route

    def prune(self, state: dict, now: float, configured_ids: set[str]) -> None:
        return  # gadgets_buttons owns its own state and windows


class StemmaDecoder(SensorDecoder):
    """Plug-in STEMMA QT / Qwiic accessories: bus-powered boards whose push
    is a heartbeat (plugged in / unplugged), not a reading."""

    name = "stemma"
    label = "STEMMA QT accessories"
    wire_kind = "stemma"

    def enabled(self) -> bool:
        from ..config import settings
        return bool(settings.stemma_enabled)

    def configured_ids(self) -> set[str]:
        from ..services import stemma
        return {stemma.norm_id(d.get("id")) for d in stemma.configured_devices()}

    def store_reading(self, state: dict, entry, now: float, source: str) -> None:
        from ..services import stemma
        beat = stemma.normalize_heartbeat(entry, now)
        if beat:
            state.setdefault("stemma", {})[beat["id"]] = beat

    def store_discovered(self, state: dict, entry: dict, dev_id: str,
                         protocol: str, now: float, source: str,
                         configured_ids: set[str]) -> None:
        # Ids are bus-plus-address, not MACs, so they keep their own case
        # rules (services.stemma.norm_id); a board we can see but cannot
        # drive rides along with supported=False, the same courtesy the BLE
        # scan extends.
        from ..services import stemma
        found = stemma.normalize_discovered(entry, now)
        if found and found["id"] not in configured_ids:
            state.setdefault("stemma_discovered", {})[found["id"]] = found

    def prune(self, state: dict, now: float, configured_ids: set[str]) -> None:
        from ..services import gadgets
        state["stemma"] = {k: v for k, v in state.get("stemma", {}).items()
                           if now - v.get("ts", 0) <= gadgets.PRUNE_SECONDS}
        state["stemma_discovered"] = {k: v for k, v in state.get("stemma_discovered", {}).items()
                                      if now - v.get("ts", 0) <= gadgets.DISCOVERED_TTL
                                      and k not in configured_ids}


def ingest_decoders() -> list[SensorDecoder]:
    """Every registered decoder, in registration order, for ingest to route
    with. Deliberately NOT filtered by enabled(): ingest has always stored
    whatever the reader pushes, and a decoder registered here takes part in
    the wire routing outright."""
    from . import registry
    return [d for d in registry.all_for(KIND_SENSORS)
            if isinstance(d, SensorDecoder)]


def fallback_decoder(decoders: list[SensorDecoder]) -> SensorDecoder:
    """The decoder entries with no or an unknown kind route to (the
    thermometer, per the original wire contract)."""
    for decoder in decoders:
        if decoder.fallback:
            return decoder
    raise LookupError("No fallback sensor decoder is registered.")


def register_builtins() -> None:
    """Register the five device families (called by the registry)."""
    from . import registry
    for decoder in (ThermometerDecoder(), HygrometerDecoder(), ContactDecoder(),
                    ButtonDecoder(), StemmaDecoder()):
        registry.register(decoder, replace=True)
