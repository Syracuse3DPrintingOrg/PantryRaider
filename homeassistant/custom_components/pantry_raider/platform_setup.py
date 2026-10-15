"""Shared platform wiring: primary device plus per-bandit devices.

Every platform (sensor, number, select, binary_sensor) exposes the same
device shape: entities for the install the user configured, and, when that
install is a server or appliance, a matching set for each bandit it reports.
Rather than repeat that plumbing four times, each platform hands this helper a
``build_fn`` that turns one coordinator plus its DeviceInfo into that platform's
entities.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import PantryRaiderCoordinator
from .entity import primary_device_info, satellite_device_info

# (coordinator, device_id, device_info, is_satellite) -> entities.
BuildFn = Callable[
    [PantryRaiderCoordinator, str, DeviceInfo, bool], Iterable[Entity]
]


async def async_setup_install_platform(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
    build_fn: BuildFn,
) -> None:
    """Add this platform's entities for the primary install and each bandit.

    New bandits that appear in a later poll are added on the fly: a listener on
    the primary coordinator ensures a coordinator exists for each and adds the
    unseen ones. Removal waits for a reload, so a transient drop never yanks
    entities out from under running automations.
    """

    runtime = entry.runtime_data
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    main_data = runtime.main.data or {}
    server_device_id = str(main_data.get("device_id") or f"{host}:{port}")

    async_add_entities(
        list(
            build_fn(
                runtime.main,
                server_device_id,
                primary_device_info(host, port, main_data),
                False,
            )
        )
    )

    known: set[str] = set()

    @callback
    def _add_new_satellite_entities() -> None:
        new_entities: list[Entity] = []
        for device_id, coordinator in list(runtime.satellite_coordinators.items()):
            if device_id in known:
                continue
            sat = runtime.satellite_info.get(device_id, {})
            new_entities.extend(
                build_fn(
                    coordinator,
                    device_id,
                    satellite_device_info(server_device_id, sat),
                    True,
                )
            )
            known.add(device_id)
        if new_entities:
            async_add_entities(new_entities)

    _add_new_satellite_entities()

    async def _ensure_and_add() -> None:
        # A server poll may reveal a brand new bandit; make sure it has a
        # coordinator, then add its entities.
        await runtime.async_sync_satellites(hass)
        _add_new_satellite_entities()

    @callback
    def _on_main_update() -> None:
        hass.async_create_task(_ensure_and_add())

    entry.async_on_unload(runtime.main.async_add_listener(_on_main_update))
