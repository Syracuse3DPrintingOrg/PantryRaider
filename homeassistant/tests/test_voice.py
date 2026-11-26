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
        mock.get(f"{BASE}/api/screens/recipe", side_effect=self._get_recipe)
        mock.post(f"{BASE}/api/screens/recipe/step", side_effect=self._step)
        mock.post(f"{BASE}/api/screens/recipe/timer", side_effect=self._recipe_timer)
        self.open = None
        self.step = None

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

    PIE = {"title": "Best-Ever Key Lime Pie", "servings": 8,
           "ingredients": ["1 1/2 cups graham cracker crumbs", "4 egg yolks"],
           "steps": [{"number": 1, "text": "Heat the oven to 350 F.", "timers": []},
                     {"number": 2, "text": "Bake the crust for 10 minutes.",
                      "timers": [{"label": "Bake crust", "seconds": 600,
                                  "duration": "10 minutes", "running": False}]},
                     {"number": 3, "text": "Pour in the filling and bake for 15 minutes.",
                      "timers": [{"label": "Bake", "seconds": 900,
                                  "duration": "15 minutes", "running": False}]}],
           "step_count": 3, "id": "server-side-only"}

    def _described(self):
        if not self.open:
            return {"ok": False, "result": "none",
                    "speech": "No recipe is open on the Kitchen tablet. What are you cooking?"}
        return {"ok": True, "result": "open",
                "screen": {"id": "ks-kitchen", "label": "Kitchen tablet"},
                **self.open, "step": self.step,
                "speech": f"{self.open['title']} is open on the Kitchen tablet."}

    async def _get_recipe(self, method, url, data):
        self.calls.append(("get_recipe", dict(url.query)))
        return self._resp(self._described())

    async def _step(self, method, url, data):
        self.calls.append(("step", data))
        out = self._described()
        if not out["ok"]:
            return self._resp(out)
        word = data["step"]
        n = {"first": 1, "next": (self.step or 0) + 1, "back": max(1, (self.step or 1) - 1),
             "repeat": self.step or 1, "last": 3}.get(word) or int(word)
        if n > 3:
            return self._resp({**out, "result": "last_step", "speech": "That was the last step."})
        self.step = n
        step = self.open["steps"][n - 1]
        return self._resp({**self._described(), "result": "step", "current": step,
                           "speech": f"Step {n} of 3: {step['text']}"})

    async def _recipe_timer(self, method, url, data):
        self.calls.append(("recipe_timer", data))
        if not self.open:
            return self._resp(self._described())
        step = data.get("step") or (self.step if not data["query"] else None)
        if not step and "bak" in data["query"]:
            return self._resp({"ok": False, "result": "ambiguous",
                               "choices": [{"label": "Bake crust", "seconds": 600, "step": 2},
                                           {"label": "Bake", "seconds": 900, "step": 3}],
                               "speech": "Which timer: bake crust for 10 minutes in step 2 "
                                         "or bake for 15 minutes in step 3?"})
        if "crust" in data["query"]:
            step = 2
        if step not in (2, 3):
            return self._resp({"ok": False, "result": "not_found",
                               "speech": f"Step {step} has no time in it."})
        t = self.open["steps"][step - 1]["timers"][0]
        return self._resp({"ok": True, "result": "started", "step": step,
                           "timer": {"id": 99, "label": t["label"], "total_seconds": t["seconds"],
                                     "remaining_seconds": t["seconds"], "state": "running",
                                     "origin": data["origin"]},
                           "speech": f"{t['label']} timer started for {t['duration']}."})

    async def _recipe(self, method, url, data):
        self.calls.append(("recipe", data))
        said = (data.get("query") or "") + " " + (data.get("choice") or "")
        if "best" in said or data.get("slug") == "klp":
            self.open, self.step = dict(self.PIE), None
            return self._resp({"ok": True, "result": "shown",
                               "recipe": {"slug": "klp", "title": "Best-Ever Key Lime Pie"},
                               "speech": "Showing Best-Ever Key Lime Pie on the Kitchen tablet."})
        if "key lime" in said:
            return self._resp({"ok": False, "result": "ambiguous",
                               "candidates": [{"slug": "klp", "title": "Best-Ever Key Lime Pie"},
                                              {"slug": "bars", "title": "Key Lime Pie Bars"}],
                               "speech": "I found Best-Ever Key Lime Pie and Key Lime Pie Bars. "
                                         "Which one?"})
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
    assert install.calls[-1][1]["page"] == "recipe"
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
    said = manager.trigger_sentences
    assert any("recipe" in s for s in said)
    # No timer sentence at all: the step sentences never say "timer".
    assert not [x for x in said if "timer" in x]
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
    # A recipe tool hands the model the answer itself, not an intent response.
    assert "Lasagna" in out["speech"] and out["recipe"]["slug"] == "lasagna"
    # The tool call carried only the device; its voice entity's screen was used.
    assert install.calls[-1] == ("recipe", {"query": "lasagna", "screen": "ks-kitchen",
                                            "page": "recipe"})
    await api.async_call_tool(llm.ToolInput(
        tool_name="pantry_raider__StartTimer", tool_args={"minutes": 3, "name": "tea"}))
    assert install.calls[-1][1]["origin"] == SAT


