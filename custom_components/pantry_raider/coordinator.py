"""Polling coordinators for a Pantry Raider install and its satellites.

The primary install has one coordinator polling its /ha/state. When that install
is a server or appliance and reports satellites, each satellite gets its own
coordinator polling the satellite's own address directly, so a bandit updates on
its own cadence and stays available even if it briefly drops off the server's
list.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    PantryRaiderAuthError,
    PantryRaiderClient,
    PantryRaiderConnectionError,
)

_LOGGER = logging.getLogger(__name__)


class PantryRaiderCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls one install's /ha/state and caches the decoded payload."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: PantryRaiderClient,
        scan_interval: int,
        *,
        name: str,
        is_satellite: bool = False,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=name,
            update_interval=timedelta(seconds=scan_interval),
            config_entry=entry,
        )
        self.client = client
        self._is_satellite = is_satellite
        # A satellite may carry its own password. We log a rejected key once so
        # the log is not spammed every poll while its entities sit unavailable.
        self._auth_warned = False

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self.client.async_get_state()
        except PantryRaiderAuthError as err:
            if self._is_satellite:
                # Do not fail the whole config entry over one bandit's auth;
                # just let its entities go unavailable and say so once.
                if not self._auth_warned:
                    _LOGGER.warning(
                        "Pantry Raider bandit at %s rejected the API key; its "
                        "entities will be unavailable until the key matches",
                        self.client.base_url,
                    )
                    self._auth_warned = True
                raise UpdateFailed("satellite auth rejected") from err
            # For the primary install an auth failure is a real problem the
            # user needs to fix, surfaced as a failed update.
            raise UpdateFailed(f"authentication rejected: {err}") from err
        except PantryRaiderConnectionError as err:
            raise UpdateFailed(str(err)) from err

        # A good poll clears the one-shot auth warning so a later recovery logs
        # cleanly if it happens again.
        self._auth_warned = False
        return data

    async def async_write_settings(self, payload: dict[str, Any]) -> None:
        """Push a settings change, then refresh so entities reflect it fast."""

        await self.client.async_post_settings(payload)
        await self.async_request_refresh()
