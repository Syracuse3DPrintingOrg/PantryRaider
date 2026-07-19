"""UART barcode scanner frame builders and parsers (FoodAssistant-x61t).

All pure: no serial port, no hardware, no network. The byte vectors here are the
actual frames on the wire, checked against the Waveshare module's documented
scan command and its fixed write-ack, so a refactor that drifts off the wire
format fails a test rather than a scanner on the counter.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "gadgets"))

from foodassistant_gadgets import scanner_uart as su  # noqa: E402


# -- The register-write frame and the documented scan command ------------------

def test_scan_command_matches_the_documented_bytes():
    # The vendor's own scan command. Because it is a register write (0x01 to
    # register 0x0002), matching it confirms the whole write-frame format.
    assert su.SCAN_COMMAND == bytes([0x7E, 0x00, 0x08, 0x01, 0x00, 0x02, 0x01,
                                     0xAB, 0xCD])


def test_build_write_frame_layout():
    frame = su.build_write_frame(0x0002, bytes([0x01]))
    assert frame == su.SCAN_COMMAND
    # header, write type, length, big-endian address, data, then the CRC-skip
    # sentinel.
    assert frame[:2] == bytes([0x7E, 0x00])
    assert frame[2] == su.TYPE_WRITE
    assert frame[3] == 1  # one data byte
    assert frame[4:6] == bytes([0x00, 0x02])  # address 0x0002, big-endian
    assert frame[6] == 0x01  # the data
    assert frame[-2:] == bytes([0xAB, 0xCD])  # CRC skip sentinel


def test_build_write_frame_multibyte_address_and_data():
    frame = su.build_write_frame(0x1234, bytes([0xAA, 0xBB]))
    assert frame == bytes([0x7E, 0x00, 0x08, 0x02, 0x12, 0x34, 0xAA, 0xBB,
                           0xAB, 0xCD])


def test_build_write_frame_can_carry_a_real_crc():
    data = bytes([0x01])
    body = bytes([su.TYPE_WRITE, 1, 0x00, 0x02]) + data
    crc = su.crc_ccitt(body)
    frame = su.build_write_frame(0x0002, data, crc=crc)
    assert frame[-2:] == bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def test_build_write_frame_rejects_bad_inputs():
    import pytest
    with pytest.raises(ValueError):
        su.build_write_frame(0x10000, bytes([0x01]))
    with pytest.raises(ValueError):
        su.build_write_frame(0x0000, b"")


def test_crc_ccitt_known_vector():
    # CRC-CCITT/XModem of b"123456789" is 0x31C3, the standard check value.
    assert su.crc_ccitt(b"123456789") == 0x31C3


# -- The command-mode / lights-off configuration frame -------------------------

def test_mode_config_defaults_are_command_mode_lights_off():
    # Command mode (bits 1-0 = 01), aiming and illumination off, so the value
    # is 0x01 and the frame writes it to register 0x0000.
    assert su.light_and_mode_value() == 0x01
    assert su.mode_config_command() == bytes([0x7E, 0x00, 0x08, 0x01, 0x00,
                                              0x00, 0x01, 0xAB, 0xCD])


def test_light_and_mode_bits():
    assert su.light_and_mode_value(illumination=True) == 0b0000_0101
    assert su.light_and_mode_value(aiming=True) == 0b0001_0001
    assert su.light_and_mode_value(beeper_silence=True) == 0b0100_0001
    assert su.light_and_mode_value(decode_led=True) == 0b1000_0001
    assert su.light_and_mode_value(illumination=True, aiming=True,
                                   beeper_silence=True,
                                   decode_led=True) == 0b1101_0101


# -- Stripping the fixed write-ack out of the read stream ----------------------

def test_strip_ack_removes_a_complete_ack():
    buf = su.ACK + b"0123456789"
    cleaned, leftover = su.strip_ack_frames(buf)
    assert cleaned == b"0123456789"
    assert leftover == b""


def test_strip_ack_removes_ack_between_data():
    buf = b"12345" + su.ACK + b"67890"
    cleaned, leftover = su.strip_ack_frames(buf)
    assert cleaned == b"1234567890"
    assert leftover == b""


def test_strip_ack_holds_a_partial_trailing_ack():
    # A tail that is a proper prefix of the ack is held for the next read, not
    # mistaken for data. The ack's CRC bytes 0x33 0x31 are printable ('3','1'),
    # which is exactly why it must be stripped by structure, not by filtering.
    partial = su.ACK[:4]
    cleaned, leftover = su.strip_ack_frames(b"999" + partial)
    assert cleaned == b"999"
    assert leftover == partial


def test_strip_ack_keeps_a_lone_0x02_that_is_not_an_ack():
    # 0x02 followed by bytes that do not continue the ack prefix is data.
    buf = bytes([0x02, 0x41, 0x42])
    cleaned, leftover = su.strip_ack_frames(buf)
    assert cleaned == buf
    assert leftover == b""


# -- Extracting barcodes from a control-stripped run ---------------------------

def test_extract_splits_on_control_bytes():
    codes, leftover = su.extract_barcodes(b"0123456789012\r\n5901234123457\r\n")
    assert codes == ["0123456789012", "5901234123457"]
    assert leftover == b""


def test_extract_holds_unterminated_tail_without_flush():
    codes, leftover = su.extract_barcodes(b"0123456789012\r012345")
    assert codes == ["0123456789012"]
    assert leftover == b"012345"


def test_extract_flush_emits_the_tail():
    codes, leftover = su.extract_barcodes(b"012345", flush=True)
    assert codes == ["012345"]
    assert leftover == b""


# -- End to end: an ack plus a decoded barcode ---------------------------------

def test_parse_stream_ack_then_barcode():
    # What a single command-mode scan looks like: the write-ack, then the
    # barcode as ASCII. Flushed because the read window ended.
    buf = su.ACK + b"5901234123457"
    codes, leftover = su.parse_stream(buf, flush=True)
    assert codes == ["5901234123457"]
    assert leftover == b""


def test_parse_stream_carries_leftover_across_reads():
    first, mid = su.parse_stream(su.ACK + b"59012", flush=False)
    assert first == []
    assert mid == b"59012"
    codes, leftover = su.parse_stream(mid + b"34123457\r\n", flush=False)
    assert codes == ["5901234123457"]
    assert leftover == b""


def test_parse_stream_ignores_a_bare_ack():
    codes, leftover = su.parse_stream(su.ACK, flush=True)
    assert codes == []
    assert leftover == b""


# -- SerialScanner degrades honestly (no hardware needed) ----------------------

def test_serial_scanner_open_fails_cleanly_on_a_missing_port():
    # Whether or not pyserial is installed, a nonexistent port must return False
    # (never raise), stay unavailable, and carry a human-readable reason. This
    # is the missing-device / no-pyserial degradation path.
    scanner = su.SerialScanner("/nonexistent/tty-does-not-exist", 9600)
    assert scanner.open() is False
    assert scanner.available is False
    assert scanner.detail
    # scan() raises the one exception the daemon catches, rather than a bare
    # OSError, and close() is safe to call on a never-opened handle.
    import pytest
    with pytest.raises(su.ScannerUnavailable):
        scanner.scan()
    scanner.close()
    assert scanner.health() == {"available": False, "detail": scanner.detail}