async def test_a_sentence_that_names_only_the_device_uses_its_screen(hass, install):
    """Home Assistant's websocket pipeline (assist_pipeline/run) takes a
    device_id and refuses a satellite_id, so a sentence can arrive naming
    only the device. Its assist_satellite entity, and the screen chosen for
    that entity, are used, the same as for a voice satellite."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    entry = await _setup(hass, _voice())
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "kitchen-sat")})
    er.async_get(hass).async_get_or_create(
        "assist_satellite", "test", "kitchen", suggested_object_id="kitchen",
        device_id=device.id)

    async def say(text, device_id=device.id):
        result = await conversation.async_converse(
            hass, text, None, Context(), device_id=device_id, satellite_id=None,
            agent_id=conversation.HOME_ASSISTANT_AGENT)
        return result.response.speech["plain"]["speech"]

    assert await say("let's cook a key lime pie") == (
        "I found Best-Ever Key Lime Pie and Key Lime Pie Bars. Which one?")
    assert install.calls[-1] == ("recipe", {"query": "key lime pie", "screen": "ks-kitchen",
                                            "page": "cook"})
    assert await say("the best-ever one") == (
        "Showing Best-Ever Key Lime Pie on the Kitchen tablet.")
    assert install.calls[-1][1]["screen"] == "ks-kitchen"
    assert await say("what's next") == "Step 1 of 3: Heat the oven to 350 F."
    assert install.calls[-1] == ("step", {"screen": "ks-kitchen", "step": "next"})
    assert await say("show me the lasagna recipe") == "Showing Lasagna on the Kitchen tablet."
    assert install.calls[-1][1]["screen"] == "ks-kitchen"
    # A timer set this way follows that entity's choices and rings on it.
    assert await say("set a 10 minute pasta timer") == "Pasta timer set for 10 minutes."
    assert install.calls[-1] == ("start", {"label": "Pasta", "seconds": 600, "origin": SAT,
                                           "screen": "ks-kitchen"})
    # A device with no voice entity of its own asks about the kitchen's recipe.
    speaker = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "den-speaker")})
    assert await say("what's next", speaker.id) == "Step 2 of 3: Bake the crust for 10 minutes."
    assert install.calls[-1] == ("step", {"screen": "", "step": "next"})


async def test_more_ways_to_ask_for_a_recipe(hass, install):
    await _setup(hass, _voice())
    for said in ("show me my lasagna recipe from pantry raider",
                 "show me a lasagna recipe", "lasagna recipe from pantry raider"):
        assert "Lasagna" in await _say(hass, said), said


async def test_open_and_close_pantry_raider_by_voice(hass, install, aioclient_mock):
    aioclient_mock.post(f"{BASE}/events/navigate", json={"ok": True, "screen": {"id": "ks-kitchen"}})
    await _setup(hass, _voice())
    assert await _say(hass, "open pantry raider") == "Opening Pantry Raider."
    sent = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/events/navigate")][-1]
    assert sent[2] == {"page": "app", "screen": "ks-kitchen"}
    assert await _say(hass, "close pantry raider") == "Closed."
    out = await _call(hass, "show_page", {"page": "app", "satellite": SAT})
    assert out["ok"]


async def _llm_api(hass, entry):
    from homeassistant.helpers import device_registry as dr, entity_registry as er, llm

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "kitchen-sat")})
    er.async_get(hass).async_get_or_create(
        "assist_satellite", "test", "kitchen", suggested_object_id="kitchen",
        device_id=device.id)
    ctx = llm.LLMContext(platform="test", context=Context(), language="en",
                         assistant="conversation", device_id=device.id)
    return await llm.async_get_api(hass, llm.LLM_API_ASSIST, ctx)


async def _tool(api, name, **args):
    from homeassistant.helpers import llm

    return await api.async_call_tool(llm.ToolInput(tool_name=f"pantry_raider__{name}",
                                                   tool_args=args))


async def test_cook_by_voice_with_the_built_in_agent(hass, install):
    """Let's cook, which one, the steps, and the step timers, all by sentence."""
    await _setup(hass, _voice(sentences=False))
    assert await _say(hass, "what do I do first") == (
        "No recipe is open on the Kitchen tablet. What are you cooking?")
    assert await _say(hass, "let's cook a key lime pie") == (
        "I found Best-Ever Key Lime Pie and Key Lime Pie Bars. Which one?")
    assert install.calls[-1] == ("recipe", {"query": "key lime pie", "screen": "ks-kitchen",
                                            "page": "cook"})
    assert await _say(hass, "the best-ever one") == (
        "Showing Best-Ever Key Lime Pie on the Kitchen tablet.")
    # The answer keeps the page the question was asked for (the install knows it).
    assert "best" in install.calls[-1][1]["choice"] and "page" not in install.calls[-1][1]
    assert await _say(hass, "what do I do first") == "Step 1 of 3: Heat the oven to 350 F."
    assert await _say(hass, "what's next") == "Step 2 of 3: Bake the crust for 10 minutes."
    assert install.calls[-1] == ("step", {"screen": "ks-kitchen", "step": "next"})
    assert await _say(hass, "what is step three") == (
        "Step 3 of 3: Pour in the filling and bake for 15 minutes.")
    assert await _say(hass, "go back a step") == "Step 2 of 3: Bake the crust for 10 minutes."
    assert await _say(hass, "next step") == (
        "Step 3 of 3: Pour in the filling and bake for 15 minutes.")
    assert await _say(hass, "previous step") == "Step 2 of 3: Bake the crust for 10 minutes."
    assert await _say(hass, "read that again") == "Step 2 of 3: Bake the crust for 10 minutes."
    assert install.calls[-1][1]["step"] == "repeat"
    # "Start the timer" belongs to the house's own automation, which calls
    # the start_recipe_timer service; no sentence here claims it.
    before = len(install.calls)
    await _say(hass, "start the timer")
    assert not [c for c in install.calls[before:] if c[0] == "recipe_timer"]
    out = await _call(hass, "start_recipe_timer", {"satellite": SAT})
    assert out["speech"] == "Bake crust timer started for 10 minutes."
    assert install.calls[-1][1] == {"screen": "ks-kitchen", "query": "", "origin": SAT,
                                    "show": False}
    out = await _call(hass, "start_recipe_timer", {"satellite": SAT, "query": "baking it"})
    assert out["result"] == "ambiguous" and [c["step"] for c in out["choices"]] == [2, 3]
    out = await _call(hass, "start_recipe_timer", {"satellite": SAT, "step": 3})
    assert out["result"] == "started"
    # "Show me the ... recipe" keeps opening the whole recipe.
    await _say(hass, "show me the lasagna recipe")
    assert install.calls[-1][1]["page"] == "recipe"


