"""Adafruit NeoKey 1x4: the physical scan-mode selector (FoodAssistant-kh1m).

Four mechanical keys, each with a NeoPixel under it, on a seesaw board. Key 1
through 4 select the scanner modes, the active mode's key stays lit in its
mode color, and a press changes the mode everywhere: the kiosk, the Stream
Deck, and any other surface follow within their own poll.

The keys are seesaw GPIO pins 4 through 7 with pullups on, not the seesaw
keypad module (that one belongs to the NeoTrellis matrix). So a scan is one
bulk read of all 32 GPIO pins, and a pressed key reads 0. The LEDs hang off
seesaw pin 3 as a four-pixel strip.

Everything that turns bytes into key events is pure and lives at module
level, so the whole decode path tests without a NeoKey on the desk.
"""
from __future__ import annotations

import logging

from ..bus import BusUnavailable
from ..seesaw import Seesaw

log = logging.getLogger("foodassistant.gadgets.i2c")

KIND = "neokey"

# The four addresses the board's A0/A1 jumpers select. Straight out of the
# box it answers at 0x30; the others exist so two NeoKeys can share a bus.
ADDRESSES = (0x30, 0x31, 0x32, 0x33)

# The seesaw pins the keys sit on, in physical left-to-right order.
KEY_PINS = (4, 5, 6, 7)
KEY_COUNT = len(KEY_PINS)

# The seesaw pin driving the four NeoPixels.
NEOPIXEL_PIN = 3

# A press repeated inside this window is swallowed. The seesaw debounces the
# switch itself; this is about a human holding a key down or double-tapping
# it, which should not fire the same mode change twice.
REPEAT_SECONDS = 0.2

# The palette the agent falls back to when the app has not answered an
# outputs poll yet. The app owns these colors (service/app/services/stemma.py
# MODE_COLORS); tests/test_stemma.py asserts the two tables agree, so a
# change there cannot silently drift from what the keys show.
MODE_COLORS = {
    "inventory": (0, 200, 83),
    "consume": (255, 145, 0),
    "shopping": (0, 145, 255),
    "audit": (170, 0, 255),
}

# The scanner modes, in the app's SCANNER_MODES order, which is also the
# out-of-the-box key order: key 1 is Stock and key 4 is Audit.
SCANNER_MODES = ("inventory", "consume", "shopping", "audit")
DEFAULT_KEYMAP = SCANNER_MODES

# What a mapped-but-inactive key shows: its color at this fraction of the
# brightness, matching the app's IDLE_LED_SCALE.
IDLE_LED_SCALE = 0.12


# --------------------------------------------------------------------------
# Pure decode and derivation
# --------------------------------------------------------------------------

def pressed_keys(bulk: int) -> tuple[int, ...]:
    """Key indexes currently held down, from one GPIO bulk read.

    The pins are pulled up, so a key that is DOWN reads 0. Returned as
    indexes (0..3) in physical order, which is what the keymap is indexed by.
    """
    try:
        value = int(bulk)
    except (TypeError, ValueError):
        return ()
    return tuple(i for i, pin in enumerate(KEY_PINS)
                 if not (value >> pin) & 1)


def key_events(previous: tuple[int, ...], current: tuple[int, ...]) -> tuple[int, ...]:
    """The keys newly pressed between two scans.

    Only the down edge fires an action: a mode change on key-up would feel
    late, and firing on both would fire twice. Holding a key produces one
    event, because it stays in ``current`` and never re-enters.
    """
    return tuple(k for k in current if k not in previous)


def swallow_repeats(last_fired: dict, keys, now: float,
                    window: float = REPEAT_SECONDS) -> tuple[int, ...]:
    """Drop keys that fired within the repeat window, and record the rest.

    Mutates ``last_fired`` (key index -> epoch) the way the caller's poll
    thread wants: pure in the sense that time and state are arguments, so a
    test walks it through a whole press sequence with no clock.
    """
    out = []
    for key in keys or ():
        prev = last_fired.get(key)
        if prev is not None and (now - prev) < window:
            continue
        last_fired[key] = now
        out.append(key)
    return tuple(out)


