"""The Pantry Raider integration.

Sets up the primary install's coordinator, spins up a coordinator per satellite
bandit the install reports, and forwards the entity platforms. Everything runs
on Home Assistant's shared aiohttp session, so there are no extra dependencies
and no blocking I/O.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import voluptuous as vol

from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .api import (
    PantryRaiderAuthError,
    PantryRaiderClient,
    PantryRaiderConnectionError,
)
from .const import (
    CONF_CONNECT_BACK,
    CONF_SCAN_INTERVAL,
    CONNECT_BACK_CLIENT_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    OPT_CONNECT_DONE,
    PLATFORMS,
    SERVICE_CAMERA_DETECT,
    SERVICE_CAMERA_POPUP,
)
from .coordinator import PantryRaiderCoordinator
from .helpers import (
    DETECTION_TYPES,
    MAX_POPUP_SECONDS,
    clamp_popup_seconds,
    is_server_mode,
    match_install,
    normalize_detection_type,
    satellite_device_ids,
)

_LOGGER = logging.getLogger(__name__)

# Typed config entry for runtime_data. Written as a plain alias (not a PEP 695
# "type" statement) so it parses on the app's Python 3.11 lint floor while
# still giving Home Assistant, which runs on 3.12+, the same typing; the string
# forward reference keeps the dataclass below from having to move above it.
PantryRaiderConfigEntry = ConfigEntry["PantryRaiderRuntimeData"]


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
    connect_back: bool = True
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
    connect_back = entry.options.get(CONF_CONNECT_BACK, True)

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
        connect_back=connect_back,
    )
    # Build coordinators for any bandits the server already reports before the
    # platforms load, so their entities appear on the first pass.
    await runtime.async_sync_satellites(hass)
    entry.runtime_data = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # A change to the poll interval or connect-back toggle reloads the entry.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Domain-level services, registered once no matter how many entries exist.
    _async_register_services(hass)

    # Hand a Home Assistant token back to the install (primary installs only),
    # once. Wrapped so any failure only warns and never breaks setup.
    await _async_connect_back(hass, entry, coordinator)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry
) -> bool:
    """Unload a config entry and all its platforms."""

    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # Drop the domain services only when this was the last loaded entry, so a
    # second install keeps them working.
    if ok and not _other_loaded_entries(hass, entry):
        hass.services.async_remove(DOMAIN, SERVICE_CAMERA_POPUP)
        hass.services.async_remove(DOMAIN, SERVICE_CAMERA_DETECT)
    return ok


async def _async_update_listener(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry
) -> None:
    """Reload only when a reload-relevant option changed.

    A new scan interval or connect-back toggle needs a rebuild; a bookkeeping
    write (marking connect-back done) must not, or setting that marker would
    loop the entry.
    """

    runtime = entry.runtime_data
    new_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    new_connect_back = entry.options.get(CONF_CONNECT_BACK, True)
    if (
        new_interval != runtime.scan_interval
        or new_connect_back != runtime.connect_back
    ):
        await hass.config_entries.async_reload(entry.entry_id)


async def _async_mint_owner_token(hass: HomeAssistant) -> str | None:
    """Create a long-lived access token for the owner, named for this app.

    Reuse-or-recreate: any existing "Pantry Raider" long-lived token for the
    owner is removed first, then a fresh one is minted. Delete plus recreate is
    the simplest safe choice here because only this integration ever uses that
    client name, so we never step on another feature's token, and the install
    always ends up with exactly one current key it can use.
    """

    user = await hass.auth.async_get_owner()
    if user is None:
        _LOGGER.warning(
            "No Home Assistant owner account found; skipping Pantry Raider "
            "connect-back"
        )
        return None
    for token in list(user.refresh_tokens.values()):
        if (
            token.client_name == CONNECT_BACK_CLIENT_NAME
            and token.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
        ):
            await hass.auth.async_remove_refresh_token(token)
    refresh = await hass.auth.async_create_refresh_token(
        user,
        client_name=CONNECT_BACK_CLIENT_NAME,
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=3650),
    )
    return hass.auth.async_create_access_token(refresh)


async def _async_connect_back(
    hass: HomeAssistant,
    entry: PantryRaiderConfigEntry,
    coordinator: PantryRaiderCoordinator,
) -> None:
    """Give a primary install a Home Assistant URL and owner token, once.

    Only a server or appliance (the primary that fronts cameras and Stream Deck)
    gets a token; a satellite inherits the connection from its server. Guarded
    by the connect-back toggle and a one-shot marker so a reload does not
    re-mint. Every failure only logs a warning and leaves entry setup intact.
    """

    if not entry.options.get(CONF_CONNECT_BACK, True):
        return
    if entry.options.get(OPT_CONNECT_DONE):
        return
    if not is_server_mode((coordinator.data or {}).get("mode")):
        return

    try:
        token = await _async_mint_owner_token(hass)
        if token is None:
            return
        try:
            base_url = get_url(hass, prefer_external=False, allow_cloud=False)
        except NoURLAvailableError:
            # No internal URL configured; try an external one before giving up.
            try:
                base_url = get_url(hass, prefer_external=True, allow_cloud=False)
            except NoURLAvailableError:
                _LOGGER.warning(
                    "Could not determine a Home Assistant URL for Pantry Raider "
                    "connect-back; set one under Settings, System, Network"
                )
                return
        result = await coordinator.client.async_connect(base_url, token)
    except Exception as err:  # noqa: BLE001 - never break setup over connect-back
        _LOGGER.warning("Pantry Raider connect-back did not complete: %s", err)
        return

    if not result.get("verified"):
        _LOGGER.warning(
            "Pantry Raider accepted the Home Assistant connection but could not "
            "verify it from %s; cameras may need the URL adjusted in Pantry "
            "Raider under Settings, Connections",
            base_url,
        )
    else:
        _LOGGER.info(
            "Pantry Raider connected back to Home Assistant at %s", base_url
        )

    # Mark done so a later reload does not re-mint. A failure above returned
    # early and left this unset, so the next reload retries.
    options = dict(entry.options)
    options[OPT_CONNECT_DONE] = True
    hass.config_entries.async_update_entry(entry, options=options)


# --- Domain services: camera_popup and camera_detect ------------------------
# The notify entities cover plain messages; these two cover the camera events,
# which are actions on a device rather than a state, so they are services. Both
# take an optional device_id (any Pantry Raider device, main or bandit) and
# post to that install's own client.

_SERVICE_CAMERA_POPUP_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        vol.Optional("camera", default=""): cv.string,
        vol.Optional("seconds", default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MAX_POPUP_SECONDS)
        ),
    }
)

_SERVICE_CAMERA_DETECT_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): cv.string,
        vol.Optional("camera", default=""): cv.string,
        # Case and whitespace are forgiven ("Person" from a hand-written
        # automation still validates), anything unknown is refused clearly.
        vol.Required("detection_type"): vol.All(
            normalize_detection_type, vol.In(DETECTION_TYPES)
        ),
        vol.Optional("seconds", default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MAX_POPUP_SECONDS)
        ),
    }
)


def _loaded_entries(hass: HomeAssistant) -> list[PantryRaiderConfigEntry]:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]


def _other_loaded_entries(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry
) -> list[PantryRaiderConfigEntry]:
    return [e for e in _loaded_entries(hass) if e.entry_id != entry.entry_id]


def _entry_main_device_id(entry: PantryRaiderConfigEntry) -> str:
    runtime = entry.runtime_data
    data = runtime.main.data or {}
    return str(
        data.get("device_id")
        or f"{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}"
    )


def _client_for_call(hass: HomeAssistant, call: ServiceCall) -> PantryRaiderClient:
    """The client a service call targets: the chosen device, or the only entry.

    With no device chosen and exactly one config entry, the entry's primary
    install is the obvious target; anything ambiguous or unknown raises a
    validation error the user sees in the service UI.
    """

    loaded = _loaded_entries(hass)
    device_id = call.data.get("device_id")
    if not device_id:
        if len(loaded) == 1:
            return loaded[0].runtime_data.main.client
        raise ServiceValidationError(
            "Pick which Pantry Raider device this should show on."
        )

    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError("That device is not known to Home Assistant.")
    pr_ids = [ident[1] for ident in device.identifiers if ident[0] == DOMAIN]
    if not pr_ids:
        raise ServiceValidationError("That device is not a Pantry Raider device.")

    for entry in loaded:
        runtime = entry.runtime_data
        main_id = _entry_main_device_id(entry)
        for pr_id in pr_ids:
            match = match_install(
                pr_id, main_id, runtime.satellite_coordinators.keys()
            )
            if match == "main":
                return runtime.main.client
            if match:
                return runtime.satellite_coordinators[match].client
    raise ServiceValidationError(
        "That Pantry Raider device is not loaded right now."
    )


async def _async_post_event(coro, not_ok_message: str) -> None:
    """Run one event post and turn its failures into user-facing errors."""

    try:
        result = await coro
    except PantryRaiderAuthError as err:
        raise HomeAssistantError(
            "Pantry Raider rejected the API key for this call."
        ) from err
    except PantryRaiderConnectionError as err:
        raise HomeAssistantError(
            "Could not reach this Pantry Raider device (it may be offline or "
            "an older version)."
        ) from err
    if not result.get("ok"):
        raise HomeAssistantError(result.get("error") or not_ok_message)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain services once, no matter how many entries load."""

    if hass.services.has_service(DOMAIN, SERVICE_CAMERA_POPUP):
        return

    async def _handle_camera_popup(call: ServiceCall) -> None:
        client = _client_for_call(hass, call)
        await _async_post_event(
            client.async_camera_popup(
                camera=call.data.get("camera") or "",
                seconds=clamp_popup_seconds(call.data.get("seconds")),
            ),
            "Pantry Raider could not show that camera.",
        )

    async def _handle_camera_detect(call: ServiceCall) -> None:
        client = _client_for_call(hass, call)
        await _async_post_event(
            client.async_camera_detect(
                camera=call.data.get("camera") or "",
                detection_type=call.data["detection_type"],
                seconds=clamp_popup_seconds(call.data.get("seconds")),
            ),
            "Pantry Raider could not handle that detection.",
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CAMERA_POPUP,
        _handle_camera_popup,
        schema=_SERVICE_CAMERA_POPUP_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CAMERA_DETECT,
        _handle_camera_detect,
        schema=_SERVICE_CAMERA_DETECT_SCHEMA,
    )
