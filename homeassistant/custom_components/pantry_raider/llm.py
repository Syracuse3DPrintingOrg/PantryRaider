"""Tools an AI model conversation agent can call.

Home Assistant offers an integration's intents to an LLM agent only when the
integration says so here (the ``llm`` platform; registering an intent is not
enough). Without this, a request the built-in sentences miss goes to the
model, which has no way to reach Pantry Raider and answers from its own head.

The tools are the same intents voice.py registers for this entry: the recipe
one while recipe requests are on, the timer ones while timer requests are on.
"""

from __future__ import annotations

from homeassistant.components.llm import LLMTools
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import intent
from homeassistant.helpers.llm import LLM_API_ASSIST, IntentTool, LLMContext, Tool

from .const import DOMAIN
from .voice import VOICE_INTENTS

_PROMPT = (
    "Pantry Raider holds this home's recipes and kitchen timers. To show a "
    "recipe, call pantry_raider__ShowRecipe with the dish name as heard; do "
    "not recite a recipe from memory when one is asked for. If it answers "
    "with several recipes, ask which one."
)


@callback
def async_get_tools(
    hass: HomeAssistant, llm_context: LLMContext, api_id: str
) -> LLMTools | None:
    if api_id != LLM_API_ASSIST:
        return None
    tools: list[Tool] = [
        # Tool names carry the integration's name, as Home Assistant asks.
        IntentTool(f"{DOMAIN}__{handler.intent_type.removeprefix('PantryRaider')}", handler)
        for handler in intent.async_get(hass)
        if handler.intent_type in VOICE_INTENTS
    ]
    if not tools:
        return None
    return LLMTools(tools=tools, prompt=_PROMPT)
