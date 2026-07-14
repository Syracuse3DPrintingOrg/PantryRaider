"""Sensor entities for a Pantry Raider install and its bandits.

Static sensors (counts, timers, version, printer queue) come straight from the
payload. Thermometer probes are dynamic: each probe becomes its own temperature
sensor, and probes that appear after setup are added through a coordinator
listener so a thermometer paired mid-session shows up without a reload.
"""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PantryRaiderConfigEntry
from .coordinator import PantryRaiderCoordinator
from .entity import PantryRaiderEntity
from .helpers import (
    applicable_sensor_keys,
    iter_probes,
    probe_display_name,
    probe_unique_id,
    sensor_value,
)

# name, icon, unit, is_diagnostic for each static sensor key.
_SENSOR_META: dict[str, tuple[str, str, str | None, bool]] = {
    "expired": ("Expired", "mdi:food-off", "items", False),
    "today": ("Expiring today", "mdi:food-variant-off", "items", False),
    "within_3_days": ("Expiring within 3 days", "mdi:food-drumstick", "items", False),
    "within_7_days": ("Expiring within 7 days", "mdi:food-apple", "items", False),
    "pending": ("Pending scans", "mdi:barcode-scan", "items", False),
    "action_items": ("Action items", "mdi:inbox-outline", "items", False),
    "timers_running": ("Timers running", "mdi:timer-outline", "timers", False),
    "next_timer": ("Next timer", "mdi:timer-sand", None, False),
    "label_queue": ("Label printer queue", "mdi:printer", None, False),
    "version": ("App version", "mdi:information-outline", None, True),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PantryRaiderConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors for the primary install and each bandit."""

    from .platform_setup import async_setup_install_platform

    def build(
        coordinator: PantryRaiderCoordinator,
        device_id: str,
        device_info: DeviceInfo,
        is_satellite: bool,
    ) -> Iterable[SensorEntity]:
        entities: list[SensorEntity] = [
            PantryRaiderSensor(coordinator, device_info, device_id, key)
            for key in applicable_sensor_keys(coordinator.data)
        ]
        # Only full-stack installs report thermometers, so only they need the
        # dynamic probe manager. A bandit never has probes.
        if not is_satellite:
            entities.extend(
                _build_probe_sensors(
                    coordinator, device_info, device_id, set()
                )
            )
            _register_probe_listener(coordinator, device_info, device_id, async_add_entities)
        return entities

    await async_setup_install_platform(hass, entry, async_add_entities, build)


def _build_probe_sensors(
    coordinator: PantryRaiderCoordinator,
    device_info: DeviceInfo,
    device_id: str,
    already: set[str],
) -> list["PantryRaiderProbeSensor"]:
    """Probe sensors present in the payload that are not already added."""

    new: list[PantryRaiderProbeSensor] = []
    for thermo, probe in iter_probes(coordinator.data):
        thermo_id = thermo.get("id")
        index = probe.get("index")
        uid = probe_unique_id(device_id, thermo_id, index)
        if uid in already:
            continue
        already.add(uid)
        new.append(
            PantryRaiderProbeSensor(
                coordinator, device_info, device_id, thermo_id, index
            )
        )
    return new


def _register_probe_listener(
    coordinator: PantryRaiderCoordinator,
    device_info: DeviceInfo,
    device_id: str,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add probe sensors that appear after setup, once each."""

    seen: set[str] = {
        probe_unique_id(device_id, thermo.get("id"), probe.get("index"))
        for thermo, probe in iter_probes(coordinator.data)
    }

    @callback
    def _check_for_new_probes() -> None:
        new = _build_probe_sensors(coordinator, device_info, device_id, seen)
        if new:
            async_add_entities(new)

    coordinator.async_add_listener(_check_for_new_probes)


class PantryRaiderSensor(PantryRaiderEntity, SensorEntity):
    """One static sensor keyed to a field in the /ha/state payload."""

    def __init__(
        self,
        coordinator: PantryRaiderCoordinator,
        device_info: DeviceInfo,
        device_id: str,
        key: str,
    ) -> None:
        super().__init__(coordinator, device_info, device_id)
        self._key = key
        name, icon, unit, diagnostic = _SENSOR_META[key]
        self._attr_name = name
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit
        self._attr_unique_id = f"{device_id}_{key}"
        if diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        # Plain counts are measurements; text states (version, next timer,
        # printer queue) carry no state class.
        if unit == "items" or unit == "timers":
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        return sensor_value(self._key, self.coordinator.data)

    @property
    def extra_state_attributes(self):
        # The next-timer sensor exposes its parts so a card can show a live
        # countdown without re-parsing the combined state string.
        if self._key != "next_timer":
            return None
        nxt = ((self.coordinator.data or {}).get("timers") or {}).get("next")
        if not isinstance(nxt, dict):
            return {"label": None, "remaining_seconds": None}
        return {
            "label": nxt.get("label"),
            "remaining_seconds": nxt.get("remaining_seconds"),
        }


class PantryRaiderProbeSensor(PantryRaiderEntity, SensorEntity):
    """One thermometer probe rendered as a temperature sensor."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: PantryRaiderCoordinator,
        device_info: DeviceInfo,
        device_id: str,
        thermo_id,
        index,
    ) -> None:
        super().__init__(coordinator, device_info, device_id)
        self._thermo_id = thermo_id
        self._index = index
        self._attr_unique_id = probe_unique_id(device_id, thermo_id, index)

    def _find_probe(self) -> tuple[dict | None, dict | None]:
        for thermo, probe in iter_probes(self.coordinator.data):
            if thermo.get("id") == self._thermo_id and probe.get("index") == self._index:
                return thermo, probe
        return None, None

    @property
    def name(self):
        thermo, probe = self._find_probe()
        if thermo is None:
            return f"Probe {self._index}"
        return probe_display_name(thermo.get("name"), (probe or {}).get("role_label"))

    @property
    def native_value(self):
        _, probe = self._find_probe()
        if probe is None:
            return None
        return probe.get("temp_c")

    @property
    def available(self) -> bool:
        # A stale thermometer (dead battery, out of range) reports stale=True;
        # its probes should read unavailable rather than show a frozen value.
        if not super().available:
            return False
        thermo, probe = self._find_probe()
        if thermo is None or probe is None:
            return False
        return not thermo.get("stale", False)

    @property
    def extra_state_attributes(self):
        _, probe = self._find_probe()
        if probe is None:
            return None
        return {
            "role": probe.get("role"),
            "target_c": probe.get("target_c"),
        }
