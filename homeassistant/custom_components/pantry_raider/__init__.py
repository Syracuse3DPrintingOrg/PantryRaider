"""The Pantry Raider integration.

Sets up the primary install's coordinator, spins up a coordinator per satellite
bandit the install reports, and forwards the entity platforms. Everything runs
on Home Assistant's shared aiohttp session, so there are no extra dependencies
and no blocking I/O.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PantryRaiderClient
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN, PLATFORMS
from .coordinator import PantryRaiderCoordinator
from .helpers import satellite_device_ids

_LOGGER = logging.getLogger(__name__)

type PantryRaiderConfigEntry = ConfigEntry["PantryRaiderRuntimeData"]


@dataclass
class PantryRaiderRuntimeData:
    """Everything the platforms need for one config entry.

    The primary coordinator is always present. Satellite coordinators are added
    lazily as the server reports bandits, keyed by the satellite's device_id so
    a bandit keeps the same coordinator (and entities) across polls.
    """

    entry: PantryRaiderConfigEntry
    main: PantryRaiderCoordinator
    api_key: str | None
    scan_interval: int
    satellite_coordinators: dict[str, PantryRaiderCoordinator] = field(
        default_factory=dict
    )
    satellite_info: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def async_sync_satellites(self, hass: HomeAssistant) -> list[str]:
        """Create coordinators for any newly reported bandits.

        Returns the device_ids added on this call so a platform listener can
        add just the new devices' entities. A bandit that later disappears from
        the server's list keeps its coordinator until the entry reloads; this
        avoids ripping entities out from under automations on a transient blip.
        """

        data = self.main.data or {}
        new_ids: list[str] = []
        by_id = {
            str(sat.get("device_id")): sat
            for sat in data.get("satellites") or []
            if isinstance(sat, dict) and sat.get("device_id")
        }
        for device_id in satellite_device_ids(data):
            sat = by_id.get(device_id) or {}
            # Remember the latest metadata every poll so device_info (hostname,
            # version, ip) stays current even for already-known bandits.
            self.satellite_info[device_id] = sat
            if device_id in self.satellite_coordinators:
                continue
            ip = sat.get("ip")
            if not ip:
                continue
            # A satellite answers /ha/state on plain port 80. It may enforce its
            # own key; the same configured key is tried and a mismatch simply
            # leaves that bandit's entities unavailable (handled in the
            # coordinator), never crashing the entry.
            client = PantryRaiderClient(
                async_get_clientsession(hass),
                f"http://{ip}:80",
                self.api_key,
            )
            coordinator = PantryRaiderCoordinator(
                hass,
                self.entry,
                client,
                self.scan_interval,
                name=f"{DOMAIN} bandit {sat.get('hostname') or device_id}",
                is_satellite=True,
            )
            self.satellite_coordinators[device_id] = coordinator
            new_ids.append(device_id)

        # First refresh for each new bandit. A failure here is fine: the
        # coordinator stays registered and its entities show unavailable until
        # the bandit answers, then recover on their own.
        for device_id in new_ids:
            await self.satellite_coordinators[device_id].async_request_refresh()
        return new_ids


async def async_setup_entry(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry
) -> bool:
    """Set up Pantry Raider from a config entry."""

    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    api_key = entry.data.get(CONF_API_KEY) or None
    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

    client = PantryRaiderClient(
        async_get_clientsession(hass),
        f"http://{host}:{port}",
        api_key,
    )
    coordinator = PantryRaiderCoordinator(
        hass, entry, client, scan_interval, name=f"{DOMAIN} {host}"
    )
    # Fail setup (retry later) if the install is unreachable on first contact,
    # so HA shows the entry as retrying rather than half-built.
    await coordinator.async_config_entry_first_refresh()

    runtime = PantryRaiderRuntimeData(
        entry=entry,
        main=coordinator,
        api_key=api_key,
        scan_interval=scan_interval,
    )
    # Build coordinators for any bandits the server already reports before the
    # platforms load, so their entities appear on the first pass.
    await runtime.async_sync_satellites(hass)
    entry.runtime_data = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # A change to the poll interval in the options flow reloads the entry.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry
) -> bool:
    """Unload a config entry and all its platforms."""

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry
) -> None:
    """Reload when options change (picks up a new scan interval cleanly)."""

    await hass.config_entries.async_reload(entry.entry_id)
