"""The Pantry Raider integration.

Sets up the primary install's coordinator, spins up a coordinator per satellite
bandit the install reports, and forwards the entity platforms. Everything runs
on Home Assistant's shared aiohttp session, so there are no extra dependencies
and no blocking I/O.

It also gives a primary install its own way back into Home Assistant: a
limited Home Assistant account made for that install, whose token is handed
over with POST /ha/connect and replaced once a year. Every decision about
that lives in helpers.handover_plan (pure and tested); this module carries it
out, and only ever removes tokens this entry made itself.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import voluptuous as vol

from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.loader import async_get_integration

from .api import (
    PantryRaiderAuthError,
    PantryRaiderClient,
    PantryRaiderConnectionError,
)
from .const import (
    CONF_CONNECT_BACK,
    CONF_PAIRING_OFF,
    CONF_SCAN_INTERVAL,
    CONNECT_BACK_CLIENT_NAME,
    DEDICATED_TOKEN_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_PROBE_ALERT,
    ISSUE_APP_UPDATE,
    ISSUE_MANUAL_TOKEN,
    ISSUE_OPEN_GRACE,
    ISSUE_OPEN_NAME,
    ISSUE_PAIR_NEEDED,
    ISSUE_PAIRING_OFF,
    OLD_TOKEN_GRACE_SECONDS,
    OPT_CONNECT_DONE,
    OPT_LEGACY_REMOVE_AFTER,
    OPT_OWNER_TOKEN_ID,
    OPT_PENDING_REMOVALS,
    OPT_REMOVED_REFS,
    OPT_TOKEN_CREATED,
    OPT_TOKEN_GEN,
    OPT_TOKEN_ID,
    OPT_TOKEN_KIND,
    OPT_USER_ID,
    PLATFORMS,
    SERVICE_CAMERA_DETECT,
    SERVICE_CAMERA_POPUP,
    SERVICE_PROBE_ALERT,
)
from .coordinator import PantryRaiderCoordinator
from .helpers import (
    DETECTION_TYPES,
    MAX_POPUP_SECONDS,
    clamp_popup_seconds,
    dedicated_token_name,
    dedicated_user_name,
    grace_issue_id,
    grace_issue_placeholders,
    handover_plan,
    http_url,
    is_server_mode,
    legacy_owner_token_removable,
    match_install,
    normalize_detection_type,
    owner_token_name,
    pasted_by_hand,
    probe_alert_event_data,
    remember_removed_ref,
    satellite_device_ids,
    token_ref_for_id,
)

_LOGGER = logging.getLogger(__name__)

try:
    # Home Assistant 2025.8 and later reload an entry after its options flow
    # changes an option (config_flow.py uses OptionsFlowWithReload there).
    from homeassistant.config_entries import OptionsFlowWithReload  # noqa: F401

    OPTIONS_FLOW_RELOADS = True
except ImportError:
    OPTIONS_FLOW_RELOADS = False

# Kept in entry data after a key exchange: the shared key the entry was set
# up with. Bandits check keys on their own and only know that one, so their
# coordinators keep using it.
_DATA_BANDIT_KEY = "bandit_api_key"

# The owner token handed to an install too old for a limited account keeps
# the lifetime older versions gave it.
_OWNER_TOKEN_DAYS = 3650

# A handover that is due again (a yearly replacement, or an install that lost
# its token) is tried from the poll at most this often.
_HANDOVER_RETRY_SECONDS = 3600

# A token that could not be removed is tried this many times before it is
# dropped from the list (it is usually already gone).
_MAX_REMOVAL_TRIES = 5

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
    version: str = ""
    bandit_api_key: str | None = None
    satellite_coordinators: dict[str, PantryRaiderCoordinator] = field(
        default_factory=dict
    )
    satellite_info: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_handover: float = 0.0
    upkeep_running: bool = False

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
                http_url(ip, 80),
                self.bandit_api_key or self.api_key,
                self.version,
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


async def _async_version(hass: HomeAssistant) -> str:
    """This integration's version, sent to installs as X-PR-Client."""
    try:
        integration = await async_get_integration(hass, DOMAIN)
        return str(integration.version or "")
    except Exception:  # noqa: BLE001 - the header is informational only
        return ""


