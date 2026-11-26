"""Voice, timers and per-device choices against a real Home Assistant, with
the Pantry Raider install played by canned HTTP answers."""
import asyncio
import re

import pytest
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import intent
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.pantry_raider import voice

BASE = "http://pr.test:9284"
SAT = "assist_satellite.kitchen"


class FakeInstall:
    """The few routes voice uses, with a timer list that behaves."""

    def __init__(self, mock):
        self.timers = []
        self.events = []
        self.next_id = 1
        self.calls = []
        self.wake = asyncio.Event()
        mock.get(f"{BASE}/ha/state", json={"mode": "server", "device_id": "main1"})
        mock.get(f"{BASE}/api/screens", json={"screens": [
            {"id": "server", "label": "Main screen", "kind": "server", "targetable": True},
            {"id": "ks-kitchen", "label": "Kitchen tablet", "kind": "kiosk_satellite",
             "targetable": True}]})
        mock.get(f"{BASE}/timers", side_effect=self._list)
        mock.post(f"{BASE}/timers", side_effect=self._start)
        mock.post(f"{BASE}/timers/dismiss", side_effect=self._dismiss)
        mock.delete(re.compile(rf"{BASE}/timers/\d+"), side_effect=self._delete)
        mock.get(f"{BASE}/timers/events", side_effect=self._events)
        mock.post(f"{BASE}/api/screens/show-recipe", side_effect=self._recipe)
        mock.post(f"{BASE}/events/close", json={"ok": True, "screen": {"id": "ks-kitchen"}})

    def _resp(self, payload, status=200):
        from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse
        return AiohttpClientMockResponse("GET", BASE, status=status, json=payload)

    def emit(self, kind, timer):
        self.events.append({"id": len(self.events) + 1, "type": kind, "timer": dict(timer)})
        self.wake.set()

    async def _list(self, method, url, data):
        return self._resp({"timers": self.timers})

    async def _start(self, method, url, data):
        self.calls.append(("start", data))
        t = {"id": self.next_id, "label": data["label"], "remaining_seconds": data["seconds"],
             "expired": False, "state": "running", "origin": data["origin"],
             "deadline_epoch": 9e12}
        self.next_id += 1
        self.timers.append(t)
        self.emit("started", t)
        return self._resp({"timer": t, "screen": {"id": data["screen"]} if data["screen"] else None})

    async def _dismiss(self, method, url, data):
        gone = [t for t in self.timers if t["expired"]]
        self.timers = [t for t in self.timers if not t["expired"]]
        for t in gone:
            self.emit("dismissed", t)
        return self._resp({"ok": True, "dismissed": len(gone)})

    async def _delete(self, method, url, data):
        tid = int(str(url).rsplit("/", 1)[1])
        hit = [t for t in self.timers if t["id"] == tid]
        self.timers = [t for t in self.timers if t["id"] != tid]
        for t in hit:
            self.emit("cancelled", t)
        return self._resp({"ok": bool(hit)}, 200 if hit else 404)

    async def _events(self, method, url, data):
        since = int(url.query["since"])
        if int(url.query.get("wait", 0)) and not [e for e in self.events if e["id"] > since]:
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), 0.3)
            except TimeoutError:
                pass
        return self._resp({"events": [e for e in self.events if e["id"] > since],
                           "last_id": len(self.events), "timers": self.timers})

    async def _recipe(self, method, url, data):
        self.calls.append(("recipe", data))
        if "lasagna" in data["query"]:
            return self._resp({"ok": True, "result": "shown",
                               "recipe": {"slug": "lasagna", "title": "Lasagna"},
                               "speech": "Showing Lasagna on the Kitchen tablet."})
        return self._resp({"ok": False, "result": "not_found",
                           "speech": f"I could not find a recipe called {data['query']}."})

    def finish(self, timer_id):
        for t in self.timers:
            if t["id"] == timer_id:
                t.update(expired=True, state="ringing", remaining_seconds=0)
                self.emit("finished", t)


@pytest.fixture
async def install(hass: HomeAssistant, aioclient_mock):
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    assert await async_setup_component(hass, "intent", {})
    return FakeInstall(aioclient_mock)


async def _setup(hass, options=None):
    entry = MockConfigEntry(
        domain="pantry_raider", data={"host": "pr.test", "port": 9284, "api_key": "k"},
        options={"connect_back": False, **(options or {})})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _voice(timers="pantry_raider", sentences=True, screen="ks-kitchen"):
    return {"voice": {"timer_sentences": sentences, "satellites": {
        SAT: {"timers": timers, "recipes": True, "screen": screen}}}}


