"""UART barcode scanner support for the gadgets agent (FoodAssistant-x61t).

A Waveshare-style scan engine wired straight to the Pi's GPIO UART (TX/RX on
GPIO14/15, physical pins 8/10, power from a 3.3V pin, ground). No level
shifter: the module is 3.3V logic. The agent reads decoded barcodes off the
serial port and POSTs them to the app on ``/pending/scan``, the same endpoint
the kiosk's on-screen scanner uses, so a scan lands in the Review queue no
matter which surface caught it.

The module stays dark and idle. On startup the agent puts it in command mode
with the aiming dot and the illumination LED off, which means it does nothing
until the agent explicitly asks for a scan. While the app reports a scan
session is active it asks continuously; when the session ends it stops asking
and the module goes dark again. That is the whole point of command mode here:
no stray scans, no light in a dark kitchen, and no reads while nobody is
looking at a scan page.

Two layers live in this file:

* Pure frame builders and parsers (no serial, no hardware). These are the
  bytes on the wire and are covered by tests with real byte vectors.
* ``SerialScanner``, a thin pyserial wrapper that degrades honestly. pyserial
  is imported lazily inside ``open`` so this package imports (and the tests
  run) on a machine that has never seen a serial port; a missing device,
  permission denied, or a port that is not there logs once, marks the scanner
  unavailable, and lets the daemon retry with backoff. It never crashes the
  agent's other loops.

Frame format (Waveshare / GM serial protocol, confirmed from the module's own
scan command). A register write is::

    7E 00 08 LEN ADDR_H ADDR_L DATA... CRC_H CRC_L

``7E 00`` is the header, ``08`` is the write-register type, ``LEN`` is the
number of data bytes, then the two-byte big-endian register address, the data,
and a two-byte CRC-CCITT. The module accepts the sentinel ``AB CD`` in place of
a real CRC and skips verification, which is exactly what its documented scan
command does: ``7E 00 08 01 00 02 01 AB CD`` writes ``01`` to register
``0x0002`` (bit 0 = "scan now"). So the scan command IS a register write, which
is how the register-write frame format is confirmed rather than guessed. The
module replies to a scan command with a fixed 7-byte ack, ``02 00 00 01 00 33
31``, then emits the decoded barcode as raw ASCII.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("foodassistant.gadgets.scanner")

# -- Wire protocol constants ---------------------------------------------------

# Frame header for a register access: 0x7E then a fixed 0x00.
HEADER = bytes([0x7E, 0x00])
# Frame type for "write register(s)". The documented scan command uses it, so
# this is the confirmed write type, not an assumption.
TYPE_WRITE = 0x08
# The CRC sentinel the module accepts in place of a real CRC-CCITT. Using it
# tells the module to skip CRC verification; it is what the vendor's own
# example scan command sends.
CRC_SKIP = 0xABCD

# Register 0x0000 configures lighting and mode. Bit layout (per the Waveshare
# setting manual): bit 7 decode-success LED, bit 6 beeper silence, bits 5-4
# aiming, bits 3-2 illumination, bits 1-0 scan mode (01 = command mode).
REG_LIGHT_AND_MODE = 0x0000
# Register 0x0002 bit 0 triggers a single scan while in command mode.
REG_SCAN_TRIGGER = 0x0002

# Scan-mode field values (bits 1-0 of register 0x0000).
MODE_COMMAND = 0b01

# The fixed acknowledgement the module returns for a register write. Its two
# CRC bytes are constant because the payload is constant, so the whole ack is a
# constant 7-byte string we can strip out of the read stream.
ACK = bytes([0x02, 0x00, 0x00, 0x01, 0x00, 0x33, 0x31])

# An alternative scan command that suppresses the ack/response data. Kept as a
# documented fallback; the register-write scan command is the primary path.
SCAN_COMMAND_QUIET = bytes([0x16, 0x54, 0x0D])


def crc_ccitt(data: bytes) -> int:
    """CRC-CCITT (XModem: polynomial 0x1021, initial value 0x0000).

    The module verifies this when the CRC field is not the ``AB CD`` sentinel.
    Exposed so a caller that wants a checked frame can build one, and so the
    value is testable, but the frame builders default to the sentinel to match
    the vendor's documented commands.
    """
    crc = 0x0000
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def build_write_frame(address: int, data: bytes, *, crc: int | None = None) -> bytes:
    """Build a register-write frame.

    ``crc`` defaults to the ``AB CD`` skip sentinel (what the vendor's commands
    send). Pass ``crc="compute"`` semantics by giving an int, or leave it None
    for the sentinel. Length is the number of data bytes and must fit one byte.
    """
    if not 0 <= address <= 0xFFFF:
        raise ValueError(f"address out of range: {address}")
    data = bytes(data)
    if not 1 <= len(data) <= 0xFF:
        raise ValueError(f"data length out of range: {len(data)}")
    crc_value = CRC_SKIP if crc is None else int(crc)
    body = bytes([TYPE_WRITE, len(data),
                  (address >> 8) & 0xFF, address & 0xFF]) + data
    return HEADER + body + bytes([(crc_value >> 8) & 0xFF, crc_value & 0xFF])


def light_and_mode_value(*, mode: int = MODE_COMMAND, illumination: bool = False,
                         aiming: bool = False, beeper_silence: bool = False,
                         decode_led: bool = False) -> int:
    """The byte written to register 0x0000.

    Defaults put the module in command mode with the aiming dot and the
    illumination LED off, which is the dark-and-idle state the agent wants: bits
    1-0 = 01 (command mode), bits 3-2 = 00 (no illumination), bits 5-4 = 00 (no
    aiming). The beeper is left on by default so a successful scan still gives
    the familiar audible confirmation; pass ``beeper_silence=True`` for a silent
    build.
    """
    value = mode & 0b11
    if illumination:
        value |= 0b01 << 2
    if aiming:
        value |= 0b01 << 4
    if beeper_silence:
        value |= 1 << 6
    if decode_led:
        value |= 1 << 7
    return value & 0xFF


def mode_config_command(**kwargs) -> bytes:
    """The frame that puts the module in command mode with lights off.

    ``7E 00 08 01 00 00 01 AB CD`` with the defaults.
    """
    return build_write_frame(REG_LIGHT_AND_MODE,
                             bytes([light_and_mode_value(**kwargs)]))


# The scan command: write 0x01 to register 0x0002 (bit 0 = scan now). Equal to
# the vendor's documented 7E 00 08 01 00 02 01 AB CD, which is asserted in the
# tests so a refactor cannot drift off the wire format.
SCAN_COMMAND = build_write_frame(REG_SCAN_TRIGGER, bytes([0x01]))


def strip_ack_frames(buf: bytes) -> tuple[bytes, bytes]:
    """Remove the module's fixed write-ack frames from a read buffer.

    Returns ``(cleaned, leftover)``. ``cleaned`` is the buffer with every
    complete 7-byte ack removed. ``leftover`` is a trailing partial ack (a
    proper prefix of the ack byte string at the very end of the buffer) held
    back so the next read can complete it; it is never mistaken for barcode
    data. The ack's own bytes include the printable ``3`` and ``1`` of its CRC,
    so stripping it explicitly (rather than filtering non-printable bytes) is
    what keeps those two characters out of a decoded code.
    """
    out = bytearray()
    i = 0
    n = len(buf)
    while i < n:
        if buf[i] == ACK[0]:
            window = buf[i:i + len(ACK)]
            if window == ACK:
                i += len(ACK)
                continue
            if len(window) < len(ACK) and ACK.startswith(window):
                # A partial ack at the tail: hold it for the next read.
                return bytes(out), bytes(buf[i:])
        out.append(buf[i])
        i += 1
    return bytes(out), b""


def extract_barcodes(buf: bytes, *, flush: bool = False) -> tuple[list[str], bytes]:
    """Pull decoded barcodes out of a control-stripped byte run.

    Runs of printable ASCII (0x20-0x7E) separated by any control byte (CR, LF,
    NUL, ...) are complete codes. A trailing printable run with no terminator is
    ambiguous under the module's "no end mark" setting, so it is returned as
    leftover unless ``flush`` is set (used when a read-timeout gap means the
    current scan is finished). Returns ``(codes, leftover)``.
    """
    codes: list[str] = []
    current = bytearray()
    for byte in buf:
        if 0x20 <= byte <= 0x7E:
            current.append(byte)
        elif current:
            codes.append(current.decode("ascii"))
            current = bytearray()
    if current:
        if flush:
            codes.append(current.decode("ascii"))
        else:
            return codes, bytes(current)
    return codes, b""


def parse_stream(buf: bytes, *, flush: bool = False) -> tuple[list[str], bytes]:
    """Strip ack frames, then extract barcodes. Returns ``(codes, leftover)``.

    The leftover is any partial ack plus any unterminated trailing code, in wire
    order, to be prepended to the next read.
    """
    cleaned, ack_leftover = strip_ack_frames(buf)
    codes, printable_leftover = extract_barcodes(cleaned, flush=flush)
    return codes, printable_leftover + ack_leftover


class ScannerUnavailable(Exception):
    """The serial port cannot be used (missing device, no permission, no
    pyserial). Carries the human-readable reason for the logs and Settings."""


class SerialScanner:
    """A pyserial handle for the UART scanner, with honest degradation.

    pyserial is imported lazily in ``open`` so importing this package never
    requires it. Every read/write failure marks the scanner unavailable with a
    reason string and raises ``ScannerUnavailable`` (or returns False from
    ``open``), so the daemon has exactly one thing to catch and can retry with
    backoff. Nothing here knows about HTTP.
    """

    def __init__(self, port: str = "/dev/serial0", baud: int = 9600,
                 *, read_timeout: float = 0.2):
        self.port = str(port or "/dev/serial0")
        self.baud = int(baud or 9600)
        self.read_timeout = float(read_timeout)
        self._ser = None
        self._serial = None
        self.available = False
        self.detail = ""
        self._logged = False
        self._configured = False
        self._leftover = b""

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> bool:
        """Open the port if it is not open. False (never a raise) on failure."""
        if self._ser is not None:
            return True
        try:
            import serial  # noqa: PLC0415 - lazy on purpose
        except ImportError as exc:
            self._unavailable(
                "The pyserial package is not installed on this device, so the "
                "UART barcode scanner cannot be read. Re-run "
                "foodassistant-gadgets-setup to install it.", exc)
            return False
        self._serial = serial
        try:
            self._ser = serial.Serial(self.port, self.baud,
                                      timeout=self.read_timeout)
        except PermissionError as exc:
            self._unavailable(
                f"No permission to open {self.port}. Re-run "
                "foodassistant-gadgets-setup to add this device's service user "
                "to the dialout group, then reboot.", exc)
            return False
        except FileNotFoundError as exc:
            self._unavailable(
                f"{self.port} is not there, so the Pi's UART is not turned on. "
                "Re-run foodassistant-gadgets-setup to free the serial port, "
                "then reboot.", exc)
            return False
        except (OSError, getattr(serial, "SerialException", OSError)) as exc:
            self._unavailable(
                f"Could not open {self.port} ({exc}). Check the wiring and that "
                "the serial console is disabled.", exc)
            return False
        self.available = True
        self.detail = ""
        self._logged = False
        self._configured = False
        self._leftover = b""
        log.info("UART scanner open on %s at %s baud", self.port, self.baud)
        return True

    def close(self) -> None:
        self._configured = False
        self._leftover = b""
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001 - closing must not raise
                pass
            self._ser = None
        self.available = False

    def _unavailable(self, detail: str, exc: Exception | None = None) -> None:
        self.available = False
        self.detail = detail
        if not self._logged:
            log.warning("UART scanner unavailable: %s (%s)", detail, exc)
            self._logged = True

    def _fail(self, exc: Exception) -> ScannerUnavailable:
        """Turn a live-port error into our exception, dropping the handle so the
        next open reconnects (a cable pulled mid-read recovers on replug)."""
        self.close()
        self._unavailable(
            f"The serial port stopped responding ({exc}). Check the scanner's "
            "wiring; it recovers on its own once it is reconnected.", exc)
        return ScannerUnavailable(self.detail)

    # -- transactions ------------------------------------------------------

    def configure(self) -> bool:
        """Put the module in command mode with the lights off (dark and idle).

        Idempotent and cheap; the daemon calls it once after each open. Returns
        False without raising if the port is not available.
        """
        if not self.open():
            return False
        try:
            self._ser.reset_input_buffer()
            self._ser.write(mode_config_command())
            self._ser.flush()
            # Swallow the write ack so it does not lead the first scan's read.
            self._ser.read(len(ACK))
        except OSError as exc:
            self._fail(exc)
            return False
        self._configured = True
        return True

    def scan(self) -> list[str]:
        """Trigger one scan and return any decoded barcodes.

        Sends the scan command, reads whatever the module emits within the read
        window, and parses barcodes out of it. Raises ``ScannerUnavailable`` if
        the port dies; the daemon catches that and retries with backoff.
        """
        if not self.open():
            raise ScannerUnavailable(self.detail)
        try:
            self._ser.write(SCAN_COMMAND)
            self._ser.flush()
            raw = self._ser.read(64)
            while getattr(self._ser, "in_waiting", 0):
                raw += self._ser.read(self._ser.in_waiting)
        except OSError as exc:
            raise self._fail(exc) from exc
        # A completed scan window delimits a code even without an end mark, so
        # flush the trailing run rather than holding it for a read that may
        # never come.
        codes, self._leftover = parse_stream(self._leftover + raw, flush=True)
        return codes

    def health(self) -> dict:
        return {"available": bool(self.available), "detail": self.detail}