async def async_setup_entry(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry
) -> bool:
    """Set up Pantry Raider from a config entry."""

    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    api_key = entry.data.get(CONF_API_KEY) or None
    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    connect_back = entry.options.get(CONF_CONNECT_BACK, True)
    version = await _async_version(hass)

    client = PantryRaiderClient(
        async_get_clientsession(hass),
        http_url(host, port),
        api_key,
        version,
    )
    coordinator = PantryRaiderCoordinator(
        hass, entry, client, scan_interval, name=f"{DOMAIN} {host}"
    )
    # Fail setup (retry later) if the install is unreachable on first contact,
    # so HA shows the entry as retrying rather than half-built. A refused key
    # starts Re-authenticate instead.
    await coordinator.async_config_entry_first_refresh()

    runtime = PantryRaiderRuntimeData(
        entry=entry,
        main=coordinator,
        api_key=api_key,
        scan_interval=scan_interval,
        connect_back=connect_back,
        version=version,
        bandit_api_key=entry.data.get(_DATA_BANDIT_KEY) or None,
    )
    entry.runtime_data = runtime

    # Swap a shared key for one of Home Assistant's own before the bandits
    # are built, so they keep the key they already know.
    await _async_exchange_legacy_key(hass, entry)

    # Build coordinators for any bandits the server already reports before the
    # platforms load, so their entities appear on the first pass.
    await runtime.async_sync_satellites(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # A change to the poll interval or connect-back toggle reloads the entry.
    # Current Home Assistant does that itself after the options flow; an
    # update listener there draws a warning on every reauth and every move to
    # a new address, so it is only added on releases without that.
    if not OPTIONS_FLOW_RELOADS:
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Domain-level services, registered once no matter how many entries exist.
    _async_register_services(hass)

    # Repairs and the Home Assistant token handover (primary installs only).
    # Each step only warns on failure and never breaks setup.
    await _async_handover(hass, entry)

    @callback
    def _on_update() -> None:
        hass.async_create_task(_async_upkeep(hass, entry))

    entry.async_on_unload(coordinator.async_add_listener(_on_update))
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
        hass.services.async_remove(DOMAIN, SERVICE_PROBE_ALERT)
    return ok


async def async_remove_entry(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry
) -> None:
    """Clean up after an entry is deleted: the install forgets the limited
    account's token and turns off the key it made for this entry, and the
    tokens and account this entry made are removed. Nothing this entry did
    not make is touched, and an install that cannot be reached never stops
    the entry from being removed."""

    options = entry.options
    api_key = entry.data.get(CONF_API_KEY) or None
    client: PantryRaiderClient | None = None
    try:
        if options.get(OPT_TOKEN_KIND) == "dedicated" or api_key:
            client = PantryRaiderClient(
                async_get_clientsession(hass),
                http_url(entry.data[CONF_HOST], entry.data[CONF_PORT]),
                api_key,
                await _async_version(hass),
            )
    except Exception as err:  # noqa: BLE001 - never block removing the entry
        _LOGGER.debug("Could not build a Pantry Raider client: %s", err)
    if client is not None and options.get(OPT_TOKEN_KIND) == "dedicated":
        try:
            async with asyncio.timeout(5):
                await client.async_disconnect()
        except Exception as err:  # noqa: BLE001 - the install may be offline
            _LOGGER.debug("Pantry Raider did not confirm the disconnect: %s", err)
    # Last call made with the key, since it stops working once this succeeds.
    # A key another entry still uses is kept.
    try:
        key_shared = any(
            other.entry_id != entry.entry_id
            and other.data.get(CONF_API_KEY) == api_key
            for other in hass.config_entries.async_entries(DOMAIN)
        )
    except Exception:  # noqa: BLE001 - when unsure, keep the key
        key_shared = True
    if client is not None and api_key and not key_shared:
        await _async_revoke_own_key(client)
    try:
        token_ids = [options.get(OPT_TOKEN_ID), options.get(OPT_OWNER_TOKEN_ID)]
        token_ids += [
            item.get("token_id")
            for item in options.get(OPT_PENDING_REMOVALS) or []
            if isinstance(item, dict)
        ]
        for token_id in token_ids:
            if token_id:
                await _async_remove_token_id(hass, token_id)
        user_id = options.get(OPT_USER_ID)
        shared = any(
            other.entry_id != entry.entry_id
            and other.options.get(OPT_USER_ID) == user_id
            for other in hass.config_entries.async_entries(DOMAIN)
        )
        if user_id and not shared:
            user = await hass.auth.async_get_user(user_id)
            if user is not None and not user.is_owner and not user.system_generated:
                await hass.auth.async_remove_user(user)
    except Exception as err:  # noqa: BLE001 - never block removing the entry
        _LOGGER.warning("Could not tidy up Pantry Raider's Home Assistant account: %s", err)
    for issue in (
        ISSUE_PAIR_NEEDED,
        ISSUE_PAIRING_OFF,
        ISSUE_APP_UPDATE,
        ISSUE_OPEN_GRACE,
        ISSUE_OPEN_NAME,
        ISSUE_MANUAL_TOKEN,
    ):
        ir.async_delete_issue(hass, DOMAIN, f"{issue}_{entry.entry_id}")


async def _async_revoke_own_key(client: PantryRaiderClient) -> bool:
    """Ask the install to turn off the key this entry holds. True when it did.

    Best effort: an offline install, an older one without the route, or a key
    the install did not make for Home Assistant (a shared key, or one made by
    hand) all leave the key as it is, and the owner can remove it from the
    install's settings."""
    try:
        async with asyncio.timeout(5):
            result = await client.async_revoke_own_key()
    except Exception as err:  # noqa: BLE001 - the install may be offline
        _LOGGER.info(
            "Pantry Raider could not be reached to turn off this entry's key; "
            "remove it in Pantry Raider's settings if it is still listed (%s)",
            err,
        )
        return False
    if result.get("revoked"):
        return True
    if not result.get("unsupported"):
        _LOGGER.debug("Pantry Raider kept this entry's key: %s", result.get("error"))
    return False


async def _async_update_listener(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry
) -> None:
    """Reload only when a reload-relevant option changed.

    A new scan interval or connect-back toggle needs a rebuild; a bookkeeping
    write (marking connect-back done, recording a token) must not, or setting
    that marker would loop the entry.
    """

    runtime = entry.runtime_data
    new_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    new_connect_back = entry.options.get(CONF_CONNECT_BACK, True)
    if (
        new_interval != runtime.scan_interval
        or new_connect_back != runtime.connect_back
    ):
        await hass.config_entries.async_reload(entry.entry_id)


# --- Small helpers -------------------------------------------------------------


async def _maybe_await(result: Any) -> Any:
    """Await a Home Assistant auth call only when it returned an awaitable.

    async_get_refresh_token and async_remove_refresh_token are plain callbacks
    in current Home Assistant releases (and coroutines in some older ones).
    Awaiting a callback's result raises TypeError, which once made every
    token removal fail silently."""
    if inspect.isawaitable(result):
        return await result
    return result


async def _async_remove_token_id(hass: HomeAssistant, token_id: str) -> bool:
    """Remove one refresh token by id. False when it no longer exists."""
    token = await _maybe_await(hass.auth.async_get_refresh_token(token_id))
    if token is None:
        return False
    await _maybe_await(hass.auth.async_remove_refresh_token(token))
    return True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _update_options(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry, changes: dict[str, Any]
) -> None:
    """Merge bookkeeping into the entry's options (None removes a key)."""
    options = dict(entry.options)
    for key, value in changes.items():
        if value is None:
            options.pop(key, None)
        else:
            options[key] = value
    hass.config_entries.async_update_entry(entry, options=options)


def _ha_url(hass: HomeAssistant) -> tuple[str | None, bool]:
    """(Home Assistant's URL for the install, whether it is the local one)."""
    try:
        return get_url(hass, allow_external=False, allow_cloud=False), True
    except NoURLAvailableError:
        pass
    try:
        return get_url(hass, allow_internal=False, allow_cloud=False), False
    except NoURLAvailableError:
        return None, False


async def _async_plan(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry, used_internal_url: bool
) -> dict[str, Any]:
    data = entry.runtime_data.main.data or {}
    options = dict(entry.options)
    token_id = options.get(OPT_TOKEN_ID)
    if token_id:
        # A token that no longer exists (someone deleted the account or the
        # token in Home Assistant), or whose account was switched off or made
        # an administrator, is treated as never made, so a new one is handed
        # over on a limited account. The old one is removed an hour later.
        try:
            token = await _maybe_await(hass.auth.async_get_refresh_token(token_id))
            if token is None or not _is_usable_user(getattr(token, "user", None)):
                options.pop(OPT_TOKEN_ID, None)
        except Exception as err:  # noqa: BLE001 - plan with what is recorded
            _LOGGER.debug("Could not look up Pantry Raider's token: %s", err)
    access = data.get("ha_access") if isinstance(data.get("ha_access"), dict) else {}
    if str(access.get("token_kind") or "") == "" and access.get("token_ref"):
        # A token saved before 0.21 carries no kind. One somebody pasted by
        # hand is left alone, exactly like a token pasted on 0.21: the
        # install keeps it and it is never removed from Home Assistant.
        if await _async_install_token_pasted(hass, entry, str(access["token_ref"])):
            data = {**data, "ha_access": {**access, "token_kind": "manual"}}
    return handover_plan(
        is_server=is_server_mode(data.get("mode")),
        connect_back=entry.options.get(CONF_CONNECT_BACK, True),
        state=data,
        options=options,
        now_iso=_now().isoformat(),
        used_internal_url=used_internal_url,
    )


async def _async_install_token_pasted(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry, token_ref: str
) -> bool:
    """Whether the long-lived token the install reports holding (by its
    token_ref) was pasted by hand. False when it cannot be told."""
    try:
        users = await _maybe_await(hass.auth.async_get_users())
    except Exception as err:  # noqa: BLE001 - plan as before
        _LOGGER.debug("Could not list Home Assistant users: %s", err)
        return False
    found = None
    for user in users or []:
        for token in list(getattr(user, "refresh_tokens", {}).values()):
            if (
                token.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
                and token_ref_for_id(token.id) == token_ref
            ):
                found = {"id": token.id, "client_name": token.client_name,
                         "owner": bool(getattr(user, "is_owner", False))}
                break
        if found:
            break
    own = [entry.options.get(OPT_TOKEN_ID), entry.options.get(OPT_OWNER_TOKEN_ID)]
    return pasted_by_hand(found, own, CONNECT_BACK_CLIENT_NAME)


# --- Legacy key exchange ---------------------------------------------------------


async def _async_exchange_legacy_key(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry
) -> None:
    """Trade the shared key this entry was set up with for a Home Assistant
    key of its own. The old key keeps working on the install, so any failure
    (an older install answers 404) just keeps it."""

    runtime = entry.runtime_data
    data = runtime.main.data or {}
    auth = data.get("auth") if isinstance(data.get("auth"), dict) else {}
    old_key = entry.data.get(CONF_API_KEY)
    if auth.get("credential") != "legacy" or not old_key:
        return
    try:
        result = await runtime.main.client.async_exchange_legacy_key()
    except Exception as err:  # noqa: BLE001 - keep the old key silently
        _LOGGER.debug("Pantry Raider kept the existing key: %s", err)
        return
    new_key = result.get("key") if isinstance(result, dict) and result.get("ok") else None
    if not new_key:
        return
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_API_KEY: new_key,
            _DATA_BANDIT_KEY: entry.data.get(_DATA_BANDIT_KEY) or old_key,
        },
    )
    runtime.api_key = new_key
    runtime.bandit_api_key = runtime.bandit_api_key or old_key
    runtime.main.client.set_api_key(new_key)
    _LOGGER.info("Pantry Raider gave Home Assistant a key of its own")