async def test_an_llm_agent_gets_the_recipe_itself(hass, install):
    """The model is handed the data (candidates, steps, timer choices), not
    only a sentence, and the recipe tools are there with timer sentences off."""
    assert await async_setup_component(hass, "llm", {})
    entry = await _setup(hass, _voice(sentences=False))
    api = await _llm_api(hass, entry)
    names = {t.name for t in api.tools}
    assert {"pantry_raider__ShowRecipe", "pantry_raider__GetRecipe",
            "pantry_raider__StartRecipeTimer"} <= names
    assert "pantry_raider__StartTimer" not in names
    assert "pantry_raider__GetRecipe" in api.api_prompt

    out = await _tool(api, "GetRecipe")
    assert out["result"] == "none" and "What are you cooking?" in out["speech"]

    # Two turns: several fit, the model is given them, and answers with a choice.
    out = await _tool(api, "ShowRecipe", query="key lime pie")
    assert out["result"] == "ambiguous"
    assert [c["slug"] for c in out["candidates"]] == ["klp", "bars"]
    out = await _tool(api, "ShowRecipe", choice="the best-ever one")
    assert out["result"] == "shown" and out["recipe"]["slug"] == "klp"
    assert install.calls[-1][1]["screen"] == "ks-kitchen"

    out = await _tool(api, "GetRecipe")
    assert out["title"] == "Best-Ever Key Lime Pie"
    assert [s["text"] for s in out["steps"]][1] == "Bake the crust for 10 minutes."
    assert out["ingredients"][0] == "1 1/2 cups graham cracker crumbs"
    assert out["screen"] == "Kitchen tablet" and "id" not in out
    out = await _tool(api, "GetRecipe", step="next")
    assert out["current"]["number"] == 1 and out["step"] == 1

    out = await _tool(api, "StartRecipeTimer", query="bake")
    assert out["result"] == "ambiguous"
    assert [c["step"] for c in out["choices"]] == [2, 3]
    out = await _tool(api, "StartRecipeTimer", step=3)
    assert out["result"] == "started" and out["timer"]["label"] == "Bake"
    assert install.calls[-1][1]["origin"] == SAT


async def test_recipe_services(hass, install):
    await _setup(hass, _voice())
    out = await _call(hass, "cook", {"query": "key lime pie", "satellite": SAT})
    assert out["result"] == "ambiguous" and len(out["candidates"]) == 2
    out = await _call(hass, "cook", {"choice": "the best-ever one", "satellite": SAT})
    assert out["ok"]
    out = await _call(hass, "get_recipe", {"satellite": SAT})
    assert out["step_count"] == 3
    out = await _call(hass, "recipe_step", {"step": 2, "satellite": SAT})
    assert out["current"]["number"] == 2
    out = await _call(hass, "recipe_step", {"step": "sideways", "satellite": SAT})
    assert out["result"] == "bad_step"
    out = await _call(hass, "start_recipe_timer", {"satellite": SAT})
    assert out["speech"] == "Bake crust timer started for 10 minutes."
    await _call(hass, "get_recipe", {"screen": "ks-other"})
    assert install.calls[-1] == ("get_recipe", {"screen": "ks-other"})
