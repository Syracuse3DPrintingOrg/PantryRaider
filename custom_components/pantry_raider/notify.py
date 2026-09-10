"""Notify entities: send an on-screen toast to a Pantry Raider screen.

Every install gets one notify entity, the primary and each bandit alike, so
``notify.send_message`` targets exactly the screen the entity belongs to. This
replaces the old rest_command YAML: the message goes to the same
/events/notify endpoint, but through the integration's client and key.
"""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.components.notify import NotifyEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PantryRaiderConfigEntry
from .api import PantryRaiderAuthError, PantryRaiderConnectionError
from .coordinator import PantryRaiderCoordinator
from .entity import PantryRaiderEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PantryRaiderConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one notify entity for the primary install and each bandit."""

    from .platform_setup import async_setup_install_platform

    def build(
        coordinator: PantryRaiderCoordinator,
        device_id: str,
        device_info: DeviceInfo,
        is_satellite: bool,
    ) -> Iterable[NotifyEntity]:
        # Every install accepts /events/notify (a headless server still shows
        # the toast on any open browser tab), so no mode gating here.
        return [PantryRaiderNotifyEntity(coordinator, device_info, device_id)]

    await async_setup_install_platform(hass, entry, async_add_entities, build)


class PantryRaiderNotifyEntity(PantryRaiderEntity, NotifyEntity):
    """Sends a message as an on-screen toast on this install's display."""

    _attr_name = "Notify"
    _attr_icon = "mdi:message-badge-outline"

    def __init__(
        self,
        coordinator: PantryRaiderCoordinator,
        device_info: DeviceInfo,
        device_id: str,
    ) -> None:
        super().__init__(coordinator, device_info, device_id)
        self._attr_unique_id = f"{device_id}_notify"

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        try:
            result = await self.coordinator.client.async_notify(
                message, title=title or ""
            )
        except PantryRaiderAuthError as err:
            raise HomeAssistantError(
                "Pantry Raider rejected the API key for this notification."
            ) from err
        except PantryRaiderConnectionError as err:
            raise HomeAssistantError(
                "Could not reach this Pantry Raider device to show the "
                "notification (it may be offline or an older version)."
            ) from err
        if not result.get("ok"):
            raise HomeAssistantError(
                result.get("error") or "Pantry Raider did not accept the notification."
            )
