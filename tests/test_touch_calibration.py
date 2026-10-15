"""Touch-calibration tap capture: pure event-folding logic (FoodAssistant-9ext).

The calibration SSE stream reads raw evdev input_event structs and turns each
BTN_TOUCH release into a tap. These tests exercise the pure folding helper with
hand-packed event buffers, covering the first-tap and repeated-coordinate races
seen on the Pi 3 satellite: the kernel input core suppresses ABS events whose
value did not change, so a release can arrive without fresh coordinates.

Run: python -m pytest tests/test_touch_calibration.py -q
"""
import struct

from app.routers.setup import (
    _fold_touch_events,
    _looks_like_touchscreen,
    _INPUT_EVENT_FORMAT,
    _EV_ABS,
    _EV_KEY,
    _ABS_X,
    _ABS_Y,
    _BTN_TOUCH,
)


def _ev(etype, code, value):
    return struct.pack(_INPUT_EVENT_FORMAT, 0, 0, etype, code, value)


def _buf(*events):
    return b"".join(events)


def test_tap_emitted_on_release_after_coordinates():
    data = _buf(
        _ev(_EV_KEY, _BTN_TOUCH, 1),
        _ev(_EV_ABS, _ABS_X, 1200),
        _ev(_EV_ABS, _ABS_Y, 800),
        _ev(_EV_KEY, _BTN_TOUCH, 0),
    )
    taps, x, y = _fold_touch_events(data, None, None)
    assert taps == [(1200, 800)]
    assert (x, y) == (1200, 800)


def test_release_without_any_position_is_dropped():
    # No coordinates ever seen (and no seed): the release cannot be located.
    data = _buf(_ev(_EV_KEY, _BTN_TOUCH, 1), _ev(_EV_KEY, _BTN_TOUCH, 0))
    taps, x, y = _fold_touch_events(data, None, None)
    assert taps == []
    assert (x, y) == (None, None)


def test_first_tap_uses_seeded_position():
    # The kernel suppresses ABS events whose value did not change, so the very
    # first tap after the stream opens can be a bare press/release. The seed
    # from EVIOCGABS (the device's last reported position) locates it.
    data = _buf(_ev(_EV_KEY, _BTN_TOUCH, 1), _ev(_EV_KEY, _BTN_TOUCH, 0))
    taps, x, y = _fold_touch_events(data, 2000, 1500)
    assert taps == [(2000, 1500)]
    assert (x, y) == (2000, 1500)


def test_second_tap_with_one_unchanged_axis_reuses_last_value():
    # Two corners in line share a raw axis value (top-left then bottom-left:
    # same X). The second tap arrives with only a fresh Y; the carried X must
    # locate it instead of dropping the release.
    first = _buf(
        _ev(_EV_ABS, _ABS_X, 300),
        _ev(_EV_ABS, _ABS_Y, 300),
        _ev(_EV_KEY, _BTN_TOUCH, 0),
    )
    taps, x, y = _fold_touch_events(first, None, None)
    assert taps == [(300, 300)]

    second = _buf(
        _ev(_EV_KEY, _BTN_TOUCH, 1),
        _ev(_EV_ABS, _ABS_Y, 3700),
        _ev(_EV_KEY, _BTN_TOUCH, 0),
    )
    taps, x, y = _fold_touch_events(second, x, y)
    assert taps == [(300, 3700)]
    assert (x, y) == (300, 3700)


def test_multiple_taps_in_one_buffer():
    data = _buf(
        _ev(_EV_ABS, _ABS_X, 100),
        _ev(_EV_ABS, _ABS_Y, 200),
        _ev(_EV_KEY, _BTN_TOUCH, 0),
        _ev(_EV_ABS, _ABS_X, 900),
        _ev(_EV_KEY, _BTN_TOUCH, 0),
    )
    taps, _, _ = _fold_touch_events(data, None, None)
    assert taps == [(100, 200), (900, 200)]


def test_press_events_and_other_codes_are_ignored():
    data = _buf(
        _ev(_EV_KEY, _BTN_TOUCH, 1),   # press: not a tap
        _ev(_EV_ABS, 0x18, 255),       # ABS_PRESSURE: ignored
        _ev(_EV_ABS, _ABS_X, 50),
        _ev(_EV_ABS, _ABS_Y, 60),
    )
    taps, x, y = _fold_touch_events(data, None, None)
    assert taps == []
    assert (x, y) == (50, 60)


def test_trailing_partial_struct_is_ignored():
    whole = _buf(
        _ev(_EV_ABS, _ABS_X, 10),
        _ev(_EV_ABS, _ABS_Y, 20),
        _ev(_EV_KEY, _BTN_TOUCH, 0),
    )
    taps, _, _ = _fold_touch_events(whole + b"\x00\x01\x02", None, None)
    assert taps == [(10, 20)]


def test_looks_like_touchscreen_by_name_hint():
    block = (
        'N: Name="ADS7846 Touchscreen"\n'
        "H: Handlers=mouse0 event1\n"
        "B: PROP=0\nB: ABS=1000003\n"
    )
    assert _looks_like_touchscreen(block) is True


def test_looks_like_touchscreen_by_direct_prop():
    block = (
        'N: Name="Some Unbranded Panel"\n'
        "H: Handlers=event3\n"
        "B: PROP=2\nB: ABS=260800000000003\n"
    )
    assert _looks_like_touchscreen(block) is True


def test_keyboard_is_not_a_touchscreen():
    block = 'N: Name="USB Keyboard"\nH: Handlers=kbd event0\nB: PROP=0\nB: KEY=fff\n'
    assert _looks_like_touchscreen(block) is False
