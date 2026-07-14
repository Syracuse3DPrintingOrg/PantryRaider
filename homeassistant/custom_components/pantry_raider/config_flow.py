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

from .api import (
    PantryRaiderAuthError,
    PantryRaiderClient,
    PantryRaiderConnectionError,
)
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
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
            client = PantryRaiderClient(
                async_get_clientsession(self.hass),
                f"http://{host}:{port}",
                api_key,
            )
            try:
                data = await client.async_get_state()
            except PantryRaiderAuthError:
                errors["base"] = "invalid_auth"
            except PantryRaiderConnectionError:
                errors["base"] = "cannot_connect"
            else:
                # Tie the entry to the install's own device_id so re-adding the
                # same box updates it instead of duplicating.
                device_id = data.get("device_id") or f"{host}:{port}"
                await self.async_set_unique_id(str(device_id))
                self._abort_if_unique_id_configured()
                title = data.get("hostname") or host
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_API_KEY: api_key,
                    },
                )

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
    """Let the user tune the poll interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
