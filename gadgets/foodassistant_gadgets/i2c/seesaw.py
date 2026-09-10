"""The shared seesaw mini-driver (FoodAssistant-etsc).

Adafruit's seesaw boards (NeoKey 1x4, the ANO encoder breakout, the NeoDriver)
are not flat register machines: a small microcontroller on the board exposes
modules (GPIO, NeoPixel, encoder, keypad, status) reached by writing a
two-byte address, ``[module_base, function]``, optionally followed by data. A
read is that same two-byte write, a short pause while the chip prepares the
answer, and then a plain read of N bytes.

One driver serves all three launch boards, which is exactly why hand-rolling
beats pulling in Blinka and a CircuitPython package per board: the protocol is
small, well documented, and identical across them.

Everything that decodes bytes is a module-level pure function so it tests
without a bus. The class only adds the transactions.
"""
from __future__ import annotations

import time

from .bus import BusUnavailable, I2CBus

# -- Module bases ------------------------------------------------------------

STATUS_BASE = 0x00
GPIO_BASE = 0x01
NEOPIXEL_BASE = 0x0E
ENCODER_BASE = 0x11
KEYPAD_BASE = 0x10

# -- Status module -----------------------------------------------------------

STATUS_HW_ID = 0x01
STATUS_VERSION = 0x02
STATUS_OPTIONS = 0x03
STATUS_SWRST = 0x7F

# -- GPIO module -------------------------------------------------------------

GPIO_DIRSET_BULK = 0x02
GPIO_DIRCLR_BULK = 0x03
GPIO_BULK = 0x04
GPIO_BULK_SET = 0x05
GPIO_BULK_CLR = 0x06
GPIO_PULLENSET = 0x0B
GPIO_PULLENCLR = 0x0C

# -- NeoPixel module ---------------------------------------------------------

NEOPIXEL_PIN = 0x01
NEOPIXEL_SPEED = 0x02
NEOPIXEL_BUF_LENGTH = 0x03
NEOPIXEL_BUF = 0x04
NEOPIXEL_SHOW = 0x05

# The seesaw firmware's hardware ids, by the chip the board is built on. The
# NeoKey 1x4 has shipped on both the SAMD09 (early boards) and the ATtiny817
# (current ones), so a probe that only accepted one would call half the
# NeoKeys in the wild unsupported.
HW_IDS = {
    0x55: "samd09",
    0x84: "attiny806",
    0x85: "attiny807",
    0x86: "attiny816",
    0x87: "attiny817",
}

# How long the chip needs between the command write and the read. Adafruit's
# driver uses 250us for ordinary reads; the SAMD09's NeoPixel and EEPROM
# writes want longer, which the callers below pass explicitly.
READ_DELAY = 0.0005


# --------------------------------------------------------------------------
# Pure decoders
# --------------------------------------------------------------------------

def decode_hw_id(value: int) -> str:
    """The chip name for a seesaw hardware id byte, or "" when it is not a
    seesaw at all. This is the handshake that tells a real NeoDriver at 0x60
    from a PCA9685 board sitting at the same address."""
    try:
        return HW_IDS.get(int(value), "")
    except (TypeError, ValueError):
        return ""


def decode_options(data: bytes) -> set[int]:
    """The module inventory from the STATUS_OPTIONS register: a 32-bit
    big-endian mask whose bits are module base numbers. A NeoKey answers with
    GPIO and NeoPixel set, which is what confirms the board really is a
    keypad-with-LEDs and not some other seesaw build.
    """
    if not data or len(data) < 4:
        return set()
    mask = int.from_bytes(bytes(data[:4]), "big")
    return {bit for bit in range(32) if mask & (1 << bit)}


def decode_bulk(data: bytes) -> int:
    """The 32-bit GPIO state from a GPIO_BULK read (big-endian)."""
    if not data or len(data) < 4:
        return 0
    return int.from_bytes(bytes(data[:4]), "big")


