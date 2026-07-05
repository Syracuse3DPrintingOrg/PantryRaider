"""Display-off logo face and cross-surface wake (FoodAssistant-zttc, -exuv).

While the kiosk display sleeps, the Stream Deck shows the Pantry Raider mark
across every key (unless the deck's own idle timeout has already blanked it),
and activity on either surface wakes both. These tests cover the deck config
toggle, the pure decision helpers, and the controller's enter/exit and wake
paths against a fake deck. No hardware, no network.
"""
from __future__ import annotations

import asyncio
import time

from foodassistant_streamdeck import config
from foodassistant_streamdeck.controller import (
    Controller,
    _display_off_logo_due,
    _external_wake_due,
)


# -- config ------------------------------------------------------------------


def test_logo_when_display_off_defaults_on_and_loads(tmp_path):
    assert config.Config().logo_when_display_off is True
    f = tmp_path / "config.toml"
    f.write_text("logo_when_display_off = false\n")
    assert config.load(f).logo_when_display_off is False


def test_logo_when_display_off_non_bool_is_ignored(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('logo_when_display_off = "nope"\n')
    assert config.load(f).logo_when_display_off is True


def test_retired_screensaver_layout_key_is_harmless(tmp_path):
    # An updated device may still carry the old shared-canvas setting in its
    # config.toml; loading must ignore it without an error or a stray field.
    f = tmp_path / "config.toml"
    f.write_text('screensaver_layout = "below"\nbrightness = 40\n')
    cfg = config.load(f)
    assert cfg.brightness == 40
    assert not hasattr(config.Config(), "screensaver_layout")


# -- pure decision helpers -----------------------------------------------------


def test_display_off_logo_due_truth_table():
    # The face shows exactly when: display asleep, setting on, deck not
    # blanked on its own timeout, no camera overlay, and not mid-wake.
    assert _display_off_logo_due(True, True, False, False, False) is True
    assert _display_off_logo_due(False, True, False, False, False) is False  # display awake
    assert _display_off_logo_due(True, False, False, False, False) is False  # setting off
    assert _display_off_logo_due(True, True, True, False, False) is False    # deck already dark
    assert _display_off_logo_due(True, True, False, True, False) is False    # camera overlay owns keys
    assert _display_off_logo_due(True, True, False, False, True) is False    # waking right now


def test_external_wake_due_window_and_advance():
    now = 1000.0
    # First look: only a fresh report wakes; the seen mark is adopted either way.
    assert _external_wake_due(now - 1, now, None) == (True, now - 1)
    assert _external_wake_due(now - 300, now, None) == (False, now - 300)
    # A later ADVANCE wakes even when the poll ran late and the report has
    # already aged out of the freshness window (FoodAssistant-exuv).
    wake, seen = _external_wake_due(now - 60, now, now - 300)
    assert wake is True and seen == now - 60
    # No advance, no freshness: stay asleep.
    assert _external_wake_due(now - 300, now, now - 300) == (False, now - 300)


def test_external_wake_due_rejects_bad_epochs():
    now = 1000.0
    assert _external_wake_due(None, now, None) == (False, None)
    assert _external_wake_due("nope", now, 5.0) == (False, 5.0)
    assert _external_wake_due(0, now, 5.0) == (False, 5.0)
    # A future timestamp is a clock error, never a wake, and never remembered.
    assert _external_wake_due(now + 60, now, 5.0) == (False, 5.0)


# -- controller paths ----------------------------------------------------------


class _FakeDeck:
    def __init__(self, key_count=15):
        self._key_count = key_count
        self.brightness_calls: list[int] = []
        self.reset_calls = 0
        self._callback = None

    def key_count(self):
        return self._key_count

    def key_image_format(self):
        return {"size": (72, 72)}

    def deck_type(self):
        return "FakeDeck"

    def set_brightness(self, pct):
        self.brightness_calls.append(pct)

    def reset(self):
        self.reset_calls += 1

    def set_key_callback(self, cb):
        self._callback = cb

    def set_key_image(self, key, image):
        pass

    def press_down(self, key):
        if self._callback:
            self._callback(self, key, True)


def _make_controller():
    cfg = config.Config().validated()
    deck = _FakeDeck()
    ctrl = Controller(deck, cfg)
    loop = asyncio.new_event_loop()
    ctrl.loop = loop
    deck.set_key_callback(ctrl._on_key)
    ctrl._page_draws = 0

    def _draw():
        ctrl._page_draws += 1
    ctrl._draw_page = _draw
    ctrl._logo_paints = 0

    def _tiles(tiles):
        ctrl._logo_paints += 1
    ctrl._set_full_deck_tiles = _tiles
    return ctrl, deck, loop


def test_display_blanked_enters_logo_face_and_wake_exits():
    ctrl, deck, loop = _make_controller()
    ctrl._apply_display_state(True, False)
    assert ctrl._logo_face_active is True
    assert ctrl._logo_paints == 1  # one static frame, no loop
    # A second report while still blanked repaints nothing.
    ctrl._apply_display_state(True, False)
    assert ctrl._logo_paints == 1
    # The display waking returns the deck to its page.
    ctrl._apply_display_state(False, False)
    assert ctrl._logo_face_active is False
    assert ctrl._page_draws == 1
    loop.close()


def test_logo_face_not_entered_when_disabled_or_deck_blanked():
    ctrl, deck, loop = _make_controller()
    ctrl.config.logo_when_display_off = False
    ctrl._apply_display_state(True, False)
    assert ctrl._logo_face_active is False
    ctrl.config.logo_when_display_off = True
    ctrl._idle_blanked = True
    ctrl._apply_display_state(True, False)
    assert ctrl._logo_face_active is False  # a dark deck stays dark
    loop.close()


def test_external_activity_exits_logo_face():
    ctrl, deck, loop = _make_controller()
    ctrl._apply_display_state(True, False)
    assert ctrl._logo_face_active is True
    # The same poll that saw the touch reports the display still blanked for
    # an instant; waking wins and the page comes back.
    ctrl._apply_display_state(True, True)
    assert ctrl._logo_face_active is False
    assert ctrl._page_draws == 1
    loop.close()


def test_key_press_on_logo_face_is_swallowed_and_exits():
    ctrl, deck, loop = _make_controller()
    ctrl._apply_display_state(True, False)
    deck.press_down(3)
    # The exit is scheduled on the loop thread; run the pending callback.
    loop.call_soon(loop.stop)
    loop.run_forever()
    assert ctrl._logo_face_active is False
    assert 3 not in ctrl._key_down_time  # no action will fire on release
    loop.close()


def test_deck_idle_blank_clears_logo_face():
    ctrl, deck, loop = _make_controller()
    ctrl.config.idle_timeout_minutes = 1
    ctrl._apply_display_state(True, False)
    ctrl._last_activity = time.monotonic() - 120
    loop.run_until_complete(ctrl._idle_loop_once())
    assert ctrl._idle_blanked is True
    assert ctrl._logo_face_active is False
    assert deck.brightness_calls[-1] == 0
    loop.close()


def test_poll_shared_activity_advance_unblanks_deck(monkeypatch):
    """A touch between polls wakes a blanked deck even when the poll tick ran
    late enough that the report aged past the freshness window."""
    import foodassistant_streamdeck.controller as controller_mod

    reports = {"last_activity": time.time() - 300, "display_blanked": False}

    class _Resp:
        def json(self):
            return dict(reports)

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _Resp()

    monkeypatch.setattr(controller_mod.httpx, "AsyncClient", _FakeClient)
    ctrl, deck, loop = _make_controller()
    ctrl.config.host_bridge_url = "http://127.0.0.1:9299"
    # First poll adopts the (stale) mark without waking.
    loop.run_until_complete(ctrl._poll_shared_activity())
    ctrl._idle_blanked = True
    # A touch happened since, but this poll runs 60s later: stale by the
    # window, yet an advance past the seen mark, so the deck still wakes.
    reports["last_activity"] = time.time() - 60
    loop.run_until_complete(ctrl._poll_shared_activity())
    assert ctrl._idle_blanked is False
    assert deck.brightness_calls[-1] > 0
    loop.close()
