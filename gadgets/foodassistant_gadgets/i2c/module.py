"""The I2C module: one poll thread, wired into the agent (FoodAssistant-etsc).

Why a thread inside an asyncio daemon: a NeoKey press should light its key and
change the scanner mode before the user's finger is off it, which means
scanning the keys about every 25ms. The agent's event loop also connects to
Bluetooth thermometers, and a slow BLE connect can stall the loop for a
moment; a keypad that stutters when a probe reconnects would feel broken. So
the bus polling gets one small dedicated thread (exactly one, no matter how
many devices are plugged in), and it hands every event to the loop with
``call_soon_threadsafe``. An smbus2 register read is well under a
millisecond, so the thread sleeps almost all the time.

Press to action, end to end: the thread sees the down edge (0 to 25ms), maps
it through the pulled keymap, and hands the loop a coroutine that POSTs
``/pending/scanner-mode``. On localhost that is a couple of milliseconds, so
the mode changes comfortably inside 100ms. The LEDs do not wait for it: the
press repaints them optimistically, and the next outputs poll reconciles if
the server disagreed.
"""
from __future__ import annotations

import logging
import threading
import time

from .bus import BusUnavailable, I2CBus
from .discovery import sweep
from .drivers import neokey

log = logging.getLogger("foodassistant.gadgets.i2c")

# The keypad scan cadence. 25ms gives worst-case sub-50ms key detection while
# costing a handful of microseconds of bus time per cycle.
POLL_SECONDS = 0.025

# How often the bus is swept for new boards. Discovery is the expensive part
# (a ping per known address), and a QT board is plugged in by hand, so
# half a minute is plenty and keeps the bus quiet for the keys.
DISCOVERY_SECONDS = 30

# How often a configured device's heartbeat is pushed, so the Settings card
# can say plugged in or unplugged.
HEARTBEAT_SECONDS = 5

# How long an outputs answer that still echoes the pre-press mode is ignored
# after a key press. It only has to cover one poll round trip: the answer that
# was already in flight when the key went down. Anything longer starts holding
# back real changes made on the touchscreen, which is what made the lit key lag
# seconds behind the screen.
_PRESS_ECHO_SECONDS = 1.5

# How long the thread waits before retrying after the bus goes away. A
# missing bus is a permanent state on most machines (no I2C, or the user is
# not in the i2c group yet), so retrying hard would just burn CPU forever.
BUS_RETRY_SECONDS = 30


