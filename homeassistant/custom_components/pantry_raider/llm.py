"""Tools an AI model conversation agent can call.

Home Assistant offers an integration's intents to an LLM agent only when the
integration says so here (the ``llm`` platform; registering an intent is not
enough). Without this, a request the built-in sentences miss goes to the
model, which has no way to reach Pantry Raider and answers from its own head.

The recipe tools are this module's own Tool classes, not intent tools: an
intent tool hands the model only the intent response (a sentence to say and
lists of entities acted on), so the recipe itself, the recipes to choose
from and the timers on offer would never reach it. These return the data.
They are offered while recipe requests are on, the recipe timer included
even when timer sentences are off: starting the timer a recipe step names
is a recipe action, and a plain "set a timer for ten minutes" stays with
whatever already answers it. The page and timer tools are the intents
voice.py registers, offered while their requests are on.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.llm import LLMTools
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, intent
from homeassistant.helpers.llm import LLM_API_ASSIST, IntentTool, LLMContext, Tool, ToolInput
from homeassistant.util.json import JsonObjectType

from .const import DOMAIN
from .voice import (
    INTENT_SHOW_RECIPE,
    VOICE_INTENTS,
    _entry,
    async_cook,
    async_get_recipe,
    async_start_recipe_timer,
    satellite_for,
)
from .voice_helpers import OPT_RECIPE_SENTENCES, for_model, voice_options

_PROMPT = (
    "Pantry Raider holds this home's recipes and kitchen timers, and shows the "
    "recipe being cooked on the kitchen screen. "
    "When someone wants to cook or see a dish, call pantry_raider__ShowRecipe "
    "with the dish as heard (cook true when they want to cook it now, false "
    "when they ask to see the whole recipe, otherwise leave cook out); never "
    "recite a recipe from memory. If it answers "
    "with result ambiguous, read out the candidates, ask which one, and call "
    "it again with choice set to their answer (or slug set to the chosen "
    "candidate's slug). "
    "For ANY question about the dish being cooked (what to do first, what is "
    "next, how much of something, what temperature, how long), call "
    "pantry_raider__GetRecipe and answer only from what it returns. Steps are "
    "numbered from 1 and step is the one the cook is on. To move through the "
    "recipe pass step: next, back, repeat, first, last or a number; that also "
    "moves the marker on the screen, so use it for 'what's next' and 'go "
    "back'. If GetRecipe says no recipe is open, ask what they are cooking. "
    "To start a timer the recipe calls for ('start the timer for baking it', "
    "'start the step 4 timer'), call pantry_raider__StartRecipeTimer with the "
    "step number or the words said; with neither it starts the current "
    "step's timer. If it answers with choices, ask which one. Keep spoken "
    "answers short. "
    "To open or close Pantry Raider on the kitchen screen, call "
    "pantry_raider__ShowPage."
)


def _satellite(hass: HomeAssistant, llm_context: LLMContext) -> str:
    return satellite_for(hass, None, llm_context.device_id)


class ShowRecipeTool(Tool):
    name = f"{DOMAIN}__ShowRecipe"
    description = (
        "Open a recipe from the Pantry Raider library on the kitchen screen, "
        "to cook it or look at it. Give query as the dish was said. When "
        "several recipes fit, the answer has result 'ambiguous' and "
        "'candidates' (slug and title): ask which one, then call again with "
        "choice set to the answer as said ('the second one', 'the best-ever "
        "one') or slug set to the chosen candidate's slug. Set cook true only "
        "when they say they want to cook it now ('let's cook ...', 'I want to "
        "make ...'): the screen opens cook mode at step 1 and the answer reads "
        "step 1. Set cook false only when they ask to see the whole recipe "
        "('show me the ... recipe'). Otherwise leave cook out: a recipe "
        "already being cooked on that screen then stays in cook mode at its "
        "step, and any other opens as the whole recipe."
    )
    parameters = vol.Schema({
        vol.Optional("query"): cv.string,
        vol.Optional("choice"): cv.string,
        vol.Optional("slug"): cv.string,
        vol.Optional("cook"): cv.boolean,
    })

    async def async_call(
        self, hass: HomeAssistant, tool_input: ToolInput, llm_context: LLMContext
    ) -> JsonObjectType:
        args: dict[str, Any] = tool_input.tool_args
        # cook: true is cook mode, false the whole recipe, and left out the
        # install decides (a recipe already in cook mode on that screen stays
        # there, at its step).
        cook = args.get("cook")
        out = await async_cook(
            hass, str(args.get("query") or ""), str(args.get("choice") or ""),
            str(args.get("slug") or ""), satellite=_satellite(hass, llm_context),
            page="" if cook is None else ("cook" if cook else "recipe"))
        return for_model(out)


class GetRecipeTool(Tool):
    name = f"{DOMAIN}__GetRecipe"
    description = (
        "The recipe being cooked on the kitchen screen: title, servings, "
        "ingredients with amounts scaled to the servings chosen, steps "
        "numbered from 1 with their timers, and step (the one the cook is on, "
        "or null). Use it to answer any question about the dish being cooked. "
        "Pass step as next, back, repeat, first, last or a number to move the "
        "cook to that step (the screen follows); the answer's 'current' is "
        "that step and 'speech' reads it."
    )
    parameters = vol.Schema({vol.Optional("step"): cv.string})

    async def async_call(
        self, hass: HomeAssistant, tool_input: ToolInput, llm_context: LLMContext
    ) -> JsonObjectType:
        out = await async_get_recipe(
            hass, satellite=_satellite(hass, llm_context),
            step=str(tool_input.tool_args.get("step") or ""))
        return for_model(out)


class StartRecipeTimerTool(Tool):
    name = f"{DOMAIN}__StartRecipeTimer"
    description = (
        "Start a timer that a step of the recipe being cooked calls for, named "
        "after its action ('Bake crust'). Give step (a number from 1) or "
        "query (the words said, such as 'baking it' or 'the crust'); with "
        "neither, the current step's timer starts. An answer with result "
        "'ambiguous' lists 'choices' to ask about; 'running' means that timer "
        "is already counting down. For a plain timer of a given length that "
        "no recipe step names, do not use this tool."
    )
    parameters = vol.Schema({
        vol.Optional("step"): vol.Coerce(int),
        vol.Optional("query"): cv.string,
    })

    async def async_call(
        self, hass: HomeAssistant, tool_input: ToolInput, llm_context: LLMContext
    ) -> JsonObjectType:
        args = tool_input.tool_args
        step = args.get("step")
        out = await async_start_recipe_timer(
            hass, int(step) if step not in (None, "") else None,
            str(args.get("query") or ""), satellite=_satellite(hass, llm_context))
        return for_model(out)


@callback
def async_get_tools(
    hass: HomeAssistant, llm_context: LLMContext, api_id: str
) -> LLMTools | None:
    if api_id != LLM_API_ASSIST:
        return None
    entry = _entry(hass)
    if entry is None:
        return None
    tools: list[Tool] = []
    if voice_options(entry.options)[OPT_RECIPE_SENTENCES]:
        tools += [ShowRecipeTool(), GetRecipeTool(), StartRecipeTimerTool()]
    tools += [
        # Tool names carry the integration's name, as Home Assistant asks.
        IntentTool(f"{DOMAIN}__{handler.intent_type.removeprefix('PantryRaider')}", handler)
        for handler in intent.async_get(hass)
        if handler.intent_type in VOICE_INTENTS and handler.intent_type != INTENT_SHOW_RECIPE
    ]
    if not tools:
        return None
    return LLMTools(tools=tools, prompt=_PROMPT)