# --- Repairs -----------------------------------------------------------------------


def _set_issue(
    hass: HomeAssistant,
    entry: PantryRaiderConfigEntry,
    issue: str,
    active: bool,
    *,
    fixable: bool = False,
    placeholders: dict[str, str] | None = None,
) -> None:
    issue_id = f"{issue}_{entry.entry_id}"
    if not active:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=fixable,
        severity=ir.IssueSeverity.WARNING,
        translation_key=issue,
        translation_placeholders=placeholders,
        data={"entry_id": entry.entry_id},
    )


@callback
def _async_sync_issues(
    hass: HomeAssistant,
    entry: PantryRaiderConfigEntry,
    plan: dict[str, Any],
) -> None:
    """Raise or clear this entry's Repairs issues from the latest state."""

    try:
        data = entry.runtime_data.main.data or {}
        features = data.get("features") if isinstance(data.get("features"), list) else []
        keyless = not entry.data.get(CONF_API_KEY)
        wants_pairing = keyless and "pairing_kinds" in features
        pairing_off = bool(entry.data.get(CONF_PAIRING_OFF))
        _set_issue(hass, entry, ISSUE_PAIR_NEEDED, wants_pairing and not pairing_off,
                   fixable=True)
        _set_issue(hass, entry, ISSUE_PAIRING_OFF, wants_pairing and pairing_off,
                   fixable=True)

        grace_id = grace_issue_id(data)
        placeholders = None
        if grace_id is not None:
            placeholders = {
                "host": str(entry.data.get(CONF_HOST) or entry.title),
                **(grace_issue_placeholders(data) or {}),
            }
        for issue in (ISSUE_OPEN_GRACE, ISSUE_OPEN_NAME):
            _set_issue(hass, entry, issue, issue == grace_id, placeholders=placeholders)

        for issue in (ISSUE_APP_UPDATE, ISSUE_MANUAL_TOKEN):
            _set_issue(hass, entry, issue, plan.get("repair") == issue)
    except Exception as err:  # noqa: BLE001 - a repair note never breaks a poll
        _LOGGER.debug("Could not update Pantry Raider repair notes: %s", err)