def normalize_keymap(raw) -> list[str]:
    """The pulled keymap, cleaned to exactly KEY_COUNT entries.

    The app normalizes this too, and the two must agree exactly (the tests
    pin them together) or a key would do one thing while the settings card
    claimed another. The agent repeats the work because a config pull can
    carry anything: an old settings file, a hand-edited TOML, or a device
    added before the options block existed.

    No usable list at all means the default order, so a NeoKey works the
    moment it is plugged in; junk inside a real list becomes "" (the key does
    nothing), because a typo must never make a key consume stock.
    """
    if not isinstance(raw, (list, tuple)):
        return list(DEFAULT_KEYMAP)
    out: list[str] = []
    for i in range(KEY_COUNT):
        value = raw[i] if i < len(raw) else None
        mode = str(value or "").strip().lower()
        out.append(mode if mode in SCANNER_MODES else "")
    return out


def scale_color(color, factor: float) -> tuple[int, int, int]:
    f = max(0.0, min(1.0, float(factor)))
    return tuple(max(0, min(255, int(round(c * f)))) for c in color)


def led_colors(keymap, active_mode: str, brightness: int = 40,
               palette: dict | None = None) -> list[tuple[int, int, int]]:
    """What the four LEDs should show for an active mode.

    Mirrors the app's stemma.led_colors: the active mode's key at full
    brightness, other mapped keys at a faint glow so the layout is readable
    in the dark, unmapped keys off. ``palette`` lets the outputs poll hand in
    the app's colors; without it the built-in table is used.
    """
    colors = palette or MODE_COLORS
    keys = normalize_keymap(keymap)
    try:
        level = max(0, min(100, int(brightness))) / 100.0
    except (TypeError, ValueError):
        level = 0.4
    active = str(active_mode or "").strip().lower()
    out = []
    for mode in keys:
        if not mode:
            out.append((0, 0, 0))
            continue
        base = tuple(colors.get(mode) or MODE_COLORS.get(mode) or (0, 0, 0))
        out.append(scale_color(base, level if mode == active
                               else level * IDLE_LED_SCALE))
    return out


# The timer bar's resting color, the brand pink, when the app has not sent one.
TIMER_COLOR = (242, 0, 110)

# How far the leading key dims at the bottom of its breath. Shallow on purpose:
# a hard blink on the counter reads as an alarm, and the timer is not one until
# it actually rings.
PULSE_FLOOR = 0.55


def timer_bar_levels(remaining, total, keys: int = KEY_COUNT) -> list[float]:
    """How lit each key is for a running timer, 0.0 to 1.0 each. Pure.

    The four keys are one bar that drains as the soonest timer comes due, so a
    glance at the counter says roughly how long is left without reading a
    number. The bar empties from the right, and the key at its head holds the
    part-filled remainder (that is the one that breathes).

    A timer that never recorded a duration has no proportion to show, so it
    holds a full bar rather than guessing at one.
    """
    try:
        left = max(0.0, float(remaining))
        whole = float(total or 0)
    except (TypeError, ValueError):
        return [1.0] * keys
    if whole <= 0:
        return [1.0] * keys
    lit = max(0.0, min(1.0, left / whole)) * keys
    out = []
    for i in range(keys):
        if lit >= i + 1:
            out.append(1.0)
        elif lit > i:
            out.append(lit - i)          # the head of the bar, part filled
        else:
            out.append(0.0)
    # The pad stands as a column beside the display, so the bar has to empty
    # the way a glass does: from the top down, with the last key still lit as
    # the timer comes due. Filling from the first key drains it upward, which
    # reads upside down on the counter.
    return out[::-1]


def pulse_scale(phase: float, floor: float = PULSE_FLOOR) -> float:
    """One gentle breath, as a brightness multiplier. Pure.

    ``phase`` walks 0 to 1 through a full cycle. A cosine (not a triangle) so
    the light eases at the top and bottom of the breath instead of snapping
    around, which is the difference between breathing and flickering.
    """
    import math
    try:
        p = float(phase) % 1.0
    except (TypeError, ValueError):
        p = 0.0
    rise = 0.5 - 0.5 * math.cos(2 * math.pi * p)      # 0 -> 1 -> 0, smoothly
    return floor + (1.0 - floor) * rise


def timer_colors(remaining, total, color=None, brightness: int = 40,
                 phase: float = 0.0) -> list[tuple[int, int, int]]:
    """The four keys as a progress bar for the soonest-finishing timer. Pure.

    Only the head of the bar breathes. Pulsing the whole row would turn the
    pad into a beacon in a dark kitchen, and it would also throw away the one
    thing the animation is for: showing which key is the live edge of the
    countdown.
    """
    try:
        level = max(0, min(100, int(brightness))) / 100.0
    except (TypeError, ValueError):
        level = 0.4
    base = tuple(color or TIMER_COLOR)
    levels = timer_bar_levels(remaining, total)
    head = _bar_head(levels)
    out = []
    for i, filled in enumerate(levels):
        if filled <= 0:
            out.append((0, 0, 0))
            continue
        scale = level * filled
        if i == head:
            scale *= pulse_scale(phase)
        out.append(scale_color(base, scale))
    return out


