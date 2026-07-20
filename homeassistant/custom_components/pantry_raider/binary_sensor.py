"""Binary sensors: kiosk presence, and an expiring-attention problem flag."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PantryRaiderConfigEntry
from .coordinator import PantryRaiderCoordinator
from .entity import PantryRaiderEntity
from .helpers import expiring_attention, is_server_mode


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PantryRaiderConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors for the primary install and each bandit."""

    from .platform_setup import async_setup_install_platform

    def build(
        coordinator: PantryRaiderCoordinator,
        device_id: str,
        device_info: DeviceInfo,
        is_satellite: bool,
    ) -> Iterable[BinarySensorEntity]:
        data = coordinator.data or {}
        entities: list[BinarySensorEntity] = []
        # Presence only makes sense on a device that actually reports it; the
        # app flags this per install with presence.available.
        if (data.get("presence") or {}).get("available"):
            entities.append(
                PantryRaiderPresence(coordinator, device_info, device_id)
            )
        # The attention flag is a full-stack notion (it needs inventory).
        if is_server_mode(data.get("mode")):
            entities.append(
                PantryRaiderExpiringProblem(coordinator, device_info, device_id)
            )
        return entities

    await async_setup_install_platform(hass, entry, async_add_entities, build)


class PantryRaiderPresence(PantryRaiderEntity, BinarySensorEntity):
    """Occupancy: whether the kiosk currently detects someone nearby."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_name = "Presence"

    def __init__(self, coordinator, device_info, device_id) -> None:
        super().__init__(coordinator, device_info, device_id)
        self._attr_unique_id = f"{device_id}_presence"

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get("presence", {}).get("detected"))


class PantryRaiderExpiringProblem(PantryRaiderEntity, BinarySensorEntity):
    """Problem flag: on when something is expired or expires today."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_name = "Expiring attention"
    _attr_icon = "mdi:clock-alert-outline"

    def __init__(self, coordinator, device_info, device_id) -> None:
        super().__init__(coordinator, device_info, device_id)
        self._attr_unique_id = f"{device_id}_expiring_attention"

    @property
    def is_on(self) -> bool:
        return expiring_attention(self.coordinator.data)