# --- Token handover ------------------------------------------------------------------


async def _async_handover(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry
) -> None:
    """Carry out handover_plan for this entry. Never raises."""

    runtime = entry.runtime_data
    runtime.last_handover = time.monotonic()
    try:
        base_url, used_internal = _ha_url(hass)
        plan = await _async_plan(hass, entry, used_internal)
        _async_sync_issues(hass, entry, plan)
        action = plan["action"]
        if action == "disconnect_own":
            await _async_disconnect_own(hass, entry)
            return
        if action not in ("owner", "dedicated"):
            return
        if action == "dedicated":
            user = await _async_dedicated_user(hass, entry, plan)
            if user is None or not plan["mint"]:
                return
        elif not plan["mint"]:
            return
        if base_url is None:
            _LOGGER.warning(
                "Could not determine a Home Assistant URL for Pantry Raider "
                "connect-back; set one under Settings, System, Network"
            )
            return
        if action == "owner":
            await _async_hand_over_owner_token(hass, entry, base_url)
        else:
            await _async_hand_over_dedicated_token(hass, entry, user, base_url)
    except Exception as err:  # noqa: BLE001 - never break setup over connect-back
        _LOGGER.warning("Pantry Raider connect-back did not complete: %s", err)


def _report_connect(result: dict[str, Any], base_url: str) -> None:
    if not result.get("verified"):
        _LOGGER.warning(
            "Pantry Raider accepted the Home Assistant connection but could not "
            "verify it from %s; cameras may need the URL adjusted in Pantry "
            "Raider under Settings, Home Assistant",
            base_url,
        )
    else:
        _LOGGER.info("Pantry Raider connected back to Home Assistant at %s", base_url)