def pin_mask(pins) -> int:
    """A 32-bit mask from an iterable of pin numbers."""
    mask = 0
    for pin in pins or ():
        mask |= 1 << int(pin)
    return mask


def encode_pixel(color, order: str = "GRB") -> bytes:
    """One pixel's wire bytes. NeoPixels take green first; the order argument
    exists because the NeoDriver can be wired to RGBW strips later."""
    r, g, b = (max(0, min(255, int(c))) for c in color)
    channels = {"R": r, "G": g, "B": b}
    return bytes(channels[c] for c in order)


def encode_pixel_buffer(colors, order: str = "GRB") -> bytes:
    """The whole strip's bytes, in one buffer write."""
    out = bytearray()
    for color in colors or ():
        out += encode_pixel(color, order)
    return bytes(out)


# --------------------------------------------------------------------------
# The driver
# --------------------------------------------------------------------------

class Seesaw:
    """Transactions against one seesaw board at one address."""

    def __init__(self, bus: I2CBus, address: int):
        self.bus = bus
        self.address = int(address)

    # -- primitives --------------------------------------------------------

    def write(self, base: int, function: int, data: bytes = b"") -> None:
        self.bus.write_bytes(self.address, bytes([base, function]) + bytes(data))

    def read(self, base: int, function: int, length: int,
             delay: float = READ_DELAY) -> bytes:
        self.bus.write_bytes(self.address, bytes([base, function]))
        # The chip needs a moment to fill its answer; reading too early gives
        # stale or zero bytes rather than an error, which is worse.
        time.sleep(delay)
        return self.bus.read_bytes(self.address, length)

    # -- status ------------------------------------------------------------

    def hw_id(self) -> str:
        """The chip name, or "" when nothing seesaw-shaped is here."""
        try:
            data = self.read(STATUS_BASE, STATUS_HW_ID, 1)
        except BusUnavailable:
            raise
        except OSError:
            return ""
        return decode_hw_id(data[0]) if data else ""

    def modules(self) -> set[int]:
        """Which module bases this board's firmware carries."""
        return decode_options(self.read(STATUS_BASE, STATUS_OPTIONS, 4))

    # -- GPIO --------------------------------------------------------------

    def pin_mode_input_pullup(self, pins) -> None:
        """Make pins inputs with their pullups on, which is how every seesaw
        board wires a button: pressed pulls the pin to ground, so a pressed
        key reads 0."""
        mask = pin_mask(pins)
        self.write(GPIO_BASE, GPIO_DIRCLR_BULK, mask.to_bytes(4, "big"))
        self.write(GPIO_BASE, GPIO_PULLENSET, mask.to_bytes(4, "big"))
        self.write(GPIO_BASE, GPIO_BULK_SET, mask.to_bytes(4, "big"))

    def digital_read_bulk(self) -> int:
        """Every GPIO pin's state in one transaction: the whole point of the
        bulk read is that four keys cost one bus round trip, not four."""
        return decode_bulk(self.read(GPIO_BASE, GPIO_BULK, 4))

    # -- NeoPixel ----------------------------------------------------------

    def neopixel_init(self, pin: int, count: int, bytes_per_pixel: int = 3) -> None:
        self.write(NEOPIXEL_BASE, NEOPIXEL_PIN, bytes([int(pin)]))
        # 1 = 800kHz, what every current NeoPixel wants.
        self.write(NEOPIXEL_BASE, NEOPIXEL_SPEED, bytes([1]))
        length = int(count) * int(bytes_per_pixel)
        self.write(NEOPIXEL_BASE, NEOPIXEL_BUF_LENGTH, length.to_bytes(2, "big"))

    def neopixel_write(self, colors, order: str = "GRB") -> None:
        """Set every pixel and latch them. One buffer write plus one show, so
        the four keys change together instead of rippling."""
        buf = encode_pixel_buffer(colors, order)
        self.write(NEOPIXEL_BASE, NEOPIXEL_BUF, (0).to_bytes(2, "big") + buf)
        self.write(NEOPIXEL_BASE, NEOPIXEL_SHOW)
