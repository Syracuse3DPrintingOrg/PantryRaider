"""Config and options flow for the Pantry Raider integration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.loader import async_get_integration

from .api import (
    PantryRaiderAuthError,
    PantryRaiderClient,
    PantryRaiderConnectionError,
    PantryRaiderNotSupported,
)
from .const import (
    CONF_CONNECT_BACK,
    CONF_PAIRING_OFF,
    CONF_SCAN_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    OPT_CONNECT_DONE,
)
from .helpers import discovery_moves_entry, http_url, pairing_result

try:
    # Home Assistant 2025.8 and later reload the entry when the options flow
    # changes an option, and warn about an update listener doing it.
    from homeassistant.config_entries import OptionsFlowWithReload as _OptionsBase
except ImportError:  # older Home Assistant: __init__ keeps its update listener
    _OptionsBase = OptionsFlow

# An approval whose key has not arrived is asked about this many more times,
# this many seconds apart, before the user is asked to press Submit again.
_KEY_RETRIES = 2
_KEY_RETRY_SECONDS = 1


class PantryRaiderConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Pantry Raider."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect host, port, and optional API key, then validate."""

        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            api_key = (user_input.get(CONF_API_KEY) or "").strip() or None
            result = await self._async_connect_or_pair(host, port, api_key, errors)
            if result is not None:
                return result

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=self._default(user_input, CONF_HOST)): str,
                vol.Required(
                    CONF_PORT, default=self._default(user_input, CONF_PORT, DEFAULT_PORT)
                ): int,
                vol.Optional(
                    CONF_API_KEY, default=self._default(user_input, CONF_API_KEY, "")
                ): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    async def _async_version(self) -> str:
        """This integration's version, sent to the install as X-PR-Client."""
        try:
            integration = await async_get_integration(self.hass, DOMAIN)
            return str(integration.version or "")
        except Exception:  # noqa: BLE001 - the header is informational only
            return ""

    async def _async_client(
        self, host: str, port: int, api_key: str | None
    ) -> PantryRaiderClient:
        return PantryRaiderClient(
            async_get_clientsession(self.hass),
            http_url(host, port),
            api_key,
            await self._async_version(),
        )

    async def _async_connect_or_pair(
        self, host: str, port: int, api_key: str | None, errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        """Try to finish the flow for one install, or start pairing.

        Shared by the user step and the zeroconf confirm step so the two entry
        points cannot drift. A typed key is checked with /ha/state. Without a
        key the flow always pairs (the same handshake a new Bandit uses,
        FoodAssistant-4box), even when the install answers without one today:
        an install without a password stops answering keyless calls once a
        password is set. The keyless /ha/state read only learns which install
        this is. Returns a flow result to return, or None with ``errors``
        populated to re-show the form.
        """

        client = await self._async_client(host, port, api_key)
        if api_key is not None:
            try:
                data = await client.async_get_state()
            except PantryRaiderAuthError:
                errors["base"] = "invalid_auth"
                return None
            except PantryRaiderConnectionError:
                errors["base"] = "cannot_connect"
                return None
            return await self._async_finish(host, port, api_key, data)

        keyless: dict[str, Any] | None = None
        try:
            keyless = await client.async_get_state()
        except PantryRaiderAuthError:
            keyless = None
        except PantryRaiderConnectionError:
            errors["base"] = "cannot_connect"
            return None

        # The kitchen shows a four digit code; the user approves it there (or
        # in Pantry Raider's settings) and this flow collects the key.
        self._pair_host = host
        self._pair_port = port
        try:
            started = await client.async_request_pairing("Home Assistant", kind="ha")
        except PantryRaiderAuthError:
            # Pairing is turned off on the install.
            if keyless is not None:
                return await self._async_finish(
                    host, port, None, keyless, pairing_off=True
                )
            errors["base"] = "pairing_unavailable"
            return None
        except PantryRaiderNotSupported:
            # An install older than pairing.
            if keyless is not None:
                return await self._async_finish(host, port, None, keyless)
            errors["base"] = "pairing_unavailable"
            return None
        except PantryRaiderConnectionError:
            errors["base"] = "cannot_connect"
            return None
        if started.get("ok") and started.get("request_id"):
            self._pair_request_id = started["request_id"]
            self._pair_code = str(started.get("code", ""))
            return await self.async_step_pair()
        errors["base"] = "pairing_unavailable"
        return None

    async def _async_finish(
        self,
        host: str,
        port: int,
        api_key: str | None,
        data: dict[str, Any],
        *,
        pairing_off: bool = False,
    ) -> ConfigFlowResult:
        """Create the entry for an install that answered /ha/state."""

        # Tie the entry to the install's own device_id so re-adding the same
        # box (or a zeroconf re-announce from a new IP) updates it instead of
        # duplicating.
        device_id = data.get("device_id") or f"{host}:{port}"
        # A discovered card for the same install may be waiting on the
        # integrations page. This flow has already paired (the install handed
        # over a key it will not hand out again), so it takes precedence and
        # the discovered card is closed when the entry is made.
        await self.async_set_unique_id(str(device_id), raise_on_progress=False)
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: host, CONF_PORT: port}
        )
        entry_data: dict[str, Any] = {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_API_KEY: api_key,
        }
        if pairing_off:
            # Setup raises a Repairs issue asking to turn pairing on.
            entry_data[CONF_PAIRING_OFF] = True
        return self.async_create_entry(
            title=data.get("hostname") or host, data=entry_data
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle an install found on the LAN via zeroconf.

        The TXT record carries device_id, mode, version, and hostname; the
        service port is the app port. We key the flow on device_id so a known
        install with a changed IP is updated silently, then show a confirm step
        naming the found install before running the same auth logic the user
        step uses.
        """

        props = discovery_info.properties or {}
        device_id = props.get("device_id")
        host = discovery_info.host
        port = discovery_info.port or DEFAULT_PORT
        hostname = props.get("hostname") or device_id or host

        if device_id:
            await self.async_set_unique_id(str(device_id))
            # A known install is only moved to the announced address once that
            # address answers as the same install. An install in a container
            # can announce an address Home Assistant cannot use (its own
            # container address with the published port), and moving a working
            # entry there would break it.
            updates = await self._async_verified_move(str(device_id), host, port)
            self._abort_if_unique_id_configured(updates=updates)

        self._discovered_host = host
        self._discovered_port = port
        self._discovered_name = hostname
        # Names the flow's card in the discovered list.
        self.context["title_placeholders"] = {"name": hostname}
        return await self.async_step_zeroconf_confirm()

    async def _async_verified_move(
        self, device_id: str, host: str, port: int
    ) -> dict[str, Any] | None:
        """The host and port to move a known install's entry to, or None to
        leave it where it is.

        None when no entry has this install, when the entry already uses this
        address, or when the announced address does not answer /ha/state as
        this same install (checked with the entry's own key)."""
        entry = next(
            (
                e
                for e in self._async_current_entries(include_ignore=False)
                if e.unique_id == device_id
            ),
            None,
        )
        if entry is None:
            return None
        if entry.data.get(CONF_HOST) == host and entry.data.get(CONF_PORT) == port:
            return None
        client = await self._async_client(host, port, entry.data.get(CONF_API_KEY))
        try:
            data = await client.async_get_state()
        except (PantryRaiderAuthError, PantryRaiderConnectionError):
            return None
        if not discovery_moves_entry(device_id, data):
            return None
        return {CONF_HOST: host, CONF_PORT: port}

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a discovered install, then connect or pair."""

        errors: dict[str, str] = {}
        if user_input is not None:
            # A discovered install is added keyless first; if it wants a key the
            # shared helper drops into the same pairing handshake as the user
            # step.
            result = await self._async_connect_or_pair(
                self._discovered_host, self._discovered_port, None, errors
            )
            if result is not None:
                return result
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"name": self._discovered_name},
        )

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for the kitchen-side approval of the pairing request.

        The form is just a "check now" button under the code: HA config flows
        cannot push, so the user approves on the kitchen (the on-screen toast
        or Settings, Bandit Remotes) and then submits here; pending simply
        re-shows the step. The request expires on the install after a few
        minutes, which answers as expired and sends the user back to try
        again. The install repeats the key until its first use or two minutes
        after it was first handed out, so an approval whose key did not arrive
        is asked about again before the user is asked to press Submit.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            client = await self._async_client(self._pair_host, self._pair_port, None)
            try:
                status = await client.async_pairing_status(self._pair_request_id)
            except (PantryRaiderAuthError, PantryRaiderConnectionError):
                errors["base"] = "cannot_connect"
            else:
                result = pairing_result(status)
                tries = 0
                while result == "approved_no_key" and tries < _KEY_RETRIES:
                    tries += 1
                    await asyncio.sleep(_KEY_RETRY_SECONDS)
                    try:
                        status = await client.async_pairing_status(
                            self._pair_request_id
                        )
                    except (PantryRaiderAuthError, PantryRaiderConnectionError):
                        break
                    result = pairing_result(status)
                if result == "approved":
                    key = status["api_key"]
                    keyed = await self._async_client(
                        self._pair_host, self._pair_port, key
                    )
                    try:
                        data = await keyed.async_get_state()
                    except (PantryRaiderAuthError, PantryRaiderConnectionError):
                        errors["base"] = "cannot_connect"
                    else:
                        if self.source == SOURCE_REAUTH:
                            return self._async_reauth_done(key)
                        return await self._async_finish(
                            self._pair_host, self._pair_port, key, data
                        )
                elif result == "approved_no_key":
                    errors["base"] = "pairing_key_missing"
                elif result == "pending":
                    errors["base"] = "pairing_pending"
                elif result == "denied":
                    return self.async_abort(reason="pairing_denied")
                else:
                    return self.async_abort(reason="pairing_expired")
        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"code": self._pair_code},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The install refused the key (or Repairs asked to pair): reconnect."""

        return await self.async_step_reauth_confirm()

    def _reauth_entry_now(self) -> ConfigEntry | None:
        # Looked up by id, which works on every Home Assistant release this
        # integration supports.
        return self.hass.config_entries.async_get_entry(self.context["entry_id"])

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair again (leave the key empty) or paste a key."""

        errors: dict[str, str] = {}
        entry = self._reauth_entry_now()
        if entry is None:
            return self.async_abort(reason="reauth_successful")
        if user_input is not None:
            host = entry.data[CONF_HOST]
            port = entry.data[CONF_PORT]
            key = (user_input.get(CONF_API_KEY) or "").strip()
            client = await self._async_client(host, port, key or None)
            if key:
                try:
                    await client.async_get_state()
                except PantryRaiderAuthError:
                    errors["base"] = "invalid_auth"
                except PantryRaiderConnectionError:
                    errors["base"] = "cannot_connect"
                else:
                    return self._async_reauth_done(key)
            else:
                self._pair_host = host
                self._pair_port = port
                try:
                    started = await client.async_request_pairing(
                        "Home Assistant", kind="ha"
                    )
                except (PantryRaiderAuthError, PantryRaiderNotSupported):
                    errors["base"] = "pairing_unavailable"
                except PantryRaiderConnectionError:
                    errors["base"] = "cannot_connect"
                else:
                    if started.get("ok") and started.get("request_id"):
                        self._pair_request_id = started["request_id"]
                        self._pair_code = str(started.get("code", ""))
                        return await self.async_step_pair()
                    errors["base"] = "pairing_unavailable"
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Optional(CONF_API_KEY, default=""): str}),
            errors=errors,
        )

    def _async_reauth_done(self, key: str) -> ConfigFlowResult:
        entry = self._reauth_entry_now()
        if entry is None:
            return self.async_abort(reason="reauth_successful")
        data = {**entry.data, CONF_API_KEY: key}
        data.pop(CONF_PAIRING_OFF, None)
        return self.async_update_reload_and_abort(
            entry, data=data, reason="reauth_successful"
        )

    @staticmethod
    def _default(user_input: dict[str, Any] | None, key: str, fallback: Any = "") -> Any:
        """Prefill a field with what the user last typed, or a fallback."""

        if user_input and user_input.get(key) is not None:
            return user_input[key]
        return fallback

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PantryRaiderOptionsFlow()


class PantryRaiderOptionsFlow(_OptionsBase):
    """Let the user tune the poll interval and the connect-back behavior."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            options = dict(self.config_entry.options)
            options[CONF_SCAN_INTERVAL] = user_input[CONF_SCAN_INTERVAL]
            was_on = options.get(CONF_CONNECT_BACK, True)
            now_on = user_input.get(CONF_CONNECT_BACK, True)
            options[CONF_CONNECT_BACK] = now_on
            # Turning connect-back back on (after it was off) is the user's way
            # to force a fresh token: clear the one-shot marker so the next
            # reload re-mints and re-posts /ha/connect. A plain interval change
            # leaves the marker set, so it does not re-mint every save.
            if now_on and not was_on:
                options[OPT_CONNECT_DONE] = False
            return self.async_create_entry(title="", data=options)

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        connect_back = self.config_entry.options.get(CONF_CONNECT_BACK, True)
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                ),
                vol.Required(CONF_CONNECT_BACK, default=connect_back): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