async def _async_hand_over_owner_token(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry, base_url: str
) -> None:
    """An install too old for a limited account gets an owner token named for
    this entry, once. Tokens with any other name are never touched."""

    owner = await hass.auth.async_get_owner()
    if owner is None:
        _LOGGER.warning(
            "No Home Assistant owner account found; skipping Pantry Raider "
            "connect-back"
        )
        return
    name = owner_token_name(entry.title, entry.entry_id)
    # Home Assistant refuses a second token with the same name, so this
    # entry's own earlier one goes first: the one it recorded (which an older
    # version of this integration may have named differently) and any by
    # this entry's name.
    earlier = entry.options.get(OPT_OWNER_TOKEN_ID)
    for token in list(owner.refresh_tokens.values()):
        if token.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN and (
            token.client_name == name or (earlier and token.id == earlier)
        ):
            await _maybe_await(hass.auth.async_remove_refresh_token(token))
    refresh = await hass.auth.async_create_refresh_token(
        owner,
        client_name=name,
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=_OWNER_TOKEN_DAYS),
    )
    access = hass.auth.async_create_access_token(refresh)
    runtime = entry.runtime_data
    try:
        result = await runtime.main.client.async_connect(
            base_url, access, token_kind="owner", component_version=runtime.version
        )
    except Exception:
        await _async_remove_token_id(hass, refresh.id)
        raise
    _report_connect(result, base_url)
    # Marked done so a later reload does not mint again; a failure above left
    # it unset, so the next load retries.
    _update_options(
        hass,
        entry,
        {OPT_OWNER_TOKEN_ID: refresh.id, OPT_TOKEN_KIND: "owner", OPT_CONNECT_DONE: True},
    )


