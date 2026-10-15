"""Button entities: sleep and wake the install's screen.

Only installs that drive a physical panel get these: a Pi appliance
(pi_hosted), a bandit added directly (pi_remote), and every bandit discovered
under a server. A press posts /ha/display; a soft failure reported by the app
(ok: false, for example off-Pi) is raised as a HomeAssistantError so the user
sees why nothing happened.
"""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PantryRaiderConfigEntry
from .api import PantryRaiderAuthError, PantryRaiderConnectionError
from .coordinator import PantryRaiderCoordinator
from .entity import PantryRaiderEntity
from .helpers import has_display


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PantryRaiderConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sleep/wake buttons for the primary install and each bandit."""

    from .platform_setup import async_setup_install_platform

    def build(
        coordinator: PantryRaiderCoordinator,
        device_id: str,
        device_info: DeviceInfo,
        is_satellite: bool,
    ) -> Iterable[ButtonEntity]:
        mode = (coordinator.data or {}).get("mode")
        # A plain server has no screen of its own, so it gets no screen buttons.
        if not has_display(mode, is_satellite):
            return []
        return [
            PantryRaiderDisplayButton(
                coordinator,
                device_info,
                device_id,
                key="display_sleep",
                name="Sleep screen",
                icon="mdi:monitor-off",
                action="sleep",
            ),
            PantryRaiderDisplayButton(
                coordinator,
                device_info,
                device_id,
                key="display_wake",
                name="Wake screen",
                icon="mdi:monitor",
                action="wake",
            ),
        ]

    await async_setup_install_platform(hass, entry, async_add_entities, build)


class PantryRaiderDisplayButton(PantryRaiderEntity, ButtonEntity):
    """A one-shot press that sleeps or wakes the install's screen."""

    def __init__(
        self,
        coordinator: PantryRaiderCoordinator,
        device_info: DeviceInfo,
        device_id: str,
        *,
        key: str,
        name: str,
        icon: str,
        action: str,
    ) -> None:
        super().__init__(coordinator, device_info, device_id)
        self._action = action
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{device_id}_{key}"

    async def async_press(self) -> None:
        try:
            result = await self.coordinator.client.async_display_action(self._action)
        except PantryRaiderAuthError as err:
            raise HomeAssistantError(
                "Pantry Raider rejected the API key for the screen control."
            ) from err
        except PantryRaiderConnectionError as err:
            # An older install without /ha/display, or one that is unreachable.
            raise HomeAssistantError(
                "This Pantry Raider install did not accept the screen command "
                "(it may be an older version)."
            ) from err
        if not result.get("ok"):
            # The app reached the endpoint but could not act, for example off a
            # Pi where there is no panel. Surface its own reason when it gives
            # one so the user is not left guessing.
            raise HomeAssistantError(
                result.get("detail") or "Pantry Raider could not change the screen."
            )