def _bar_head(levels) -> int:
    """The index of the key at the head of the bar: the part-filled one, or the
    lit key nearest the spent end when it lands exactly on a boundary."""
    for i, filled in enumerate(levels):
        if 0 < filled < 1.0:
            return i
    lit = [i for i, filled in enumerate(levels) if filled > 0]
    return min(lit) if lit else -1


def alarm_strobe_colors(color=None, brightness: int = 40,
                        phase: float = 0.0) -> list[tuple[int, int, int]]:
    """All four keys strobing together: a timer has finished. Pure.

    A hard square wave, not the bar's breath. The countdown pulse is meant to
    sit quietly in the corner of your eye; this one has to pull you back to the
    kitchen, and the two must never be mistakable for each other.
    """
    try:
        level = max(0, min(100, int(brightness))) / 100.0
    except (TypeError, ValueError):
        level = 0.4
    try:
        lit = (float(phase) % 1.0) < 0.5
    except (TypeError, ValueError):
        lit = True
    if not lit:
        return [(0, 0, 0)] * KEY_COUNT
    return [scale_color(tuple(color or TIMER_COLOR), level)] * KEY_COUNT


def test_colors(key: int, brightness: int = 40) -> list[tuple[int, int, int]]:
    """The frame a Test click shows: the named key white, the rest dark, so
    the user can see which physical key the card means."""
    try:
        level = max(0, min(100, int(brightness))) / 100.0
    except (TypeError, ValueError):
        level = 0.4
    return [scale_color((255, 255, 255), level) if i == key else (0, 0, 0)
            for i in range(KEY_COUNT)]


# --------------------------------------------------------------------------
# The device
# --------------------------------------------------------------------------

def probe(bus, address: int) -> str | None:
    """Whether a NeoKey lives at this address (the discovery contract).

    Two gates, because 0x30 is not exclusively ours: the board must answer
    the seesaw hardware-id handshake, and its module inventory must carry
    both GPIO and NeoPixel. A non-seesaw part at the same address fails the
    first, and a seesaw build without LEDs fails the second.
    """
    from ..seesaw import GPIO_BASE, NEOPIXEL_BASE
    try:
        chip = Seesaw(bus, address)
        if not chip.hw_id():
            return None
        modules = chip.modules()
    except BusUnavailable:
        return None
    except OSError:
        return None
    if GPIO_BASE in modules and NEOPIXEL_BASE in modules:
        return KIND
    return None


class NeoKey:
    """One configured NeoKey: scan its keys, drive its LEDs."""

    def __init__(self, bus, address: int):
        self.seesaw = Seesaw(bus, address)
        self.address = int(address)
        self._ready = False
        # The last frame written, so an unchanged mode costs no bus traffic.
        self._last_colors: list | None = None
        self._held: tuple[int, ...] = ()

    def begin(self) -> None:
        """Set the keys up as pulled-up inputs and the LEDs as a strip.

        Re-runnable: a replugged board comes back through here, which is what
        makes recovery work without restarting the agent.
        """
        self.seesaw.pin_mode_input_pullup(KEY_PINS)
        self.seesaw.neopixel_init(NEOPIXEL_PIN, KEY_COUNT)
        self._ready = True
        self._last_colors = None

    def scan(self, now: float, last_fired: dict) -> tuple[int, ...]:
        """One poll: the keys newly pressed, repeats already swallowed."""
        if not self._ready:
            self.begin()
        current = pressed_keys(self.seesaw.digital_read_bulk())
        fresh = key_events(self._held, current)
        self._held = current
        return swallow_repeats(last_fired, fresh, now)

    def show(self, colors, force: bool = False) -> None:
        """Write the LEDs, skipping a frame that is already on screen."""
        frame = [tuple(c) for c in colors]
        if not force and frame == self._last_colors:
            return
        if not self._ready:
            self.begin()
        self.seesaw.neopixel_write(frame)
        self._last_colors = frame

    def invalidate(self) -> None:
        """Forget the board's state after a bus error, so the next cycle
        re-initializes it rather than trusting a handle that may have been
        replugged into a different board."""
        self._ready = False
        self._last_colors = None
        self._held = ()