async def _call(hass, service, data):
    return await hass.services.async_call(
        "pantry_raider", service, data, blocking=True, return_response=True)


async def _say(hass, text, satellite=SAT):
    result = await conversation.async_converse(
        hass, text, None, Context(), satellite_id=satellite,
        agent_id=conversation.HOME_ASSISTANT_AGENT)
    return result.response.speech["plain"]["speech"]


async def test_services_answer_with_something_to_say(hass, install):
    await _setup(hass, _voice())
    out = await _call(hass, "get_routing", {"satellite": SAT})
    assert out == {"satellite": SAT, "timers": "pantry_raider", "recipes": True,
                   "screen": "ks-kitchen", "screen_label": "Kitchen tablet"}
    out = await _call(hass, "get_routing", {"satellite": "assist_satellite.den"})
    assert out["timers"] == "home_assistant" and out["screen"] == ""

    out = await _call(hass, "show_recipe", {"query": "lasagna", "satellite": SAT})
    assert out["ok"] and out["speech"] == "Showing Lasagna on the Kitchen tablet."
    assert install.calls[-1] == ("recipe", {"query": "lasagna", "screen": "ks-kitchen"})
    out = await _call(hass, "show_recipe", {"query": "pad thai"})
    assert not out["ok"] and out["result"] == "not_found"

    out = await _call(hass, "start_timer", {"duration": "10 minutes", "name": "pasta",
                                            "satellite": SAT})
    assert out["speech"] == "Pasta timer set for 10 minutes."
    assert install.calls[-1] == ("start", {"label": "Pasta", "seconds": 600, "origin": SAT,
                                           "screen": "ks-kitchen"})
    out = await _call(hass, "start_timer", {"duration": 90})
    assert out["timer"]["label"] == "1 minute 30 second timer"
    assert (await _call(hass, "start_timer", {"duration": "soon"}))["result"] == "no_duration"

    out = await _call(hass, "timer_status", {"name": "pasta"})
    assert out["speech"] == "Pasta has 10 minutes left."
    out = await _call(hass, "cancel_timer", {})
    assert out["result"] == "ambiguous"
    out = await _call(hass, "cancel_timer", {"name": "pasta"})
    assert out["speech"] == "Pasta cancelled." and len(install.timers) == 1
    out = await _call(hass, "cancel_timer", {"all": True})
    assert out["cancelled"] == 1 and install.timers == []
    out = await _call(hass, "dismiss_timer", {"all": True, "satellite": SAT})
    assert out == {"ok": False, "result": "not_ringing", "dismissed": 0,
                   "speech": "No timer is ringing."}
    out = await _call(hass, "show_page", {"page": "close", "satellite": SAT})
    assert out["ok"]


async def test_sentences_reach_the_same_actions(hass, install):
    await _setup(hass, _voice())
    assert await _say(hass, "show me the lasagna recipe") == "Showing Lasagna on the Kitchen tablet."
    assert await _say(hass, "open the recipe for pad thai") == (
        "I could not find a recipe called pad thai.")
    assert await _say(hass, "set a 10 minute pasta timer") == "Pasta timer set for 10 minutes."
    assert await _say(hass, "set a timer for an hour and a half") == (
        "Timer set for 1 hour and 30 minutes.")
    assert install.timers[0]["origin"] == SAT
    assert "Pasta has 10 minutes left" in await _say(hass, "how long is left on the pasta timer")
    assert await _say(hass, "cancel the pasta timer") == "Pasta cancelled."
    assert await _say(hass, "cancel all timers") == "1 hour 30 minute timer cancelled."
    assert await _say(hass, "stop the timer") == "No timer is ringing."
    assert await _say(hass, "close the recipe") == "Closed."


async def test_timer_sentences_are_off_unless_chosen(hass, install):
    await _setup(hass, _voice(sentences=False))
    manager = voice._agent_manager(hass)
    said = " ".join(manager.trigger_sentences)
    assert "recipe" in said and "timer" not in said
    assert intent.async_get(hass) and voice.INTENT_START_TIMER not in {
        h.intent_type for h in intent.async_get(hass)}
    assert voice.INTENT_SHOW_RECIPE in {h.intent_type for h in intent.async_get(hass)}


