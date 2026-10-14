"""Repairs fix flows for the Pantry Raider integration.

Two issues can be fixed from the Repairs page: an install that should be
paired (it answers Home Assistant without a key today, which stops once a
password is set) and an install whose device pairing was turned off. Both
fixes start the entry's Re-authenticate flow, which pairs again or takes a
pasted key. Every other issue is a note with no fix flow.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant

from .const import ISSUE_PAIR_NEEDED, ISSUE_PAIRING_OFF


class PantryRaiderPairFlow(RepairsFlow):
    """Confirm, then start pairing through the entry's reauth flow."""

    def __init__(self, entry_id: str | None) -> None:
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        if user_input is not None:
            entry = (
                self.hass.config_entries.async_get_entry(self._entry_id)
                if self._entry_id
                else None
            )
            if entry is not None:
                entry.async_start_reauth(self.hass)
            return self.async_create_entry(data={})
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Return the fix flow for a fixable Pantry Raider issue."""

    if issue_id.startswith(ISSUE_PAIR_NEEDED) or issue_id.startswith(ISSUE_PAIRING_OFF):
        return PantryRaiderPairFlow((data or {}).get("entry_id"))
    raise ValueError(f"Pantry Raider has no fix for {issue_id}")