class I2CModule:
    """The agent's I2C side: discovery, key polling, and LED output.

    Owns its thread and its bus, and reports through the callbacks the daemon
    hands it, so nothing here knows about HTTP.
    """

    def __init__(self, bus_number: int = 1, *, on_press=None,
                 on_heartbeat=None, on_discovered=None):
        self.bus = I2CBus(bus_number)
        self.bus_number = int(bus_number)
        # Callbacks into the daemon (all optional so this tests standalone).
        self._on_press = on_press
        self._on_heartbeat = on_heartbeat
        self._on_discovered = on_discovered
        # Pulled from the app's GET /gadgets/config "stemma" block.
        self.enabled = False
        self.devices: dict[str, dict] = {}
        # Live drivers, keyed by device id.
        self._drivers: dict[str, object] = {}
        # The latest GET /gadgets/outputs answer, and the mode the LEDs are
        # currently showing (which a press updates optimistically, ahead of
        # the server confirming).
        self.outputs: dict = {}
        # When that answer landed, so the timer bar keeps draining between
        # polls rather than stepping once per poll interval.
        self._outputs_at = 0.0
        self._mode = ""
        self._mode_from_press = 0.0
        # What the keys showed just before the last press, so a poll answer
        # that predates it can be told apart from a real change elsewhere.
        self._mode_before_press = ""
        self._last_key_test = 0.0
        self._last_fired: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- config ------------------------------------------------------------

    def apply_config(self, block) -> None:
        """Take the app's stemma block: {"enabled": bool, "devices": [...]}.

        Called from the config poll, so it must be cheap and must not touch
        the bus: the poll thread notices the new device list on its next
        cycle.
        """
        block = block if isinstance(block, dict) else {}
        devices = {}
        for dev in block.get("devices") or []:
            if not isinstance(dev, dict):
                continue
            dev_id = str(dev.get("id") or "").strip().lower()
            if dev_id:
                devices[dev_id] = dict(dev)
        with self._lock:
            self.enabled = bool(block.get("enabled"))
            # Drop drivers for devices that are gone, so a removed NeoKey
            # stops being polled without a restart.
            for dev_id in list(self._drivers):
                if dev_id not in devices:
                    self._drivers.pop(dev_id, None)
            self.devices = devices

    def apply_outputs(self, data) -> None:
        """Take a GET /gadgets/outputs answer (the LED state to render)."""
        if not isinstance(data, dict):
            return
        with self._lock:
            self.outputs = data
            # When this answer landed, so the timer bar can keep counting down
            # between polls instead of stepping once every poll interval.
            self._outputs_at = time.time()
            mode = str(data.get("scanner_mode") or "")
            if not mode or mode == self._mode:
                return
            # An answer already in flight when a key went down still carries
            # the mode from BEFORE that press, and applying it would snap the
            # lit key back and read as a dropped press. Ignore exactly that
            # echo, briefly. Everything else is a real change made on another
            # surface (the touchscreen, the deck) and has to show at once:
            # holding back EVERY server answer for a few seconds after a press
            # is what made picking a mode on the screen take seconds to reach
            # the keys, which is the one thing the pad is meant to be good at.
            stale_echo = (mode == self._mode_before_press
                          and time.time() - self._mode_from_press <= _PRESS_ECHO_SECONDS)
            if not stale_echo:
                self._mode = mode

    def health(self) -> dict:
        return self.bus.health()

    # -- lifecycle ---------------------------------------------------------

    def start(self, loop) -> None:
        """Start the poll thread. ``loop`` is the daemon's asyncio loop, the
        one every callback is bounced onto."""
        self._loop = loop
        self._thread = threading.Thread(target=self._run, name="i2c-poll",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.bus.close()

    def _dispatch(self, fn, *args) -> None:
        """Hand a callback to the event loop from the poll thread."""
        if not fn:
            return
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            # The loop is closing (shutdown). Dropping the event is correct.
            log.debug("No loop to dispatch an I2C event to")

    # -- the thread --------------------------------------------------------

    def _run(self) -> None:
        """The one thread: scan keys fast, sweep and heartbeat slowly.

        Wrapped so nothing that happens on a plugged-in board can take the
        agent's Bluetooth loops with it.
        """
        next_discovery = 0.0
        next_heartbeat = 0.0
        while not self._stop.is_set():
            now = time.time()
            try:
                if not self.bus.open():
                    # No bus here (not enabled, no permission, or no smbus2).
                    # Report it once and check back rarely.
                    self._stop.wait(BUS_RETRY_SECONDS)
                    continue
                if now >= next_discovery:
                    self._sweep(now)
                    next_discovery = now + DISCOVERY_SECONDS
                if now >= next_heartbeat:
                    self._heartbeat(now)
                    next_heartbeat = now + HEARTBEAT_SECONDS
                self._poll_keys(now)
                self._render(now)
            except BusUnavailable as exc:
                # The cable came out mid-cycle. Every driver forgets its
                # state so a replug re-initializes cleanly.
                log.warning("I2C bus went away: %s", exc)
                with self._lock:
                    for driver in self._drivers.values():
                        if hasattr(driver, "invalidate"):
                            driver.invalidate()
                self._stop.wait(1.0)
                continue
            except Exception:  # noqa: BLE001 - the thread must never die
                log.exception("I2C poll cycle failed; continuing")
                self._stop.wait(1.0)
                continue
            self._stop.wait(POLL_SECONDS)

    def _driver_for(self, dev_id: str, dev: dict):
        """The live driver for a configured device, made on first use."""
        driver = self._drivers.get(dev_id)
        if driver is not None:
            return driver
        parsed = _parse_id(dev_id)
        if not parsed:
            return None
        kind = str(dev.get("kind") or "").strip().lower()
        if kind != neokey.KIND:
            return None  # phase 2 devices land here
        driver = neokey.NeoKey(self.bus, parsed[1])
        self._drivers[dev_id] = driver
        return driver

    def _sweep(self, now: float) -> None:
        """Report what is on the bus but not configured yet.

        Runs whether or not the class is enabled: the discovered list is how
        a user turns the feature on in the first place, the same way the BLE
        scan keeps its add list warm while thermometers are off.
        """
        try:
            found = sweep(self.bus, self.bus_number)
        except BusUnavailable:
            raise
        except Exception:  # noqa: BLE001
            log.exception("I2C sweep failed")
            return
        with self._lock:
            configured = set(self.devices)
        fresh = [f for f in found if f["id"] not in configured]
        if fresh:
            self._dispatch(self._on_discovered, fresh)

    def _heartbeat(self, now: float) -> None:
        """Push a last-seen for each configured device that still answers.

        Bus-powered, so there is no battery and no reading: whether the board
        ACKs IS the reading, and it is what makes an unplugged NeoKey's card
        go stale instead of lying.
        """
        with self._lock:
            devices = dict(self.devices)
        beats = []
        for dev_id, dev in devices.items():
            parsed = _parse_id(dev_id)
            if not parsed:
                continue
            if not self.bus.ping(parsed[1]):
                continue
            beats.append({"id": dev_id, "kind": "stemma",
                          "model": str(dev.get("kind") or ""),
                          "name": str(dev.get("name") or "")})
        if beats:
            self._dispatch(self._on_heartbeat, beats)

    def _poll_keys(self, now: float) -> None:
        """One scan of every configured NeoKey."""
        with self._lock:
            enabled = self.enabled
            devices = dict(self.devices)
        if not enabled:
            return
        for dev_id, dev in devices.items():
            if str(dev.get("kind") or "").lower() != neokey.KIND:
                continue
            driver = self._driver_for(dev_id, dev)
            if driver is None:
                continue
            keys = driver.scan(now, self._last_fired.setdefault(dev_id, {}))
            for key in keys:
                self._press(dev_id, dev, key, now)

    def _press(self, dev_id: str, dev: dict, key: int, now: float) -> None:
        """Turn one key-down into a mode change."""
        keymap = neokey.normalize_keymap(
            (dev.get("options") or {}).get("keymap"))
        mode = keymap[key] if key < len(keymap) else ""
        if not mode:
            log.debug("NeoKey %s key %s is mapped to nothing; ignoring",
                      dev_id, key + 1)
            return
        # Optimistic: the LEDs move now, not when the server answers, so the
        # key feels instant even if the app is briefly slow.
        with self._lock:
            self._mode_before_press = self._mode
            self._mode = mode
            self._mode_from_press = now
        log.info("NeoKey %s key %s selects scanner mode %s",
                 dev_id, key + 1, mode)
        self._dispatch(self._on_press, mode)

    def _render(self, now: float) -> None:
        """Paint every NeoKey's LEDs for the mode we believe is active.

        The driver skips a frame identical to the one already showing, so
        this costs nothing on the vast majority of cycles.
        """
        with self._lock:
            devices = dict(self.devices)
            mode = self._mode
            outputs = dict(self.outputs)
            outputs_at = self._outputs_at
        palette = _palette(outputs)
        timer = _timer_view(outputs, outputs_at, now)
        key_test = outputs.get("key_test") if isinstance(outputs, dict) else None
        for dev_id, dev in devices.items():
            if str(dev.get("kind") or "").lower() != neokey.KIND:
                continue
            driver = self._drivers.get(dev_id)
            if driver is None:
                continue
            options = dev.get("options") or {}
            brightness = options.get("brightness", 40)
            # A Test click from Settings flashes one key white. Each request
            # carries a timestamp and fires once, so it can ride several
            # outputs polls without strobing.
            if (isinstance(key_test, dict)
                    and str(key_test.get("id") or "").lower() == dev_id
                    and float(key_test.get("ts") or 0) > self._last_key_test):
                self._last_key_test = float(key_test.get("ts") or 0)
                driver.show(neokey.test_colors(int(key_test.get("key") or 0),
                                               brightness), force=True)
                time.sleep(0.4)
            if timer is not None:
                # A running timer takes the pad over: the four keys become one
                # draining bar, which is readable from across the kitchen in a
                # way a number on a screen is not. Once it comes due the whole
                # pad strobes instead.
                if timer.get("ringing"):
                    driver.show(neokey.alarm_strobe_colors(
                        timer["color"], brightness, timer["phase"]))
                else:
                    driver.show(neokey.timer_colors(
                        timer["remaining"], timer["total"], timer["color"],
                        brightness, timer["phase"]))
                continue
            driver.show(neokey.led_colors(options.get("keymap"), mode,
                                          brightness, palette))


# One full breath of the timer bar's leading key, in seconds. Slow enough to
# read as breathing rather than blinking from across the room.
PULSE_PERIOD = 2.4

# One on/off cycle of the finished-timer strobe. Fast enough to read as an
# alarm from the doorway, slow enough not to be unpleasant in a dark kitchen.
STROBE_PERIOD = 0.6


def _timer_view(outputs: dict, outputs_at: float, now: float) -> dict | None:
    """What the keys show for a running timer, or None to show the modes.

    The remaining time is carried forward from when the answer landed, so the
    bar drains smoothly at the render rate instead of stepping once per poll.
    """
    timer = (outputs or {}).get("timer")
    if not isinstance(timer, dict):
        return None
    if timer.get("ringing"):
        # Time is up. The whole pad strobes, because a finished timer is the
        # one thing here worth interrupting someone for.
        return {"ringing": True, "color": _rgb(timer.get("color")),
                "phase": (now % STROBE_PERIOD) / STROBE_PERIOD}
    if not timer.get("running"):
        return None
    left = timer.get("soonest_remaining")
    if left is None:
        return None
    try:
        age = max(0.0, now - (outputs_at or now))
        remaining = max(0.0, float(left) - age)
    except (TypeError, ValueError):
        return None
    return {
        "ringing": False,
        "remaining": remaining,
        "total": timer.get("soonest_total"),
        "color": _rgb(timer.get("color")),
        "phase": (now % PULSE_PERIOD) / PULSE_PERIOD,
    }


def _rgb(value) -> tuple | None:
    """A three-channel color from the wire, or None to use the driver's own."""
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            return tuple(max(0, min(255, int(c))) for c in value)
        except (TypeError, ValueError):
            return None
    return None


def _palette(outputs: dict) -> dict | None:
    """The app's mode colors from an outputs answer, or None to use ours."""
    colors = (outputs or {}).get("mode_colors")
    if not isinstance(colors, dict):
        return None
    out = {}
    for mode, value in colors.items():
        if isinstance(value, (list, tuple)) and len(value) == 3:
            out[str(mode)] = tuple(int(c) for c in value)
    return out or None


def _parse_id(dev_id: str) -> tuple[int, int] | None:
    """(bus, address) from an i2c:1:0x30 id, or None."""
    parts = str(dev_id or "").split(":")
    if len(parts) != 3 or parts[0] != "i2c":
        return None
    try:
        return int(parts[1]), int(parts[2], 16)
    except ValueError:
        return None
