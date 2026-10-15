"""Number entities: display sleep and screensaver timeouts (in minutes).

Both write their setting back to the install through POST /ha/settings and then
refresh the coordinator so the new value shows without waiting for the next
poll.
"""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PantryRaiderConfigEntry
from .const import ATTR_DISPLAY_IDLE_TIMEOUT, ATTR_SCREENSAVER_MINUTES
from .coordinator import PantryRaiderCoordinator
from .entity import PantryRaiderEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PantryRaiderConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities for the primary install and each bandit."""

    from .platform_setup import async_setup_install_platform

    def build(
        coordinator: PantryRaiderCoordinator,
        device_id: str,
        device_info: DeviceInfo,
        is_satellite: bool,
    ) -> Iterable[NumberEntity]:
        # Every mode has a display block, so both numbers apply everywhere.
        return [
            PantryRaiderMinutesNumber(
                coordinator,
                device_info,
                device_id,
                key="display_sleep",
                name="Display sleep",
                icon="mdi:monitor-off",
                state_key="idle_timeout",
                setting_key=ATTR_DISPLAY_IDLE_TIMEOUT,
            ),
            PantryRaiderMinutesNumber(
                coordinator,
                device_info,
                device_id,
                key="screensaver_minutes",
                name="Screensaver delay",
                icon="mdi:monitor-screenshot",
                state_key="screensaver_minutes",
                setting_key=ATTR_SCREENSAVER_MINUTES,
            ),
        ]

    await async_setup_install_platform(hass, entry, async_add_entities, build)


class PantryRaiderMinutesNumber(PantryRaiderEntity, NumberEntity):
    """A 0-120 minute display setting backed by POST /ha/settings."""

    _attr_native_min_value = 0
    _attr_native_max_value = 120
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: PantryRaiderCoordinator,
        device_info: DeviceInfo,
        device_id: str,
        *,
        key: str,
        name: str,
        icon: str,
        state_key: str,
        setting_key: str,
    ) -> None:
        super().__init__(coordinator, device_info, device_id)
        self._state_key = state_key
        self._setting_key = setting_key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{device_id}_{key}"

    @property
    def native_value(self):
        value = (self.coordinator.data or {}).get("display", {}).get(self._state_key)
        return None if value is None else float(value)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_write_settings(
            {self._setting_key: int(value)}
        )
