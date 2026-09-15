"""Shared base entity and device_info builders.

A primary install is one HA device; each bandit it reports is its own device
linked back to the server through via_device, so the HA device page mirrors the
physical fleet.
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import PantryRaiderCoordinator
from .helpers import server_device_model


def primary_device_info(host: str, port: int, data: dict[str, Any]) -> DeviceInfo:
    """Device_info for the install the user configured directly."""

    device_id = str(data.get("device_id") or f"{host}:{port}")
    hostname = data.get("hostname") or "Pantry Raider"
    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        manufacturer=MANUFACTURER,
        name=hostname,
        model=server_device_model(data.get("mode")),
        sw_version=data.get("version"),
        configuration_url=f"http://{host}:{port}",
    )


def satellite_device_info(
    server_device_id: str, sat: dict[str, Any]
) -> DeviceInfo:
    """Device_info for a bandit, linked to its server via via_device."""

    device_id = str(sat.get("device_id"))
    hostname = sat.get("hostname") or device_id
    ip = sat.get("ip")
    info = DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        manufacturer=MANUFACTURER,
        name=f"Pantry Raider Bandit {hostname}",
        model="Pantry Raider Bandit",
        sw_version=sat.get("version"),
        via_device=(DOMAIN, server_device_id),
    )
    if ip:
        # A bandit answers /ha/state on port 80; its config page is the same host.
        info["configuration_url"] = f"http://{ip}"
    return info


class PantryRaiderEntity(CoordinatorEntity[PantryRaiderCoordinator]):
    """Base entity: names come from the device, availability from the poll."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PantryRaiderCoordinator,
        device_info: DeviceInfo,
        device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_device_info = device_info
