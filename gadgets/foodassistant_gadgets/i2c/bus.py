"""The I2C bus wrapper (FoodAssistant-etsc).

One bus, several drivers, so this owns the smbus2 handle and the lock that
serializes transactions across them. The kernel's i2c-dev driver serializes
each transaction on its own, but a driver that does read-modify-write (the
seesaw NeoPixel buffer, say) needs its own transactions kept together, and a
single lock is cheap next to the microseconds a register read takes.

The hard rule here is that a missing or unreadable bus must never take the
agent down. A Pi with I2C turned off, a server with no bus at all, and a user
not yet in the ``i2c`` group are all normal states for this module, not
errors: it logs the reason once, marks itself unavailable, and the agent's
Bluetooth loops carry on as if it had never been asked. The reason string is
pushed to the app so Settings can say what is actually wrong instead of
showing an empty list.

smbus2 is imported lazily, so the package imports (and the tests run) on a
machine that has never seen an I2C device.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("foodassistant.gadgets.i2c")

# The bus a Pi's STEMMA QT connector and its header I2C pins both land on.
DEFAULT_BUS = 1


class BusUnavailable(Exception):
    """The bus cannot be used (missing device node, no permission, no smbus2).

    Carries the human-readable reason that reaches the Settings pane."""


class I2CBus:
    """A locked smbus2 handle with honest degradation.

    Every read/write raises BusUnavailable rather than the underlying OSError,
    so callers have exactly one thing to catch, and the availability flag plus
    its detail string are what the agent reports to the app.
    """

    def __init__(self, bus_number: int = DEFAULT_BUS):
        self.bus_number = int(bus_number)
        self._bus = None
        self._lock = threading.RLock()
        self.available = False
        self.detail = ""
        # Whether the unavailable reason has been logged. A poll thread hits
        # this path every cycle; the journal only needs to hear it once.
        self._logged = False

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> bool:
        """Open the bus if it is not open. False (never a raise) on failure."""
        with self._lock:
            if self._bus is not None:
                return True
            try:
                from smbus2 import SMBus  # noqa: PLC0415 - lazy on purpose
            except ImportError as exc:
                self._unavailable(
                    "The smbus2 package is not installed on this device, so "
                    "plug-in accessories cannot be read. Re-run "
                    "foodassistant-gadgets-setup to install it.", exc)
                return False
            try:
                self._bus = SMBus(self.bus_number)
            except PermissionError as exc:
                self._unavailable(
                    f"No permission to use /dev/i2c-{self.bus_number}. Re-run "
                    "foodassistant-gadgets-setup to add this device's service "
                    "user to the i2c group, then reboot.", exc)
                return False
            except (FileNotFoundError, OSError) as exc:
                self._unavailable(
                    f"/dev/i2c-{self.bus_number} is not there, so this device "
                    "has no I2C bus turned on. Re-run "
                    "foodassistant-gadgets-setup to enable it, then reboot.",
                    exc)
                return False
            self.available = True
            self.detail = ""
            self._logged = False
            log.info("I2C bus %s open", self.bus_number)
            return True

    def close(self) -> None:
        with self._lock:
            if self._bus is not None:
                try:
                    self._bus.close()
                except Exception:  # noqa: BLE001 - closing must not raise
                    pass
                self._bus = None

    def _unavailable(self, detail: str, exc: Exception | None = None) -> None:
        self.available = False
        self.detail = detail
        if not self._logged:
            log.warning("I2C unavailable: %s (%s)", detail, exc)
            self._logged = True

    def _fail(self, exc: Exception) -> BusUnavailable:
        """Turn a live-bus error into our exception, dropping the handle so
        the next call reopens. A QT cable pulled mid-read shows up as an EIO
        storm; reopening is how a replug recovers without a restart."""
        self.close()
        self._unavailable(
            f"The I2C bus stopped responding ({exc}). Check the accessory's "
            "cable; it recovers on its own once it is plugged back in.", exc)
        return BusUnavailable(self.detail)

    # -- transactions ------------------------------------------------------

    def ping(self, address: int) -> bool:
        """Whether anything answers at this address.

        A one-byte read, not the quick-write probe i2cdetect defaults to:
        quick-write is known to upset some parts (it looks like a write with
        no data), and every device this agent probes tolerates a read.
        """
        if not self.open():
            return False
        with self._lock:
            try:
                self._bus.read_byte(address)
                return True
            except OSError:
                # No ACK is the normal answer for an empty address, so this
                # is not a bus failure and must not mark the bus unavailable.
                return False

    def write_bytes(self, address: int, data: bytes) -> None:
        if not self.open():
            raise BusUnavailable(self.detail)
        from smbus2 import i2c_msg  # noqa: PLC0415
        with self._lock:
            try:
                self._bus.i2c_rdwr(i2c_msg.write(address, data))
            except OSError as exc:
                raise self._fail(exc) from exc

    def read_bytes(self, address: int, length: int) -> bytes:
        if not self.open():
            raise BusUnavailable(self.detail)
        from smbus2 import i2c_msg  # noqa: PLC0415
        with self._lock:
            try:
                msg = i2c_msg.read(address, length)
                self._bus.i2c_rdwr(msg)
                return bytes(bytearray(list(msg)))
            except OSError as exc:
                raise self._fail(exc) from exc

    def health(self) -> dict:
        """The block the agent pushes to the app: available plus the reason."""
        return {"available": bool(self.available), "detail": self.detail}
