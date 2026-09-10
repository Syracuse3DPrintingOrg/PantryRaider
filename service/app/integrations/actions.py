"""ActionProvider inhabitants: the named-action vocabulary (FoodAssistant-pjtq).

One token vocabulary drives every press surface: the Start Page's on-screen
keys, the Stream Deck's server-fired keys, the ESP button route
(POST /gadgets/esp-action), and BLE button esp_action mappings all funnel
into the same resolver. That resolver is the first (and today only)
ActionProvider: the Start Page provider, wrapping the execution logic that
lives in services.start_actions. services.start_actions.fire_key stays the
public entry point as a thin shim over ``fire_key`` here, so no caller
changed and every token answers exactly what it always has.

A future provider (a plug-in wanting its own tokens, FoodAssistant-czui)
would register here and be asked in turn; a token nobody claims reports the
same "Unknown key." it always has.
"""
from __future__ import annotations

from .interfaces import KIND_ACTIONS, ActionProvider

# The reply for a token no provider claims, byte-for-byte the answer
# services.start_actions.fire_key has always given.
UNKNOWN_KEY = {"ok": False, "detail": "Unknown key."}


class StartPageActions(ActionProvider):
    """The built-in vocabulary: HA slot keys (ha_1..ha_5), the cycle and
    preset timers, and the shared custom key overrides (HA action, media,
    macro, shopping add, timer)."""

    name = "start_page"
    label = "Start Page and deck actions"

    def enabled(self) -> bool:
        # Always on: the vocabulary is part of the app itself. Pieces that
        # need configuration (a Home Assistant connection, a configured
        # override) degrade inside the execution with the same honest
        # details they always have.
        return True

    async def fire(self, name: str, long: bool = False) -> dict | None:
        from ..services import start_actions
        return await start_actions.execute_token(name, long=long)


async def fire_key(name: str, long: bool = False) -> dict:
    """Execute a named action token through the registered providers.

    Providers are asked in registration order; the first non-None answer
    wins, and a token nobody claims is reported, never raised."""
    from . import registry
    for provider in registry.enabled_for(KIND_ACTIONS):
        result = await provider.fire(name, long=long)
        if result is not None:
            return result
    return dict(UNKNOWN_KEY)


def register_builtins() -> None:
    """Register the built-in provider (called by the registry)."""
    from . import registry
    registry.register(StartPageActions(), replace=True)