def _note_removed(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry, token_ids: list[str]
) -> None:
    """Remember the tokens this entry removed, so an install found still
    holding one of them is handed a new token (helpers.handover_plan)."""
    refs = entry.options.get(OPT_REMOVED_REFS)
    for token_id in token_ids:
        refs = remember_removed_ref(refs, token_id)
    if refs != entry.options.get(OPT_REMOVED_REFS):
        _update_options(hass, entry, {OPT_REMOVED_REFS: refs})


async def _async_install_holds(
    entry: PantryRaiderConfigEntry, token_id: str
) -> bool | None:
    """Whether the install reports holding this token (its ha_access.token_ref).
    None when it cannot be asked or does not say which token it holds."""
    try:
        state = await entry.runtime_data.main.client.async_get_state()
    except Exception:  # noqa: BLE001 - the install did not answer
        return None
    access = state.get("ha_access") if isinstance(state, dict) else None
    ref = access.get("token_ref") if isinstance(access, dict) else None
    if not ref:
        return None
    return ref == token_ref_for_id(token_id)


def _is_usable_user(user: Any) -> bool:
    if user is None or not user.is_active or user.is_owner or user.system_generated:
        return False
    return not any(group.id == GROUP_ID_ADMIN for group in user.groups)


async def _async_dedicated_user(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry, plan: dict[str, Any]
) -> Any:
    """The limited account for this install: reused when it still exists and
    is still limited, otherwise made new. Its group follows the plan."""

    user_id = entry.options.get(OPT_USER_ID)
    user = await hass.auth.async_get_user(user_id) if user_id else None
    if not _is_usable_user(user):
        user = await hass.auth.async_create_user(
            dedicated_user_name(entry.title),
            group_ids=[plan["group"]],
            local_only=plan["local_only"],
        )
        _update_options(hass, entry, {OPT_USER_ID: user.id})
        return user
    await _async_follow_group(hass, user, plan["group"], plan["local_only"])
    return user


async def _async_follow_group(
    hass: HomeAssistant, user: Any, group: str, local_only: bool | None = None
) -> None:
    """Put the limited account in the group the install's keys need."""
    groups = sorted(g.id for g in user.groups)
    changes: dict[str, Any] = {}
    if groups != [group]:
        changes["group_ids"] = [group]
    if local_only is not None and bool(user.local_only) != bool(local_only):
        changes["local_only"] = bool(local_only)
    if changes:
        await hass.auth.async_update_user(user, **changes)


