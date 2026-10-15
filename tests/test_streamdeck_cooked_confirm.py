"""The Stream Deck Cooked key needs two presses (FoodAssistant-hyr67).

Marking a recipe cooked uses up its matched stock, so one press only arms the
key ("Press again to mark cooked" on the key, plus a kiosk toast naming the
recipe). A second press inside the window posts /mealie/cooked; a press after
the window arms again instead. No hardware, no network.
"""
from __future__ import annotations

import asyncio

from foodassistant_streamdeck import actions, config
from foodassistant_streamdeck.controller import Controller


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, recipe=None, consumed=("Carrot",)):
        self.recipe = recipe
        self.consumed = list(consumed)
        self.calls: list[tuple[str, str, object]] = []

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, None))
        if url.endswith("/current-recipe"):
            return _Resp(200, {"recipe": self.recipe})
        return _Resp(404, {})

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs.get("json")))
        if url.endswith("/mealie/cooked"):
            return _Resp(200, {"consumed": self.consumed})
        if url.endswith("/events/confirm"):
            return _Resp(200, {"ok": True})
        return _Resp(404, {})

    def cooked_posts(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/mealie/cooked")]

    def toasts(self):
        return [c[2]["message"] for c in self.calls
                if c[0] == "POST" and c[1].endswith("/events/confirm")]


class _Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


STEW = {"id": "stew", "title": "Beef Stew"}


def _ctx(client, confirm, notes=None):
    refreshed = {"n": 0}

    async def refresh():
        refreshed["n"] += 1

    async def navigate(path):
        return True

    def key_note(name, text, seconds=0):
        if notes is not None:
            notes.append((name, text, seconds))

    ctx = actions.ActionContext(
        client=client, base_url="http://x", refresh=refresh, navigate=navigate,
        cycle_brightness=lambda: 80, page_next=lambda: None, page_prev=lambda: None,
        cooked_confirm=confirm, key_note=key_note,
    )
    return ctx, refreshed


def _press(ctx):
    return asyncio.run(actions.run_action(actions.ACTIONS["cooked"], ctx))


# -- the pure confirm ----------------------------------------------------------


def test_press_confirm_arms_then_confirms_then_starts_over():
    clock = _Clock()
    pc = actions.PressConfirm(window=5, clock=clock)
    assert pc.press("cooked", "stew") is False
    assert pc.armed("cooked")
    clock.now += 4
    assert pc.press("cooked", "stew") is True
    assert not pc.armed("cooked")
    # A third press after a confirm arms again rather than confirming.
    assert pc.press("cooked", "stew") is False


def test_press_confirm_expires_after_the_window():
    clock = _Clock()
    pc = actions.PressConfirm(window=5, clock=clock)
    pc.press("cooked", "stew")
    clock.now += 5.01
    assert not pc.armed("cooked")
    assert pc.press("cooked", "stew") is False   # re-armed, not confirmed
    clock.now += 1
    assert pc.press("cooked", "stew") is True


def test_press_confirm_token_change_rearms():
    pc = actions.PressConfirm(window=5, clock=_Clock())
    pc.press("cooked", "stew")
    # The active recipe changed between presses: arm for the new one instead.
    assert pc.press("cooked", "soup") is False
    assert pc.press("cooked", "soup") is True


# -- the Cooked key through run_action -----------------------------------------


def test_one_press_consumes_nothing_and_says_press_again():
    client = _FakeClient(recipe=STEW)
    notes: list = []
    ctx, refreshed = _ctx(client, actions.PressConfirm(clock=_Clock()), notes)
    assert _press(ctx) == "armed"
    assert client.cooked_posts() == []
    assert notes[-1][1] == actions.COOKED_ARMED_LABEL
    assert notes[-1][2] == actions.COOKED_CONFIRM_SECONDS
    assert client.toasts() == ["Press Cooked again to mark Beef Stew cooked"]
    assert refreshed["n"] == 0


def test_second_press_within_window_consumes_once():
    clock = _Clock()
    client = _FakeClient(recipe=STEW, consumed=["Carrot", "Onion"])
    notes: list = []
    ctx, refreshed = _ctx(client, actions.PressConfirm(clock=clock), notes)
    _press(ctx)
    clock.now += 2
    assert _press(ctx) == "Cooked 2"
    posts = client.cooked_posts()
    assert len(posts) == 1 and posts[0][2] == {"slug": "stew"}
    assert notes[-1][1] == "Cooked 2"
    assert client.toasts()[-1] == "Marked Beef Stew cooked"
    assert refreshed["n"] == 1
    # The next press starts over: it arms again and consumes nothing more.
    assert _press(ctx) == "armed"
    assert len(client.cooked_posts()) == 1


def test_press_after_the_window_starts_over():
    clock = _Clock()
    client = _FakeClient(recipe=STEW)
    ctx, _ = _ctx(client, actions.PressConfirm(clock=clock))
    _press(ctx)
    clock.now += actions.COOKED_CONFIRM_SECONDS + 1
    assert _press(ctx) == "armed"
    assert client.cooked_posts() == []
    clock.now += 1
    assert _press(ctx).startswith("Cooked")
    assert len(client.cooked_posts()) == 1


def test_no_active_recipe_never_arms():
    client = _FakeClient(recipe=None)
    confirm = actions.PressConfirm(clock=_Clock())
    notes: list = []
    ctx, _ = _ctx(client, confirm, notes)
    assert _press(ctx) == "No recipe"
    assert _press(ctx) == "No recipe"
    assert not confirm.armed("cooked")
    assert client.cooked_posts() == [] and client.toasts() == []
    assert notes[-1][1] == "No recipe"


def test_default_context_cannot_consume_on_one_press():
    # A context built without the controller's shared confirm gets a fresh one
    # each time, so even repeated single-press contexts never consume.
    client = _FakeClient(recipe=STEW)
    for _ in range(3):
        ctx = actions.ActionContext(
            client=client, base_url="http://x",
            refresh=lambda: asyncio.sleep(0), navigate=lambda p: asyncio.sleep(0, True),
            cycle_brightness=lambda: 80, page_next=lambda: None, page_prev=lambda: None,
        )
        assert _press(ctx) == "armed"
    assert client.cooked_posts() == []


def test_macro_never_runs_cooked():
    # A macro fires on one press, so it must not carry the Cooked action, even
    # listed twice (which would otherwise arm and confirm in the same press).
    client = _FakeClient(recipe=STEW)
    ctx, _ = _ctx(client, actions.PressConfirm(clock=_Clock()))
    macro = actions.ActionSpec(name="custom_1", label="M", color="#000", kind="macro",
                               macro_actions=("cooked", "cooked"))
    asyncio.run(actions.run_action(macro, ctx))
    assert client.cooked_posts() == [] and client.toasts() == []


# -- the controller keeps the arm across presses -------------------------------


class _FakeDeck:
    def key_count(self):
        return 15

    def key_image_format(self):
        return {"size": (72, 72)}

    def deck_type(self):
        return "FakeDeck"

    def set_brightness(self, pct):
        pass

    def reset(self):
        pass

    def set_key_callback(self, cb):
        pass

    def set_key_image(self, key, image):
        pass


def test_controller_two_presses_consume_and_key_shows_press_again():
    ctrl = Controller(_FakeDeck(), config.Config().validated())
    loop = asyncio.new_event_loop()
    ctrl.loop = loop
    ctrl._draw_page = lambda: None

    async def _refresh():
        return None
    ctrl._refresh = _refresh
    client = _FakeClient(recipe=STEW)
    ctrl.client = client
    spec = actions.ACTIONS["cooked"]
    try:
        loop.run_until_complete(ctrl._handle(spec))
        assert client.cooked_posts() == []
        assert ctrl._key_note("cooked") == actions.COOKED_ARMED_LABEL
        loop.run_until_complete(ctrl._handle(spec))
        assert len(client.cooked_posts()) == 1
        assert ctrl._key_note("cooked").startswith("Cooked")
    finally:
        loop.close()
