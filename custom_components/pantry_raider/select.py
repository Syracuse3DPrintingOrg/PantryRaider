"""Select entities: screensaver style, and how presence wakes the screen.

Both write to POST /ha/settings using the app's own option keys, while showing
a friendly label in the dropdown.
"""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PantryRaiderConfigEntry
from .const import ATTR_SCREENSAVER_MODE, ATTR_WAKE_ON_PRESENCE
from .coordinator import PantryRaiderCoordinator
from .entity import PantryRaiderEntity
from .helpers import (
    SCREENSAVER_MODE_LABELS,
    WAKE_ON_PRESENCE_LABELS,
    WAKE_ON_PRESENCE_OPTIONS,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PantryRaiderConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up select entities for the primary install and each bandit."""

    from .platform_setup import async_setup_install_platform

    def build(
        coordinator: PantryRaiderCoordinator,
        device_id: str,
        device_info: DeviceInfo,
        is_satellite: bool,
    ) -> Iterable[SelectEntity]:
        entities: list[SelectEntity] = [
            PantryRaiderChoiceSelect(
                coordinator,
                device_info,
                device_id,
                key="screensaver_mode",
                name="Screensaver style",
                icon="mdi:image-multiple",
                state_key="screensaver_mode",
                setting_key=ATTR_SCREENSAVER_MODE,
                labels=SCREENSAVER_MODE_LABELS,
            )
        ]
        # Wake-on-presence only means something where presence is available.
        if (coordinator.data or {}).get("presence", {}).get("available"):
            entities.append(
                PantryRaiderChoiceSelect(
                    coordinator,
                    device_info,
                    device_id,
                    key="wake_on_presence",
                    name="Wake on presence",
                    icon="mdi:motion-sensor",
                    state_key="wake_on_presence",
                    setting_key=ATTR_WAKE_ON_PRESENCE,
                    labels=WAKE_ON_PRESENCE_LABELS,
                    order=WAKE_ON_PRESENCE_OPTIONS,
                )
            )
        return entities

    await async_setup_install_platform(hass, entry, async_add_entities, build)


class PantryRaiderChoiceSelect(PantryRaiderEntity, SelectEntity):
    """A dropdown mapping the app's option keys to friendly labels."""

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
        labels: dict[str, str],
        order: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(coordinator, device_info, device_id)
        self._state_key = state_key
        self._setting_key = setting_key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{device_id}_{key}"
        # Keep a fixed option order; fall back to the label dict's own order.
        keys = order if order is not None else tuple(labels.keys())
        self._label_by_key = {k: labels.get(k, k) for k in keys}
        self._key_by_label = {v: k for k, v in self._label_by_key.items()}
        self._attr_options = list(self._label_by_key.values())

    @property
    def current_option(self):
        raw = (self.coordinator.data or {}).get("display", {}).get(self._state_key)
        # If the app ever reports a value we do not have a label for, show the
        # raw value rather than hide the state.
        return self._label_by_key.get(raw, raw)

    async def async_select_option(self, option: str) -> None:
        key = self._key_by_label.get(option, option)
        await self.coordinator.async_write_settings({self._setting_key: key})