async def test_intents_for_an_llm_agent(hass, install):
    await _setup(hass, _voice())
    response = await intent.async_handle(
        hass, "test", voice.INTENT_START_TIMER,
        {"minutes": {"value": 4}, "name": {"value": "eggs"}}, satellite_id=SAT)
    assert response.speech["plain"]["speech"] == "Eggs timer set for 4 minutes."
    response = await intent.async_handle(
        hass, "test", voice.INTENT_SHOW_RECIPE, {"query": {"value": "lasagna"}},
        satellite_id=SAT)
    assert "Lasagna" in response.speech["plain"]["speech"]
    response = await intent.async_handle(hass, "test", voice.INTENT_STOP_TIMER, {})
    assert response.speech["plain"]["speech"] == "No timer is ringing."


async def test_a_finished_timer_rings_its_satellite_until_dismissed(hass, install, monkeypatch):
    monkeypatch.setattr(voice, "RING_EVERY", 0.05)
    announced = async_mock_service(hass, "assist_satellite", "announce")
    hass.states.async_set(SAT, "idle")
    fired = []
    hass.bus.async_listen(voice.EVENT_TIMER, lambda e: fired.append(e.data["type"]))
    await _setup(hass, _voice())
    out = await _call(hass, "start_timer", {"duration": 60, "name": "eggs", "satellite": SAT})
    install.finish(out["timer"]["id"])
    for _ in range(400):
        await asyncio.sleep(0.02)
        if len(announced) >= 2:
            break
    assert len(announced) >= 2
    assert announced[0].data == {"entity_id": SAT, "message": "Your Eggs timer is done."}
    assert (await _call(hass, "dismiss_timer", {"all": True}))["dismissed"] == 1
    await asyncio.sleep(0.2)
    count = len(announced)
    await asyncio.sleep(0.3)
    assert len(announced) == count
    assert fired[:2] == ["started", "finished"] and "dismissed" in fired


async def test_options_flow(hass, install):
    entry = await _setup(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] == "menu" and flow["menu_options"] == ["connection", "voice", "satellite"]
    step = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "satellite"})
    hass.states.async_set(SAT, "idle")
    step = await hass.config_entries.options.async_configure(flow["flow_id"], {"satellite": SAT})
    assert step["step_id"] == "satellite_edit"
    done = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"timers": "pantry_raider", "recipes": True, "screen": "ks-kitchen"})
    assert done["type"] == "create_entry"
    assert done["data"]["voice"]["satellites"][SAT] == {
        "timers": "pantry_raider", "recipes": True, "screen": "ks-kitchen"}
    await hass.async_block_till_done()
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "voice"})
    done = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"recipe_sentences": True, "timer_sentences": True, "ring": False})
    assert done["data"]["voice"]["timer_sentences"] is True
    assert done["data"]["voice"]["satellites"][SAT]["screen"] == "ks-kitchen"
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_an_llm_agent_is_given_the_tools_and_they_know_the_device(hass, install):
    """Registering an intent is not enough for an AI model agent to see it:
    the integration's llm platform has to offer it."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er, llm

    assert await async_setup_component(hass, "llm", {})
    entry = await _setup(hass, _voice())
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "kitchen-sat")})
    er.async_get(hass).async_get_or_create(
        "assist_satellite", "test", "kitchen", suggested_object_id="kitchen",
        device_id=device.id)
    ctx = llm.LLMContext(platform="test", context=Context(), language="en",
                         assistant="conversation", device_id=device.id)
    api = await llm.async_get_api(hass, llm.LLM_API_ASSIST, ctx)
    names = {t.name for t in api.tools}
    assert {"pantry_raider__ShowRecipe", "pantry_raider__StartTimer",
            "pantry_raider__StopTimer"} <= names
    assert "pantry_raider__ShowRecipe" in api.api_prompt
    out = await api.async_call_tool(llm.ToolInput(
        tool_name="pantry_raider__ShowRecipe", tool_args={"query": "lasagna"}))
    assert "Lasagna" in out["speech"]["plain"]["speech"]
    # The tool call carried only the device; its voice entity's screen was used.
    assert install.calls[-1] == ("recipe", {"query": "lasagna", "screen": "ks-kitchen"})
    await api.async_call_tool(llm.ToolInput(
        tool_name="pantry_raider__StartTimer", tool_args={"minutes": 3, "name": "tea"}))
    assert install.calls[-1][1]["origin"] == SAT


async def test_more_ways_to_ask_for_a_recipe(hass, install):
    await _setup(hass, _voice())
    for said in ("show me my lasagna recipe from pantry raider",
                 "show me a lasagna recipe", "lasagna recipe from pantry raider"):
        assert "Lasagna" in await _say(hass, said), said
