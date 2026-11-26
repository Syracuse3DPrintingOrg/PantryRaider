"""Recipes and timers by voice, aimed at one screen.

Four pieces, all thin wrappers over voice_helpers.py and the app's routes:

- Services (pantry_raider.show_recipe, start_timer, cancel_timer,
  dismiss_timer, timer_status, show_page, get_routing), each answering with
  data that includes a ``speech`` line, so an automation's own sentences can
  call them and say the answer.
- Intents with the same actions, which is how an LLM conversation agent
  reaches them when speech to text misses a sentence.
- Sentences for the built-in agent: recipes on by default, timers only when
  chosen, because many houses already have something answering "set a timer".
- A watcher that long-polls the install's timer events, fires
  ``pantry_raider_timer`` on the event bus for each, and rings the voice
  satellite that set a timer until it is dismissed anywhere.

Each voice satellite has its own choices (entry options, see the options
flow): timers to Home Assistant or Pantry Raider, recipes on or off, and
which screen shows what it is asked for.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, intent

from .api import (
    PantryRaiderAuthError,
    PantryRaiderClient,
    PantryRaiderConnectionError,
    PantryRaiderNotSupported,
)
from .const import DOMAIN
from .voice_helpers import (
    OPT_RECIPE_SENTENCES,
    OPT_TIMER_SENTENCES,
    RING_EVERY,
    RING_TIMES,
    SAT_RECIPES,
    SAT_SCREEN,
    SAT_TIMERS,
    TIMERS_PANTRY_RAIDER,
    parse_duration,
    pick_timers,
    ring_message,
    routing_for,
    should_ring,
    split_timer_request,
    spoken_duration,
    status_speech,
    timer_label,
    voice_options,
)

_LOGGER = logging.getLogger(__name__)

EVENT_TIMER = "pantry_raider_timer"

SERVICE_SHOW_RECIPE = "show_recipe"
SERVICE_START_TIMER = "start_timer"
SERVICE_CANCEL_TIMER = "cancel_timer"
SERVICE_DISMISS_TIMER = "dismiss_timer"
SERVICE_TIMER_STATUS = "timer_status"
SERVICE_SHOW_PAGE = "show_page"
SERVICE_GET_ROUTING = "get_routing"
VOICE_SERVICES = (
    SERVICE_SHOW_RECIPE,
    SERVICE_START_TIMER,
    SERVICE_CANCEL_TIMER,
    SERVICE_DISMISS_TIMER,
    SERVICE_TIMER_STATUS,
    SERVICE_SHOW_PAGE,
    SERVICE_GET_ROUTING,
)

INTENT_SHOW_RECIPE = "PantryRaiderShowRecipe"
INTENT_START_TIMER = "PantryRaiderStartTimer"
INTENT_CANCEL_TIMER = "PantryRaiderCancelTimer"
INTENT_STOP_TIMER = "PantryRaiderStopTimer"
INTENT_TIMER_STATUS = "PantryRaiderTimerStatus"
INTENT_SHOW_PAGE = "PantryRaiderShowPage"
VOICE_INTENTS = (
    INTENT_SHOW_RECIPE,
    INTENT_SHOW_PAGE,
    INTENT_START_TIMER,
    INTENT_CANCEL_TIMER,
    INTENT_STOP_TIMER,
    INTENT_TIMER_STATUS,
)

_TOO_OLD = "Pantry Raider needs an update before it can do that."
_UNREACHABLE = "I could not reach Pantry Raider."

RECIPE_SENTENCES = [
    "(show|open|display|find|pull up|bring up) [me] [the|my|our|a] recipe for {query}",
    "(show|open|display|find|pull up|bring up) [me] [the|my|our|a] {query} recipe",
    "(show|open|display|find|pull up|bring up) [me] [the|my|our|a] {query} recipe on [the] screen",
    "(show|open|display|find|pull up|bring up) [me] [the|my|our|a] {query} recipe (from|in|on) pantry raider",
    "(show|open|display|find|pull up|bring up) [me] [the|my|our|a] recipe for {query} (from|in|on) pantry raider",
    "[the|my|our] {query} recipe (from|in|on) pantry raider",
]
CLOSE_SENTENCES = [
    "(close|hide|put away) [the] recipe",
    "(close|hide) [the] (kitchen|pantry raider) screen",
    "(close|hide|exit|leave) pantry raider",
]
APP_SENTENCES = [
    "(show|open|start|launch|bring up|pull up) pantry raider",
    "(show|open|start|launch|bring up|pull up) pantry raider on [the] screen",
    "(switch|go) to pantry raider [mode]",
]
PAGES = ("app", "home", "recipe", "timers", "close")
START_TIMER_SENTENCES = [
    "(set|start|create) [a|an] timer for {request}",
    "(set|start|create) [a|an] {request} timer",
    "(set|start|create) [a|an] {name} timer for {request}",
    "timer for {request}",
]
STATUS_SENTENCES = [
    "how (long|much time) is left",
    "how (long|much time) is left on [the|my] timer[s]",
    "how (long|much time) is left on [the|my] {name} timer",
    "how (long|much time) [is|do i have] left on [the|my] {name}",
]
CANCEL_SENTENCES = [
    "(cancel|delete|remove) [the|my] timer",
    "(cancel|delete|remove) [the|my] {name} timer",
]
CANCEL_ALL_SENTENCES = ["(cancel|delete|remove) all [the|my|of the|of my] timers"]
STOP_SENTENCES = [
    "(stop|dismiss|silence|turn off) [the|my] (timer|timers|alarm)",
    "(stop|dismiss|silence|turn off) [the|my] {name} timer",
]


# --- Which install, and what a satellite is set to ---------------------------


def _entry(hass: HomeAssistant):
    """The loaded Pantry Raider entry voice acts on (the first one)."""
    from homeassistant.config_entries import ConfigEntryState

    # An entry that is part way through loading (a reload after an options
    # change, still looking up its Bandits) can already answer.
    for wanted in (ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS):
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is wanted and getattr(entry, "runtime_data", None) is not None:
                return entry
    return None


def _client(hass: HomeAssistant) -> PantryRaiderClient:
    entry = _entry(hass)
    if entry is None:
        raise HomeAssistantError("Pantry Raider is not set up.")
    return entry.runtime_data.main.client


def _routing(hass: HomeAssistant, satellite: str | None) -> dict[str, Any]:
    entry = _entry(hass)
    return routing_for(entry.options if entry else {}, satellite)


async def _guard(coro) -> dict[str, Any]:
    """Run one call to the install; a failure becomes an answer to say."""
    try:
        return await coro
    except PantryRaiderNotSupported:
        return {"ok": False, "result": "too_old", "speech": _TOO_OLD}
    except PantryRaiderAuthError:
        return {"ok": False, "result": "key_rejected",
                "speech": "Pantry Raider did not accept Home Assistant's key."}
    except PantryRaiderConnectionError:
        return {"ok": False, "result": "unreachable", "speech": _UNREACHABLE}


async def _refresh(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    if entry is not None:
        await entry.runtime_data.main.async_request_refresh()


# --- The actions (shared by services, intents and sentences) ----------------


async def async_show_recipe(
    hass: HomeAssistant, query: str, screen: str = "", satellite: str = ""
) -> dict[str, Any]:
    route = _routing(hass, satellite)
    if satellite and not route[SAT_RECIPES]:
        return {"ok": False, "result": "off",
                "speech": "Recipes are turned off for this device."}
    target = screen or route[SAT_SCREEN]

    async def run() -> dict[str, Any]:
        out = await _client(hass).async_show_recipe(query, target)
        if "speech" not in out:
            out["speech"] = str(out.get("error") or "I could not show that recipe.")
        return out

    return await _guard(run())


async def async_start_timer(
    hass: HomeAssistant, duration: Any, name: str = "", screen: str = "",
    satellite: str = "",
) -> dict[str, Any]:
    seconds = parse_duration(duration)
    if not name and isinstance(duration, str):
        name = split_timer_request(duration)[1]
    if seconds <= 0:
        return {"ok": False, "result": "no_duration",
                "speech": "How long should the timer be?"}
    route = _routing(hass, satellite)
    label = timer_label(name, seconds)

    async def run() -> dict[str, Any]:
        out = await _client(hass).async_start_timer(
            label, seconds, origin=satellite, screen=screen or route[SAT_SCREEN])
        said = spoken_duration(seconds)
        speech = (f"{label} timer set for {said}." if name else f"Timer set for {said}.")
        return {"ok": True, "result": "started", "timer": out.get("timer"),
                "screen": out.get("screen"), "speech": speech}

    out = await _guard(run())
    await _refresh(hass)
    return out


async def async_cancel_timer(
    hass: HomeAssistant, name: str = "", timer_id: int | None = None,
    cancel_all: bool = False, satellite: str = "",
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        client = _client(hass)
        if timer_id is not None:
            gone = await client.async_delete_timer(timer_id)
            return {"ok": gone, "result": "cancelled" if gone else "not_found",
                    "cancelled": 1 if gone else 0,
                    "speech": "Timer cancelled." if gone else "I could not find that timer."}
        timers = await client.async_timers()
        if cancel_all:
            picked = pick_timers(timers)
        else:
            picked = pick_timers(timers, name, origin=satellite, ringing=False)
            if not picked:
                # "Cancel the timer" while one is ringing means that one.
                picked = pick_timers(timers, name, origin=satellite, ringing=True)
            if len(picked) > 1 and not name:
                names = ", ".join(str(t.get("label")) for t in picked[:4])
                return {"ok": False, "result": "ambiguous", "cancelled": 0,
                        "speech": f"Which timer? There are {len(picked)}: {names}."}
        if not picked:
            return {"ok": False, "result": "not_found", "cancelled": 0,
                    "speech": "There is no timer running." if not name
                    else f"I could not find a {name} timer."}
        for t in picked:
            await client.async_delete_timer(int(t["id"]))
        if len(picked) == 1:
            speech = f"{picked[0].get('label', 'Timer')} cancelled."
        else:
            speech = f"{len(picked)} timers cancelled."
        return {"ok": True, "result": "cancelled", "cancelled": len(picked), "speech": speech}

    out = await _guard(run())
    await _refresh(hass)
    return out


async def async_dismiss_timer(
    hass: HomeAssistant, timer_id: int | None = None, satellite: str = "",
) -> dict[str, Any]:
    """Stop the ringing: one finished timer by id, or every finished one."""

    async def run() -> dict[str, Any]:
        client = _client(hass)
        if timer_id is not None:
            body = await client.async_call(
                "POST", f"/timers/{int(timer_id)}/dismiss", ok_statuses=(200, 404, 409))
            ok = bool(body.get("ok"))
            return {"ok": ok, "result": "dismissed" if ok else "not_ringing",
                    "dismissed": 1 if ok else 0,
                    "speech": "Timer dismissed." if ok else "That timer is not ringing."}
        # Whoever says stop wants the noise to end, whichever device set it.
        count = await client.async_dismiss_timers()
        return {"ok": count > 0, "result": "dismissed" if count else "not_ringing",
                "dismissed": count,
                "speech": "Timer dismissed." if count else "No timer is ringing."}

    out = await _guard(run())
    _stop_ringing(hass)
    await _refresh(hass)
    return out


async def async_timer_status(
    hass: HomeAssistant, name: str = "", satellite: str = ""
) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        timers = await _client(hass).async_timers()
        picked = pick_timers(timers, name, origin=satellite)
        if name and not picked:
            return {"ok": False, "result": "not_found", "timers": [],
                    "speech": f"I could not find a {name} timer."}
        return {"ok": True, "result": "status", "timers": picked,
                "speech": status_speech(picked)}

    return await _guard(run())


async def async_show_page(
    hass: HomeAssistant, page: str, screen: str = "", satellite: str = ""
) -> dict[str, Any]:
    target = screen or _routing(hass, satellite)[SAT_SCREEN]
    if not target:
        return {"ok": False, "result": "no_screen",
                "speech": "No screen is set for this device."}

    async def run() -> dict[str, Any]:
        out = await _client(hass).async_show_page(page, target)
        ok = bool(out.get("ok"))
        return {"ok": ok, "result": "shown" if ok else "refused",
                "screen": out.get("screen"),
                "speech": "Done." if ok else str(out.get("error") or "That did not work.")}

    return await _guard(run())


async def async_get_routing(hass: HomeAssistant, satellite: str) -> dict[str, Any]:
    route = _routing(hass, satellite)
    label = ""
    if route[SAT_SCREEN]:
        try:
            for s in await _client(hass).async_screens():
                if s.get("id") == route[SAT_SCREEN]:
                    label = str(s.get("label") or "")
        except (PantryRaiderAuthError, PantryRaiderConnectionError, HomeAssistantError):
            label = ""
    return {"satellite": satellite, "timers": route[SAT_TIMERS],
            "recipes": route[SAT_RECIPES], "screen": route[SAT_SCREEN],
            "screen_label": label}


# --- Services ----------------------------------------------------------------



def _schema(fields: dict) -> vol.Schema:
    return vol.Schema(fields)


@callback
def async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SHOW_RECIPE):
        return

    def register(name: str, schema: vol.Schema, handler) -> None:
        async def run(call: ServiceCall) -> ServiceResponse:
            out = await handler(call.data)
            return out if call.return_response else None

        hass.services.async_register(
            DOMAIN, name, run, schema=schema,
            supports_response=SupportsResponse.OPTIONAL)

    register(SERVICE_SHOW_RECIPE, _schema({
        vol.Required("query"): cv.string,
        vol.Optional("screen", default=""): cv.string,
        vol.Optional("satellite", default=""): cv.string,
    }), lambda d: async_show_recipe(hass, d["query"], d["screen"], d["satellite"]))

    register(SERVICE_START_TIMER, _schema({
        vol.Required("duration"): vol.Any(vol.Coerce(int), cv.string, dict),
        vol.Optional("name", default=""): cv.string,
        vol.Optional("screen", default=""): cv.string,
        vol.Optional("satellite", default=""): cv.string,
    }), lambda d: async_start_timer(hass, d["duration"], d["name"], d["screen"],
                                    d["satellite"]))

    register(SERVICE_CANCEL_TIMER, _schema({
        vol.Optional("name", default=""): cv.string,
        vol.Optional("timer_id"): vol.Coerce(int),
        vol.Optional("all", default=False): cv.boolean,
        vol.Optional("satellite", default=""): cv.string,
    }), lambda d: async_cancel_timer(hass, d["name"], d.get("timer_id"), d["all"],
                                     d["satellite"]))

    register(SERVICE_DISMISS_TIMER, _schema({
        vol.Optional("timer_id"): vol.Coerce(int),
        vol.Optional("all", default=True): cv.boolean,
        vol.Optional("satellite", default=""): cv.string,
    }), lambda d: async_dismiss_timer(hass, d.get("timer_id"), d["satellite"]))

    register(SERVICE_TIMER_STATUS, _schema({
        vol.Optional("name", default=""): cv.string,
        vol.Optional("satellite", default=""): cv.string,
    }), lambda d: async_timer_status(hass, d["name"], d["satellite"]))

    register(SERVICE_SHOW_PAGE, _schema({
        vol.Required("page"): vol.In(PAGES),
        vol.Optional("screen", default=""): cv.string,
        vol.Optional("satellite", default=""): cv.string,
    }), lambda d: async_show_page(hass, d["page"], d["screen"], d["satellite"]))

    register(SERVICE_GET_ROUTING, _schema({
        vol.Required("satellite"): cv.string,
    }), lambda d: async_get_routing(hass, d["satellite"]))


@callback
def async_remove_services(hass: HomeAssistant) -> None:
    for name in VOICE_SERVICES:
        hass.services.async_remove(DOMAIN, name)


# --- Intents (how an LLM agent reaches the same actions) ---------------------


def _slot(intent_obj: intent.Intent, name: str) -> Any:
    value = intent_obj.slots.get(name, {})
    return value.get("value") if isinstance(value, dict) else None


def _say(intent_obj: intent.Intent, out: dict[str, Any]) -> intent.IntentResponse:
    response = intent_obj.create_response()
    response.async_set_speech(str(out.get("speech") or "Done."))
    return response


async def _builtin(intent_obj: intent.Intent, name: str, slots: dict) -> intent.IntentResponse:
    """Hand a timer request to Home Assistant's own timers, for a device
    whose timers stay there."""
    return await intent.async_handle(
        intent_obj.hass, DOMAIN, name,
        {k: {"value": v} for k, v in slots.items() if v not in (None, "", 0)},
        text_input=intent_obj.text_input, context=intent_obj.context,
        language=intent_obj.language, assistant=intent_obj.assistant,
        device_id=intent_obj.device_id, satellite_id=intent_obj.satellite_id,
        conversation_agent_id=intent_obj.conversation_agent_id)


def _satellite(intent_obj: intent.Intent) -> str:
    """The voice device a request came from. A sentence carries it; a tool
    call from an AI model carries only the device, so its assist_satellite
    entity is looked up."""
    if intent_obj.satellite_id:
        return intent_obj.satellite_id
    if not intent_obj.device_id:
        return ""
    from homeassistant.helpers import entity_registry as er

    for entry in er.async_entries_for_device(
        er.async_get(intent_obj.hass), intent_obj.device_id
    ):
        if entry.domain == "assist_satellite":
            return entry.entity_id
    return ""


def _to_pantry_raider(intent_obj: intent.Intent) -> bool:
    """Whether this device's timers run in Pantry Raider. A request with no
    device (a typed one, a script) does: it asked for Pantry Raider by name."""
    sat = _satellite(intent_obj)
    if not sat:
        return True
    return _routing(intent_obj.hass, sat)[SAT_TIMERS] == TIMERS_PANTRY_RAIDER


class ShowRecipeIntent(intent.IntentHandler):
    intent_type = INTENT_SHOW_RECIPE
    description = (
        "Show a recipe from the Pantry Raider recipe library on the kitchen "
        "screen. Use when someone asks to see, open, find or pull up a recipe, "
        "or asks how to make a dish they have a recipe for."
    )
    slot_schema = {vol.Required("query"): cv.string}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        slots = self.async_validate_slots(intent_obj.slots)
        out = await async_show_recipe(
            intent_obj.hass, slots["query"]["value"], satellite=_satellite(intent_obj))
        return _say(intent_obj, out)


class ShowPageIntent(intent.IntentHandler):
    intent_type = INTENT_SHOW_PAGE
    description = (
        "Show Pantry Raider on the kitchen screen, or close it. Use page "
        "'app' to open Pantry Raider (the whole kitchen app), 'timers' for "
        "the timers, 'recipe' for the recipe being cooked, and 'close' to "
        "close Pantry Raider or put the screen back."
    )
    slot_schema = {vol.Required("page"): vol.In(PAGES)}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        slots = self.async_validate_slots(intent_obj.slots)
        out = await async_show_page(
            intent_obj.hass, slots["page"]["value"], satellite=_satellite(intent_obj))
        return _say(intent_obj, out)


class StartTimerIntent(intent.IntentHandler):
    intent_type = INTENT_START_TIMER
    description = (
        "Start a kitchen timer that shows on the Pantry Raider screens. Give "
        "the length as hours, minutes and seconds, and a name if one was said."
    )
    slot_schema = {
        vol.Optional("hours"): vol.Coerce(int),
        vol.Optional("minutes"): vol.Coerce(int),
        vol.Optional("seconds"): vol.Coerce(int),
        vol.Optional("name"): cv.string,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        slots = self.async_validate_slots(intent_obj.slots)
        parts = {k: (slots.get(k) or {}).get("value") for k in
                 ("hours", "minutes", "seconds", "name")}
        if not _to_pantry_raider(intent_obj):
            return await _builtin(intent_obj, "HassStartTimer", parts)
        out = await async_start_timer(
            intent_obj.hass,
            {k: parts[k] or 0 for k in ("hours", "minutes", "seconds")},
            parts["name"] or "", satellite=_satellite(intent_obj))
        return _say(intent_obj, out)


class CancelTimerIntent(intent.IntentHandler):
    intent_type = INTENT_CANCEL_TIMER
    description = (
        "Cancel a Pantry Raider kitchen timer by name, the only one running, "
        "or all of them."
    )
    slot_schema = {vol.Optional("name"): cv.string, vol.Optional("all"): cv.boolean}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        slots = self.async_validate_slots(intent_obj.slots)
        name = (slots.get("name") or {}).get("value") or ""
        every = bool((slots.get("all") or {}).get("value"))
        if not _to_pantry_raider(intent_obj):
            return await _builtin(
                intent_obj, "HassCancelAllTimers" if every else "HassCancelTimer",
                {} if every else {"name": name})
        out = await async_cancel_timer(
            intent_obj.hass, name, cancel_all=every, satellite=_satellite(intent_obj))
        return _say(intent_obj, out)


class StopTimerIntent(intent.IntentHandler):
    intent_type = INTENT_STOP_TIMER
    description = (
        "Stop a Pantry Raider kitchen timer that has finished and is ringing. "
        "Use for stop, dismiss or silence the timer or the alarm."
    )

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        out = await async_dismiss_timer(
            intent_obj.hass, satellite=_satellite(intent_obj))
        return _say(intent_obj, out)


class TimerStatusIntent(intent.IntentHandler):
    intent_type = INTENT_TIMER_STATUS
    description = "Say how much time is left on the Pantry Raider kitchen timers."
    slot_schema = {vol.Optional("name"): cv.string}

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        slots = self.async_validate_slots(intent_obj.slots)
        name = (slots.get("name") or {}).get("value") or ""
        if not _to_pantry_raider(intent_obj):
            return await _builtin(intent_obj, "HassTimerStatus", {"name": name})
        out = await async_timer_status(
            intent_obj.hass, name, satellite=_satellite(intent_obj))
        return _say(intent_obj, out)


# --- Sentences for the built-in agent ----------------------------------------


def _agent_manager(hass: HomeAssistant):
    """Whatever holds sentence triggers on this Home Assistant release."""
    try:
        from homeassistant.components.conversation.agent_manager import (
            get_agent_manager,
        )

        return get_agent_manager(hass)
    except ImportError:
        from homeassistant.components import conversation

        return conversation.async_get_agent(hass)


def _said(result: Any, name: str) -> str:
    entity = getattr(result, "entities", {}).get(name)
    text = getattr(entity, "text", None) or getattr(entity, "value", None) or ""
    return str(text).strip()


async def _speech_of(response: intent.IntentResponse) -> str:
    speech = getattr(response, "speech", {}) or {}
    return str((speech.get("plain") or {}).get("speech") or "")


def _satellite_of(user_input: Any) -> str:
    return str(getattr(user_input, "satellite_id", None) or "")


async def _ha_timer(hass: HomeAssistant, user_input: Any, name: str, slots: dict) -> str:
    """A timer sentence from a device whose timers stay in Home Assistant."""
    try:
        response = await intent.async_handle(
            hass, DOMAIN, name,
            {k: {"value": v} for k, v in slots.items() if v not in (None, "", 0)},
            text_input=getattr(user_input, "text", None),
            context=getattr(user_input, "context", None),
            language=getattr(user_input, "language", None),
            device_id=getattr(user_input, "device_id", None),
            satellite_id=getattr(user_input, "satellite_id", None))
    except intent.IntentError as err:
        return str(err) or "That did not work."
    return await _speech_of(response)


@callback
def async_register_voice(hass: HomeAssistant, entry) -> None:
    """Register the intents and, as the options say, the sentences. Undone
    when the entry unloads, so a changed option takes effect on reload."""
    options = voice_options(entry.options)

    handlers: list[intent.IntentHandler] = []
    if options[OPT_RECIPE_SENTENCES]:
        handlers += [ShowRecipeIntent(), ShowPageIntent()]
    if options[OPT_TIMER_SENTENCES]:
        handlers += [StartTimerIntent(), CancelTimerIntent(), StopTimerIntent(),
                     TimerStatusIntent()]
    for handler in handlers:
        intent.async_register(hass, handler)

    @callback
    def drop_intents() -> None:
        registered = hass.data.get(intent.DATA_KEY, {})
        for handler in handlers:
            if registered.get(handler.intent_type) is handler:
                registered.pop(handler.intent_type, None)

    entry.async_on_unload(drop_intents)

    try:
        manager = _agent_manager(hass)
    except Exception:  # noqa: BLE001 - no conversation agent: services still work
        _LOGGER.debug("No conversation agent to give sentences to", exc_info=True)
        return

    def listen(sentences: list[str], handler) -> None:
        async def run(user_input: Any, result: Any) -> str | None:
            try:
                return await handler(user_input, result)
            except Exception:  # noqa: BLE001 - a spoken request gets an answer
                _LOGGER.exception("A Pantry Raider voice request failed")
                return "Something went wrong with Pantry Raider."

        try:
            entry.async_on_unload(manager.register_trigger(sentences, run))
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Could not register Pantry Raider sentences", exc_info=True)

    if options[OPT_RECIPE_SENTENCES]:
        async def recipe(user_input: Any, result: Any) -> str:
            out = await async_show_recipe(
                hass, _said(result, "query"), satellite=_satellite_of(user_input))
            return out["speech"]

        async def close(user_input: Any, result: Any) -> str:
            out = await async_show_page(hass, "close", satellite=_satellite_of(user_input))
            return "Closed." if out["ok"] else out["speech"]

        async def whole_app(user_input: Any, result: Any) -> str:
            out = await async_show_page(hass, "app", satellite=_satellite_of(user_input))
            return "Opening Pantry Raider." if out["ok"] else out["speech"]

        listen(RECIPE_SENTENCES, recipe)
        listen(CLOSE_SENTENCES, close)
        listen(APP_SENTENCES, whole_app)

    if not options[OPT_TIMER_SENTENCES]:
        return

    def mine(user_input: Any) -> bool:
        sat = _satellite_of(user_input)
        return not sat or _routing(hass, sat)[SAT_TIMERS] == TIMERS_PANTRY_RAIDER

    async def start(user_input: Any, result: Any) -> str:
        said = " ".join(p for p in (_said(result, "name"), _said(result, "request")) if p)
        seconds, name = split_timer_request(said)
        if not mine(user_input):
            h, rest = divmod(seconds, 3600)
            return await _ha_timer(hass, user_input, "HassStartTimer", {
                "hours": h, "minutes": rest // 60, "seconds": rest % 60, "name": name})
        out = await async_start_timer(hass, seconds, name, satellite=_satellite_of(user_input))
        return out["speech"]

    async def status(user_input: Any, result: Any) -> str:
        name = _said(result, "name")
        if not mine(user_input):
            return await _ha_timer(hass, user_input, "HassTimerStatus", {"name": name})
        return (await async_timer_status(hass, name, _satellite_of(user_input)))["speech"]

    async def cancel(user_input: Any, result: Any) -> str:
        name = _said(result, "name")
        if not mine(user_input):
            return await _ha_timer(hass, user_input, "HassCancelTimer", {"name": name})
        out = await async_cancel_timer(hass, name, satellite=_satellite_of(user_input))
        return out["speech"]

    async def cancel_all(user_input: Any, result: Any) -> str:
        if not mine(user_input):
            return await _ha_timer(hass, user_input, "HassCancelAllTimers", {})
        out = await async_cancel_timer(hass, cancel_all=True,
                                       satellite=_satellite_of(user_input))
        return out["speech"]

    async def stop(user_input: Any, result: Any) -> str:
        out = await async_dismiss_timer(hass, satellite=_satellite_of(user_input))
        return out["speech"]

    listen(START_TIMER_SENTENCES, start)
    listen(STATUS_SENTENCES, status)
    listen(CANCEL_SENTENCES, cancel)
    listen(CANCEL_ALL_SENTENCES, cancel_all)
    listen(STOP_SENTENCES, stop)


# --- Timer events and ringing ------------------------------------------------

_RINGING = "pantry_raider_ringing"


def _stop_ringing(hass: HomeAssistant, timer_id: int | None = None) -> None:
    tasks: dict[int, asyncio.Task] = hass.data.get(_RINGING, {})
    for tid in [timer_id] if timer_id is not None else list(tasks):
        task = tasks.pop(tid, None)
        if task is not None:
            task.cancel()


async def _ring(hass: HomeAssistant, satellite: str, timer: dict[str, Any]) -> None:
    """Announce a finished timer on the satellite that set it, again and
    again, until its task is cancelled (the timer was dismissed or cancelled)
    or ten minutes pass."""
    message = ring_message(timer)
    for _ in range(RING_TIMES):
        if hass.states.get(satellite) is None:
            return
        try:
            await hass.services.async_call(
                "assist_satellite", "announce",
                {"entity_id": satellite, "message": message}, blocking=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a busy or offline speaker: try again
            _LOGGER.debug("Could not announce on %s", satellite, exc_info=True)
        await asyncio.sleep(RING_EVERY)


def _start_ringing(hass: HomeAssistant, entry, satellite: str, timer: dict[str, Any]) -> None:
    tasks: dict[int, asyncio.Task] = hass.data.setdefault(_RINGING, {})
    try:
        tid = int(timer.get("id"))
    except (TypeError, ValueError):
        return
    if tid in tasks and not tasks[tid].done():
        return
    task = entry.async_create_background_task(
        hass, _ring(hass, satellite, timer), f"{DOMAIN} ring {tid}")
    tasks[tid] = task
    task.add_done_callback(lambda _t: tasks.pop(tid, None) if tasks.get(tid) is _t else None)


async def _watch_timers(hass: HomeAssistant, entry) -> None:
    """Follow the install's timer events for as long as the entry is loaded."""
    client: PantryRaiderClient = entry.runtime_data.main.client
    since = 999999999
    first = True
    delay = 1.0
    while True:
        try:
            data = await client.async_timer_events(since, 0 if first else 25)
        except PantryRaiderNotSupported:
            await asyncio.sleep(1800)  # an older install: look again later
            continue
        except (PantryRaiderAuthError, PantryRaiderConnectionError):
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
            continue
        delay = 1.0
        last = int(data.get("last_id") or 0)
        events = [] if first else [e for e in data.get("events") or [] if isinstance(e, dict)]
        restarted = not first and last < since
        if first or restarted:
            # Starting up (either end): ring for anything that finished in the
            # last ten minutes and is still waiting to be dismissed.
            now = time.time()
            for t in data.get("timers") or []:
                if (isinstance(t, dict) and t.get("expired")
                        and now - float(t.get("deadline_epoch") or 0) < RING_EVERY * RING_TIMES):
                    events.append({"type": "finished", "timer": t, "replayed": True})
        first = False
        since = last
        for event in events:
            timer = event.get("timer") if isinstance(event.get("timer"), dict) else {}
            if not event.get("replayed"):
                hass.bus.async_fire(EVENT_TIMER, {
                    "type": event.get("type"), "timer": timer, "entry_id": entry.entry_id})
            if event.get("type") in ("cancelled", "dismissed"):
                try:
                    _stop_ringing(hass, int(timer.get("id")))
                except (TypeError, ValueError):
                    pass
            satellite = should_ring(event, entry.options)
            if satellite:
                _start_ringing(hass, entry, satellite, timer)
        if events:
            await entry.runtime_data.main.async_request_refresh()


@callback
def async_start_timer_watch(hass: HomeAssistant, entry) -> None:
    entry.async_create_background_task(
        hass, _watch_timers(hass, entry), f"{DOMAIN} timer events")
    entry.async_on_unload(lambda: _stop_ringing(hass))