async def _async_hand_over_dedicated_token(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry, user: Any, base_url: str
) -> None:
    """Mint the limited account's next token and hand it over. Old tokens are
    removed only after the install accepted the new one, and an hour later,
    so satellites holding a copy pick up the new one first."""

    options = entry.options
    gen = int(options.get(OPT_TOKEN_GEN) or 0) + 1
    taken = {token.client_name for token in user.refresh_tokens.values()}
    while dedicated_token_name(gen) in taken:
        gen += 1
    refresh = await hass.auth.async_create_refresh_token(
        user,
        client_name=dedicated_token_name(gen),
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=DEDICATED_TOKEN_DAYS),
    )
    access = hass.auth.async_create_access_token(refresh)
    runtime = entry.runtime_data
    try:
        result = await runtime.main.client.async_connect(
            base_url, access, token_kind="dedicated", component_version=runtime.version
        )
    except Exception:
        # A reply that timed out may still have been saved. Ask the install
        # which token it holds before taking this one back.
        holds = await _async_install_holds(entry, refresh.id)
        if not holds:
            # The install never accepted it: remove it and keep everything
            # else. When it could not say, the token is remembered as
            # removed, so should it have saved it after all it gets a new
            # token on a later poll.
            await _async_remove_token_id(hass, refresh.id)
            if holds is None:
                _note_removed(hass, entry, [refresh.id])
            raise
        _LOGGER.info("Pantry Raider confirmed it holds its new Home Assistant token")
    else:
        _report_connect(result, base_url)
    now = _now()
    after = (now + timedelta(seconds=OLD_TOKEN_GRACE_SECONDS)).isoformat()
    pending = [
        item for item in options.get(OPT_PENDING_REMOVALS) or [] if isinstance(item, dict)
    ]
    for old in (options.get(OPT_TOKEN_ID), options.get(OPT_OWNER_TOKEN_ID)):
        if old and old != refresh.id:
            pending.append({"token_id": old, "after": after})
    _update_options(
        hass,
        entry,
        {
            OPT_TOKEN_ID: refresh.id,
            OPT_TOKEN_CREATED: now.isoformat(),
            OPT_TOKEN_KIND: "dedicated",
            OPT_TOKEN_GEN: gen,
            OPT_CONNECT_DONE: True,
            OPT_OWNER_TOKEN_ID: None,
            OPT_PENDING_REMOVALS: pending,
            OPT_LEGACY_REMOVE_AFTER: after,
        },
    )


async def _async_disconnect_own(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry
) -> None:
    """Connect-back was turned off: the install forgets the connection and
    the tokens this entry made are removed. The limited account stays until
    the entry is deleted."""

    try:
        await entry.runtime_data.main.client.async_disconnect()
    except Exception as err:  # noqa: BLE001 - an older install has no route
        _LOGGER.debug("Pantry Raider did not confirm the disconnect: %s", err)
    for key in (OPT_TOKEN_ID, OPT_OWNER_TOKEN_ID):
        token_id = entry.options.get(key)
        if token_id:
            await _async_remove_token_id(hass, token_id)
    # The install was told to forget the connection and this entry's tokens
    # are gone, so it no longer counts as one that might still use the owner
    # token older versions shared (legacy_owner_token_removable). Turning
    # connect-back on again hands over a new token, as for a new entry.
    _update_options(
        hass,
        entry,
        {OPT_TOKEN_ID: None, OPT_OWNER_TOKEN_ID: None, OPT_TOKEN_CREATED: None,
         OPT_TOKEN_KIND: None, OPT_CONNECT_DONE: None},
    )


def _entry_summaries(hass: HomeAssistant) -> list[dict[str, Any]]:
    """What legacy_owner_token_removable needs about every entry."""
    return [
        {
            "history": bool(
                other.options.get(OPT_CONNECT_DONE) or other.options.get(OPT_TOKEN_KIND)
            ),
            "token_kind": other.options.get(OPT_TOKEN_KIND) or "",
        }
        for other in hass.config_entries.async_entries(DOMAIN)
    ]


