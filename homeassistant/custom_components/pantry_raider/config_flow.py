"""Config and options flow for the Pantry Raider integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import (
    PantryRaiderAuthError,
    PantryRaiderClient,
    PantryRaiderConnectionError,
)
from .const import (
    CONF_CONNECT_BACK,
    CONF_SCAN_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    OPT_CONNECT_DONE,
)


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

    async def _async_connect_or_pair(
        self, host: str, port: int, api_key: str | None, errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        """Try to finish the flow for one install, or start pairing.

        Shared by the user step and the zeroconf confirm step so the two entry
        points cannot drift. Tries a keyless /ha/state; on an auth error with no
        key it opens the same pairing handshake a new Bandit uses
        (FoodAssistant-4box) and hands off to async_step_pair. Returns a flow
        result to return, or None with ``errors`` populated to re-show the form.
        """

        client = PantryRaiderClient(
            async_get_clientsession(self.hass),
            f"http://{host}:{port}",
            api_key,
        )
        try:
            data = await client.async_get_state()
        except PantryRaiderAuthError:
            if api_key is None:
                # No key and the install wants one: instead of failing, start
                # the pairing handshake. The kitchen shows a four digit code;
                # the user approves it there and this flow collects the key.
                self._pair_host = host
                self._pair_port = port
                try:
                    started = await client.async_request_pairing("Home Assistant")
                except PantryRaiderAuthError:
                    # Pairing is off: fall back to asking for a key.
                    errors["base"] = "pairing_unavailable"
                except PantryRaiderConnectionError:
                    errors["base"] = "cannot_connect"
                else:
                    if started.get("ok") and started.get("request_id"):
                        self._pair_request_id = started["request_id"]
                        self._pair_code = str(started.get("code", ""))
                        return await self.async_step_pair()
                    errors["base"] = "pairing_unavailable"
            else:
                errors["base"] = "invalid_auth"
        except PantryRaiderConnectionError:
            errors["base"] = "cannot_connect"
        else:
            # Tie the entry to the install's own device_id so re-adding the same
            # box (or a zeroconf re-announce from a new IP) updates it instead
            # of duplicating.
            device_id = data.get("device_id") or f"{host}:{port}"
            await self.async_set_unique_id(str(device_id))
            self._abort_if_unique_id_configured(
                updates={CONF_HOST: host, CONF_PORT: port}
            )
            title = data.get("hostname") or host
            return self.async_create_entry(
                title=title,
                data={
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_API_KEY: api_key,
                },
            )
        return None

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
            self._abort_if_unique_id_configured(
                updates={CONF_HOST: host, CONF_PORT: port}
            )

        self._discovered_host = host
        self._discovered_port = port
        self._discovered_name = hostname
        # Names the flow's card in the discovered list.
        self.context["title_placeholders"] = {"name": hostname}
        return await self.async_step_zeroconf_confirm()

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
        or Settings, Devices) and then submits here; pending simply re-shows
        the step. The request expires server-side after a few minutes, which
        answers as expired and sends the user back to try again.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            client = PantryRaiderClient(
                async_get_clientsession(self.hass),
                f"http://{self._pair_host}:{self._pair_port}",
                None,
            )
            try:
                status = await client.async_pairing_status(self._pair_request_id)
            except (PantryRaiderAuthError, PantryRaiderConnectionError):
                errors["base"] = "cannot_connect"
            else:
                state = status.get("status", "expired")
                if state == "approved" and status.get("api_key"):
                    key = status["api_key"]
                    keyed = PantryRaiderClient(
                        async_get_clientsession(self.hass),
                        f"http://{self._pair_host}:{self._pair_port}",
                        key,
                    )
                    try:
                        data = await keyed.async_get_state()
                    except (PantryRaiderAuthError, PantryRaiderConnectionError):
                        errors["base"] = "cannot_connect"
                    else:
                        device_id = data.get("device_id") or (
                            f"{self._pair_host}:{self._pair_port}")
                        await self.async_set_unique_id(str(device_id))
                        self._abort_if_unique_id_configured()
                        return self.async_create_entry(
                            title=data.get("hostname") or self._pair_host,
                            data={
                                CONF_HOST: self._pair_host,
                                CONF_PORT: self._pair_port,
                                CONF_API_KEY: key,
                            },
                        )
                elif state == "pending":
                    errors["base"] = "pairing_pending"
                elif state == "denied":
                    return self.async_abort(reason="pairing_denied")
                else:
                    return self.async_abort(reason="pairing_expired")
        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"code": self._pair_code},
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


class PantryRaiderOptionsFlow(OptionsFlow):
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
