"""Idle logo face and cross-surface wake (FoodAssistant-gic5, -exuv).

When the Stream Deck reaches its OWN "Blank after idle" timeout and the toggle
is on, it lights the Pantry Raider mark across every key instead of going dark;
a key press (or activity on another surface) returns it to the keys. The logo is
tied to the deck's idle, not the kitchen display. These tests cover the deck
config toggle, the pure decision helper, and the controller's idle-blank, wake,
and press paths against a fake deck. No hardware, no network.
"""
from __future__ import annotations

import asyncio
import time

from foodassistant_streamdeck import config
from foodassistant_streamdeck.controller import (
    Controller,
    _idle_logo_due,
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


def test_idle_logo_due_truth_table():
    # The logo shows exactly when: the deck reached its idle timeout, the toggle
    # is on, and no full-deck camera overlay owns the keys.
    assert _idle_logo_due(True, True, False) is True
    assert _idle_logo_due(False, True, False) is False   # deck not idled yet -> keys
    assert _idle_logo_due(True, False, False) is False   # toggle off -> go dark
    assert _idle_logo_due(True, True, True) is False      # camera overlay owns keys


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


def test_key_press_reports_activity_to_the_bridge():
    # The kiosk's screensaver dismiss-on-external-activity (FoodAssistant-fho8)
    # relies on the deck already telling the bridge about a press. That wiring
    # (FoodAssistant-otiy) must stay in _on_key, so a deck press keeps waking
    # the physical panel and advancing the shared activity epoch the kiosk polls.
    import inspect

    src = inspect.getsource(Controller._on_key)
    assert "_report_activity" in src


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


def _run_idle_to_blank(ctrl, loop):
    """Push the deck past its idle timeout and run one idle-loop tick."""
    ctrl.config.idle_timeout_minutes = 1
    ctrl._last_activity = time.monotonic() - 120
    loop.run_until_complete(ctrl._idle_loop_once())


def test_idle_timeout_shows_logo_when_toggle_on():
    ctrl, deck, loop = _make_controller()
    ctrl.config.logo_when_display_off = True
    _run_idle_to_blank(ctrl, loop)
    # The deck is idle but lit with the logo, not blanked to black.
    assert ctrl._idle_blanked is True
    assert ctrl._logo_face_active is True
    assert ctrl._logo_paints == 1  # one static frame, no loop
    assert 0 not in deck.brightness_calls
    # A second tick while already idle must not repaint or re-blank.
    loop.run_until_complete(ctrl._idle_loop_once())
    assert ctrl._logo_paints == 1
    loop.close()


def test_idle_timeout_blanks_fully_when_toggle_off():
    ctrl, deck, loop = _make_controller()
    ctrl.config.logo_when_display_off = False
    _run_idle_to_blank(ctrl, loop)
    assert ctrl._idle_blanked is True
    assert ctrl._logo_face_active is False
    assert ctrl._logo_paints == 0  # never lit the logo
    assert deck.brightness_calls[-1] == 0
    assert deck.reset_calls >= 1
    loop.close()


def test_no_logo_when_deck_never_idle_blanks():
    # idle_timeout_minutes <= 0 means the deck never blanks, so there is no
    # black state to replace and the logo never shows.
    ctrl, deck, loop = _make_controller()
    ctrl.config.logo_when_display_off = True
    ctrl.config.idle_timeout_minutes = 0
    ctrl._last_activity = time.monotonic() - 100000
    loop.run_until_complete(ctrl._idle_loop_once())
    assert ctrl._idle_blanked is False
    assert ctrl._logo_face_active is False
    assert ctrl._logo_paints == 0
    loop.close()


def test_key_press_from_idle_logo_wakes_to_keys_and_resets_idle():
    ctrl, deck, loop = _make_controller()
    ctrl.config.logo_when_display_off = True
    _run_idle_to_blank(ctrl, loop)
    assert ctrl._logo_face_active is True and ctrl._idle_blanked is True
    before = time.monotonic()
    deck.press_down(3)
    # The wake coroutine is scheduled on the loop thread; drain it.
    loop.run_until_complete(asyncio.sleep(0.05))
    assert ctrl._logo_face_active is False  # back to the keys
    assert ctrl._idle_blanked is False      # idle state cleared, timer re-armed
    assert ctrl._last_activity >= before    # press reset the idle clock
    assert 3 in ctrl._wake_keys             # the release is swallowed, no action
    assert deck.brightness_calls[-1] > 0    # relit to normal brightness
    assert 3 not in ctrl._key_down_time
    loop.close()


def test_camera_overlay_wins_over_idle_logo():
    # A live full-deck camera overlay holds the keys; the idle blanker must not
    # paint the logo over it.
    ctrl, deck, loop = _make_controller()
    ctrl.config.logo_when_display_off = True
    ctrl._camera_full_active = True
    _run_idle_to_blank(ctrl, loop)
    assert ctrl._idle_blanked is False   # overlay guard skipped the blank
    assert ctrl._logo_face_active is False
    assert ctrl._logo_paints == 0
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