async def _async_process_removals(
    hass: HomeAssistant, entry: PantryRaiderConfigEntry
) -> None:
    """Remove the replaced tokens whose hour is up, and the owner token older
    versions shared between installs once no install still needs it."""

    now = _now()
    options = entry.options
    pending = [
        item for item in options.get(OPT_PENDING_REMOVALS) or [] if isinstance(item, dict)
    ]
    keep: list[dict[str, Any]] = []
    removed: list[str] = []
    for item in pending:
        token_id = item.get("token_id")
        if not token_id:
            continue
        due = _parse_time(item.get("after"))
        if due is not None and now < due:
            keep.append(item)
            continue
        try:
            await _async_remove_token_id(hass, token_id)
            removed.append(token_id)
        except Exception as err:  # noqa: BLE001 - try again on a later poll
            tries = int(item.get("tries") or 0) + 1
            _LOGGER.warning("Could not remove an old Pantry Raider token: %s", err)
            if tries < _MAX_REMOVAL_TRIES:
                keep.append({**item, "tries": tries})
    changes: dict[str, Any] = {}
    if keep != pending:
        changes[OPT_PENDING_REMOVALS] = keep
    if removed:
        refs = options.get(OPT_REMOVED_REFS)
        for token_id in removed:
            refs = remember_removed_ref(refs, token_id)
        changes[OPT_REMOVED_REFS] = refs

    legacy_after = _parse_time(options.get(OPT_LEGACY_REMOVE_AFTER))
    if legacy_after is not None and now >= legacy_after:
        if legacy_owner_token_removable(_entry_summaries(hass)):
            owner = await hass.auth.async_get_owner()
            if owner is not None:
                for token in list(owner.refresh_tokens.values()):
                    if (
                        token.client_name == CONNECT_BACK_CLIENT_NAME
                        and token.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
                    ):
                        await _maybe_await(hass.auth.async_remove_refresh_token(token))
            changes[OPT_LEGACY_REMOVE_AFTER] = None
    if changes:
        _update_options(hass, entry, changes)


async def _async_upkeep(hass: HomeAssistant, entry: PantryRaiderConfigEntry) -> None:
    """After each poll of the main install: refresh the repair notes, keep the
    limited account's group in step with the install's keys, hand over a
    token when one is due again (at most once an hour), and remove replaced
    tokens whose hour is up."""

    runtime = getattr(entry, "runtime_data", None)
    if runtime is None or runtime.upkeep_running:
        return
    runtime.upkeep_running = True
    try:
        plan = await _async_plan(hass, entry, _ha_url(hass)[1])
        _async_sync_issues(hass, entry, plan)
        if plan["action"] == "dedicated":
            due = time.monotonic() - runtime.last_handover >= _HANDOVER_RETRY_SECONDS
            if plan["mint"] and due:
                await _async_handover(hass, entry)
            else:
                user_id = entry.options.get(OPT_USER_ID)
                user = await hass.auth.async_get_user(user_id) if user_id else None
                if _is_usable_user(user):
                    await _async_follow_group(hass, user, plan["group"])
        await _async_process_removals(hass, entry)
    except Exception as err:  # noqa: BLE001 - upkeep never breaks a poll
        _LOGGER.warning("Pantry Raider token upkeep did not complete: %s", err)
    finally:
        runtime.upkeep_running = False


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
        raise _invalid("device_needed")

    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise _invalid("device_unknown")
    pr_ids = [ident[1] for ident in device.identifiers if ident[0] == DOMAIN]
    if not pr_ids:
        raise _invalid("device_not_pantry_raider")

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
    raise _invalid("device_not_loaded")


def _invalid(key: str, **placeholders: str) -> ServiceValidationError:
    """A service call the user can fix (shown as a validation message, not
    as a server error), worded in strings.json under exceptions."""
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


async def _async_post_event(coro, not_ok_message: str) -> None:
    """Run one event post and turn its failures into user-facing errors.

    The install answers ok False only when it refuses what was asked, such as
    a camera name it does not have, so that is a validation error. A refused
    key or an install that cannot be reached is a real failure."""

    try:
        result = await coro
    except PantryRaiderAuthError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="key_rejected"
        ) from err
    except PantryRaiderConnectionError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="unreachable"
        ) from err
    if not result.get("ok"):
        raise _invalid(
            "request_refused", detail=str(result.get("error") or not_ok_message)
        )


_SERVICE_PROBE_ALERT_SCHEMA = vol.Schema({}, extra=vol.ALLOW_EXTRA)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain services once, no matter how many entries load."""

    if not hass.services.has_service(DOMAIN, SERVICE_PROBE_ALERT):

        async def _handle_probe_alert(call: ServiceCall) -> None:
            # The install calls this when a probe reaches its target. It
            # fires the same event the install used to post directly, so
            # automations keep working unchanged.
            hass.bus.async_fire(
                EVENT_PROBE_ALERT,
                probe_alert_event_data(call.data),
                context=call.context,
            )

        hass.services.async_register(
            DOMAIN,
            SERVICE_PROBE_ALERT,
            _handle_probe_alert,
            schema=_SERVICE_PROBE_ALERT_SCHEMA,
        )

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
