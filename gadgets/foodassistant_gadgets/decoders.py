"""Pure BLE payload decoders for supported kitchen thermometers.

Every function here is pure (bytes in, plain data out) so the whole module is
unit-testable without a radio, bluez, or bleak installed. The daemon imports
these; the app's test suite imports them too via the same sys.path trick the
Stream Deck tests use.

Protocols, with the sources the byte layouts were taken from:

* Inkbird iBBQ family (IBT-2X / IBT-4XS / IBT-6XS and other "iBBQ" devices):
  community reverse engineering of the iBBQ GATT protocol
  (https://gist.github.com/uucidl/b9c60b6d36d8080d085a8e3310621d64). Service
  fff0; write the pairing credentials to fff2, commands to fff5, and read
  temperatures from notifications on fff4 as little-endian tenths of a degree
  Celsius per probe.
* ThermoPro TP25-style BBQ thermometers (TP25 / TP25W and similar): community
  reverse engineering (https://github.com/martin-hughes/thermopro-tools and
  https://github.com/daniel-corbett/thermopro-cli). A TLVC frame protocol on a
  vendor service; temperatures are two-byte BCD in tenths of a degree.
* Combustion Inc Predictive Thermometer: Combustion publishes its BLE spec
  (https://github.com/combustion-inc/combustion-documentation,
  probe_ble_specification.rst). No connection is needed: the advertising
  packet's manufacturer-specific data carries all eight thermistor readings.
* ThermoWorks BlueDOT: community reverse engineering
  (https://github.com/jamesshannon/thermoworks-ha). A 20-byte notification
  payload with the temperature as a little-endian int32 in the device's
  display unit.
* ThermoPro TempSpike (TP96x: TP960 / TP960R / TP962R): the Bluetooth-Devices
  thermopro-ble parser
  (https://github.com/Bluetooth-Devices/thermopro-ble). No connection is
  needed: the tip and ambient temperatures ride the manufacturer-specific
  advertising data. The device stuffs the tip temperature's low byte into the
  2-byte company id itself, so the frame is decoded by restoring the company
  id in front of the value. Validated against a live capture from Dan's
  kitchen (a TP960R at room temperature).
* Govee grill thermometers (H5181 / H5182 / H5183 / H5184 / H5185): the
  Bluetooth-Devices govee-ble parser
  (https://github.com/Bluetooth-Devices/govee-ble). Also advertising-only:
  the manufacturer data carries each probe's current temperature and its
  alarm/target temperature as big-endian signed hundredths of a degree
  Celsius, with a negative value meaning an unplugged probe. Validated
  against a live capture of an H5182 with both probes unplugged.
"""
from __future__ import annotations

import struct
from math import tanh

# --------------------------------------------------------------------------
# Inkbird iBBQ (IBT-2X / IBT-4XS / IBT-6XS, and other iBBQ-protocol devices)
# --------------------------------------------------------------------------

IBBQ_SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
# Notifications: control-message responses (battery answers arrive here).
IBBQ_SETTINGS_RESULT_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
# Write: the pairing credentials message.
IBBQ_PAIR_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
# Notifications: real-time probe temperatures.
IBBQ_REALTIME_UUID = "0000fff4-0000-1000-8000-00805f9b34fb"
# Write: commands (enable realtime, request battery, units, targets).
IBBQ_SETTINGS_UUID = "0000fff5-0000-1000-8000-00805f9b34fb"

# Fixed credentials handshake, written to fff2 right after connecting.
IBBQ_CREDENTIALS = bytes([0x21, 0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01,
                          0xB8, 0x22, 0x00, 0x00, 0x00, 0x00, 0x00])
# Written to fff5: start streaming realtime temperatures on fff4.
IBBQ_ENABLE_REALTIME = bytes([0x0B, 0x01, 0x00, 0x00, 0x00, 0x00])
# Written to fff5: ask for a battery report (answered on fff1, header 0x24).
IBBQ_REQUEST_BATTERY = bytes([0x08, 0x24, 0x00, 0x00, 0x00, 0x00])
# Written to fff5: keep the device itself reporting in Celsius (we convert
# for display app-side, so the wire format stays fixed).
IBBQ_UNITS_CELSIUS = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x00])

# Raw uint16 values at or above this read as "no probe plugged in". Real
# devices report 0xFFF6 or 0xFFFF for an empty socket; anything this large
# would be a nonsense temperature (>= 6428 C) anyway.
_IBBQ_DISCONNECTED = 0xFB00


def decode_ibbq_realtime(payload: bytes) -> list[float | None]:
    """Decode an fff4 realtime notification into per-probe Celsius readings.

    One little-endian uint16 per probe socket, tenths of a degree Celsius.
    A disconnected probe reads as a sentinel (0xFFF6 / 0xFFFF) and becomes
    None, keeping its position so probe numbering stays stable.
    """
    probes: list[float | None] = []
    for i in range(0, len(payload) - (len(payload) % 2), 2):
        raw = payload[i] | (payload[i + 1] << 8)
        if raw >= _IBBQ_DISCONNECTED:
            probes.append(None)
        else:
            probes.append(raw / 10.0)
    return probes


def decode_ibbq_battery(payload: bytes) -> int | None:
    """Decode an fff1 battery report (header 0x24) into a 0-100 percentage.

    Layout: 0x24, current voltage (uint16 LE), max voltage (uint16 LE, where
    0 means the 6550 mV factory default). Returns None for anything else.
    """
    if len(payload) < 5 or payload[0] != 0x24:
        return None
    current = payload[1] | (payload[2] << 8)
    maximum = payload[3] | (payload[4] << 8)
    if maximum == 0:
        maximum = 6550
    pct = round(100.0 * current / maximum)
    return max(0, min(100, pct))


# --------------------------------------------------------------------------
# ThermoPro TP25-style (TP25 / TP25W / TP920-family BBQ thermometers)
# --------------------------------------------------------------------------

TP25_SERVICE_UUID = "1086fff0-3343-4817-8bb2-b32206336ce8"
TP25_WRITE_UUID = "1086fff1-3343-4817-8bb2-b32206336ce8"
TP25_NOTIFY_UUID = "1086fff2-3343-4817-8bb2-b32206336ce8"


def tp25_checksum(data: bytes) -> int:
    """Mod-256 sum of every byte, the checksum every TP25 frame ends with."""
    return sum(data) & 0xFF


def tp25_command(command_type: int, value: bytes = b"") -> bytes:
    """Build a TLVC command frame: type, length, value, checksum."""
    body = bytes([command_type & 0xFF, len(value) & 0xFF]) + value
    return body + bytes([tp25_checksum(body)])


# The 0x01 setup command must be sent first or the thermometer stays silent.
# The 9 payload bytes are a captured known-good handshake from the vendor app
# (their exact meaning is not understood; see thermopro-cli).
TP25_HANDSHAKE = tp25_command(0x01, bytes.fromhex("8a7a13b73ed68b67c2"))
# Ask for a temperature report now (the device also sends them unprompted).
TP25_REQUEST_TEMPS = tp25_command(0x30)


def decode_tp25_bcd(b1: int, b2: int) -> float | None:
    """Decode a two-byte BCD temperature in tenths of a degree.

    0xFFFF means no probe; 0xDDDD and 0xEEEE are under/over-range markers.
    All three come back as None (nothing displayable). The high bit of the
    first byte is the sign.
    """
    if (b1, b2) in ((0xFF, 0xFF), (0xDD, 0xDD), (0xEE, 0xEE)):
        return None
    hundreds = ((b1 & 0x70) >> 4) * 100
    tens = (b1 & 0x0F) * 10
    ones = (b2 & 0xF0) >> 4
    tenths = (b2 & 0x0F) * 0.1
    temp = hundreds + tens + ones + tenths
    return -temp if (b1 & 0x80) else temp


def decode_tp25_frame(data: bytes) -> dict | None:
    """Decode a 0x30 temperature-report notification frame.

    Frame: 0x30, length, value bytes, checksum (then junk padding). The value
    is [battery, unit mode, alarm status, then two BCD bytes per probe slot].
    Unit mode 0x0C means the device displays Celsius, 0x0F Fahrenheit; the
    BCD digits are in that display unit, so Fahrenheit readings are converted
    here and the result is always Celsius. Returns None for anything that is
    not a checksum-valid 0x30 frame.
    """
    if len(data) < 3 or data[0] != 0x30:
        return None
    length = data[1]
    if len(data) < 2 + length + 1:
        return None
    if tp25_checksum(data[:2 + length]) != data[2 + length]:
        return None
    value = data[2:2 + length]
    if len(value) < 3:
        return None
    battery = int(value[0]) if 0 <= value[0] <= 100 else None
    fahrenheit = value[1] == 0x0F
    alarm = bool(value[2] & 0x08)
    probes: list[float | None] = []
    for i in range(3, len(value) - 1, 2):
        temp = decode_tp25_bcd(value[i], value[i + 1])
        if temp is not None and fahrenheit:
            temp = round((temp - 32.0) * 5.0 / 9.0, 1)
        probes.append(temp)
    return {
        "battery": battery,
        "unit": "F" if fahrenheit else "C",
        "alarm": alarm,
        "probes": probes,
    }


# --------------------------------------------------------------------------
# Combustion Inc Predictive Thermometer (advertising data; no connection)
# --------------------------------------------------------------------------

# Bluetooth SIG company identifier for Combustion Inc. bleak presents
# manufacturer data as {company_id: payload} with the id already stripped.
COMBUSTION_MANUFACTURER_ID = 0x09C7


def decode_combustion_advertising(payload: bytes) -> dict | None:
    """Decode a Combustion probe's manufacturer-specific advertising payload.

    The payload (after the 2-byte vendor id bleak strips) is: product type
    (1), serial number (4, LE), raw temperature data (13), mode/id (1),
    battery status + virtual sensors (1), then fields we ignore. The 13 bytes
    pack eight 13-bit thermistor readings, LSB first;
    Celsius = raw * 0.05 - 20.

    In Instant Read mode only the first slot is meaningful, so the rest come
    back as None. Product type 1 is the probe; other product types (displays,
    boosters, MeatNet nodes) are not probes and return None.
    """
    if len(payload) < 20:
        return None
    product_type = payload[0]
    if product_type != 1:
        return None
    serial = int.from_bytes(payload[1:5], "little")
    packed = int.from_bytes(payload[5:18], "little")
    temps: list[float | None] = []
    for i in range(8):
        raw = (packed >> (13 * i)) & 0x1FFF
        temps.append(round(raw * 0.05 - 20.0, 2))
    mode_id = payload[18]
    mode = mode_id & 0x03
    color_id = (mode_id >> 2) & 0x07
    probe_id = ((mode_id >> 5) & 0x07) + 1
    battery_low = bool(payload[19] & 0x01)
    instant_read = mode == 1
    if instant_read:
        temps = [temps[0]] + [None] * 7
    return {
        "serial": f"{serial:08X}",
        "mode": mode,
        "instant_read": instant_read,
        "color_id": color_id,
        "probe_id": probe_id,
        "battery_low": battery_low,
        "temps_c": temps,
    }


# --------------------------------------------------------------------------
# ThermoWorks BlueDOT
# --------------------------------------------------------------------------

BLUEDOT_NOTIFY_UUID = "783f2991-23e0-4bdc-ac16-78601bd84b39"
_BLUEDOT_FRAME_LEN = 20


def decode_bluedot(data: bytes) -> dict | None:
    """Decode a 20-byte BlueDOT notification payload.

    Layout: probe status (0 connected, 3 disconnected), temperature (int32
    LE, whole degrees in the device's display unit), alarm temperature (int32
    LE), alarm silenced, alarm disabled, unit (0 C / 1 F), one unknown byte,
    MAC (6), alarm active. Temperatures are returned in Celsius regardless of
    the device's display unit. Returns None for a wrong-length payload.
    """
    if len(data) != _BLUEDOT_FRAME_LEN:
        return None
    connected = data[0] == 0x00
    raw_temp = int.from_bytes(data[1:5], "little", signed=True)
    raw_alarm = int.from_bytes(data[5:9], "little", signed=True)
    fahrenheit = data[11] == 0x01

    def _c(value: int) -> float:
        if fahrenheit:
            return round((value - 32.0) * 5.0 / 9.0, 1)
        return float(value)

    return {
        "connected": connected,
        "temp_c": _c(raw_temp) if connected else None,
        "alarm_temp_c": _c(raw_alarm),
        "alarm_silenced": data[9] != 0,
        "alarm_disabled": data[10] != 0,
        "alarm_active": data[19] != 0,
        "unit": "F" if fahrenheit else "C",
    }


# --------------------------------------------------------------------------
# ThermoPro TempSpike (TP96x, advertising data; no connection)
# --------------------------------------------------------------------------

# The tip temperature is offset by this many degrees in the raw frame, so a
# raw 52 reads as 22 C. thermopro-ble applies the same fixed offset.
_TEMPSPIKE_TEMP_OFFSET = 30
# The TempSpike frame (after restoring the 2-byte company id) is 7 bytes; a
# newer firmware appends a reversed MAC for 13 bytes total, which we ignore.
_TEMPSPIKE_FRAME_LENGTHS = (7, 13)
# A restored frame outside this Celsius band is treated as a mis-detected
# advert rather than a real reading (the probe range is well inside this).
_TEMPSPIKE_MIN_C = -40
_TEMPSPIKE_MAX_C = 350


def tempspike_battery(voltage: int) -> int:
    """Battery percentage from the raw voltage field.

    The tanh fit is thermopro-ble's, machine-fit against the vendor app's
    percentage; clamped to 0-100 and rounded to a whole percent.
    """
    raw = 52.317286 * tanh(voltage / 273.624277936 - 8.76485439394) + 51.06925
    return int(round(max(0.0, min(100.0, raw))))


def decode_tempspike(frame: bytes) -> dict | None:
    """Decode a TempSpike advertising frame into tip and ambient Celsius.

    ``frame`` is the manufacturer payload with its 2-byte company id restored
    in front (little-endian id + the value bleak hands back). That matters:
    the device packs the tip temperature's low byte into the company id, so
    the temperature cannot be read from the value alone. Layout, little-endian:
    probe index (1), tip temperature (uint16), battery voltage (uint16),
    ambient temperature (uint16); temperatures are the raw value minus a fixed
    30-degree offset. Returns None for a wrong-length or out-of-range frame.
    """
    if len(frame) not in _TEMPSPIKE_FRAME_LENGTHS:
        return None
    probe_index, tip_raw, battery_mv, ambient_raw = struct.unpack_from("<BHHH", frame, 0)
    tip_c = tip_raw - _TEMPSPIKE_TEMP_OFFSET
    ambient_c = ambient_raw - _TEMPSPIKE_TEMP_OFFSET
    if not (_TEMPSPIKE_MIN_C <= tip_c <= _TEMPSPIKE_MAX_C):
        return None
    if not (_TEMPSPIKE_MIN_C <= ambient_c <= _TEMPSPIKE_MAX_C):
        return None
    return {
        "probe_index": probe_index,
        "tip_c": float(tip_c),
        "ambient_c": float(ambient_c),
        "battery": tempspike_battery(battery_mv),
    }


def decode_tempspike_from_manufacturer(manufacturer_data: dict | None) -> dict | None:
    """Decode a TempSpike from a bleak manufacturer_data dict.

    The device rolls the tip temperature's low byte through the company id, so
    a single advertisement can carry more than one keyed frame; the newest one
    (the last key, as thermopro-ble does) is the current reading. Restores the
    company id in front of the value before decoding.
    """
    if not manufacturer_data:
        return None
    try:
        company_id = list(manufacturer_data)[-1]
        value = bytes(manufacturer_data.get(company_id) or b"")
        frame = int(company_id).to_bytes(2, "little") + value
    except (ValueError, TypeError):
        return None
    return decode_tempspike(frame)


# --------------------------------------------------------------------------
# Govee grill thermometers (H5181 / H5182 / H5183 / H5184 / H5185; advertising)
# --------------------------------------------------------------------------

# The govee-ble frame lengths, one per model family. The temperature block
# always starts at byte 8; the length tells single from dual probe.
_GOVEE_GRILL_LENGTHS = (14, 17, 20)


def _govee_temp(raw: int) -> float | None:
    """One govee grill temperature: signed hundredths of a degree Celsius, with
    a negative value (0xFFFF reads as -1) meaning an unplugged probe."""
    return round(raw / 100.0, 2) if raw >= 0 else None


def decode_govee_grill(value: bytes) -> dict | None:
    """Decode a Govee grill advertisement into per-probe temps and targets.

    ``value`` is the manufacturer payload bleak hands back (company id already
    stripped). The temperature block starts at byte 8; each probe is a
    big-endian signed uint16 in hundredths of a degree Celsius followed by its
    alarm/target temperature. A single-probe model (14 bytes) has one pair; a
    dual model (17 or 20 bytes) has two, split by a padding byte. Returns
    ``{"probes": [...], "targets": [...]}`` in Celsius (None for an unplugged
    probe or an unset target), or None for an unrecognized length.
    """
    n = len(value)
    if n == 14:
        pairs = [struct.unpack_from(">hh", value, 8)]
    elif n == 17:
        p1, a1, _pad, p2, a2 = struct.unpack_from(">hhbhh", value, 8)
        pairs = [(p1, a1), (p2, a2)]
    elif n == 20:
        p1, a1, _mid, p2, a2 = struct.unpack_from(">hhhhh", value, 8)
        pairs = [(p1, a1), (p2, a2)]
    else:
        return None
    probes: list[float | None] = []
    targets: list[float | None] = []
    for temp_raw, alarm_raw in pairs:
        probes.append(_govee_temp(temp_raw))
        # A target of 0 or below means no alarm is set for that probe.
        targets.append(round(alarm_raw / 100.0, 2) if alarm_raw > 0 else None)
    return {"probes": probes, "targets": targets}


# --------------------------------------------------------------------------
# Auto-detection
# --------------------------------------------------------------------------

# Protocol names the daemon and the app agree on.
PROTOCOL_INKBIRD = "inkbird"
PROTOCOL_THERMOPRO = "thermopro"
PROTOCOL_COMBUSTION = "combustion"
PROTOCOL_BLUEDOT = "bluedot"
PROTOCOL_TEMPSPIKE = "tempspike"
PROTOCOL_GOVEE_GRILL = "govee_grill"

# Bluetooth SIG company id Govee's ambient hygrometers (GVH50xx and kin)
# advertise under. It is not one a cooking probe uses, so it is a reliable
# marker for "room sensor, filter it out".
GOVEE_HYGROMETER_MANUFACTURER_ID = 0xEC88

_INKBIRD_NAME_PREFIXES = ("ibbq", "ibt-", "inkbird", "tibt")
# TP25/TP27/TP920 are the connect-and-notify BBQ thermometers; the TP96x
# TempSpike is a different, advertising-only protocol, matched separately.
_THERMOPRO_NAME_PREFIXES = ("tp25", "tp-25", "tp27", "tp-27", "tp920")
_TEMPSPIKE_NAME_PREFIXES = ("tp96", "tp97", "tempspike")
_GOVEE_GRILL_NAME_PREFIXES = ("gvh5181", "gvh5182", "gvh5183", "gvh5184",
                              "gvh5185", "govee_h518")

# Room hygrometers to keep out of the thermometer list. All Govee GVH50xx are
# ambient sensors; a few other Govee model prefixes are too. Grills are
# GVH518x, so a plain "gvh50" match never touches them.
_ROOM_SENSOR_NAME_PREFIXES = (
    "gvh50", "govee_h50",
    "gvh5100", "gvh5101", "gvh5102", "gvh5104", "gvh5105", "gvh5106",
    "gvh5177", "gvh5179",
)

# Names that read like a cooking probe but that we have no decoder for yet.
# These surface as "seen nearby, not supported yet" instead of vanishing, so a
# user knows the reader saw the device. iDevices' Kitchen Thermometer
# advertises the bare name "KT".
_UNSUPPORTED_PROBE_PREFIXES = ("tp", "igrill", "meater", "tempspike",
                               "ibbq", "ibt", "bluedot", "inkbird", "combustion")
_UNSUPPORTED_PROBE_HINTS = ("kitchen thermometer", "meat thermometer",
                            "bbq", "igrill", "meater")


def is_room_sensor(name: str | None,
                   manufacturer_data: dict | None = None) -> bool:
    """True for an ambient room hygrometer/thermometer that discovery filters
    out (Govee GVH50xx and kin, or anything on the hygrometer company id).

    A cooking-probe grill (Govee GVH518x, manufacturer id and payload of its
    own) is not a room sensor and returns False, so it still reaches the
    discovery list. Pure: name and manufacturer data in, bool out.
    """
    if manufacturer_data and GOVEE_HYGROMETER_MANUFACTURER_ID in manufacturer_data:
        return True
    low = (name or "").strip().lower()
    return bool(low and low.startswith(_ROOM_SENSOR_NAME_PREFIXES))


def looks_like_probe(name: str | None) -> bool:
    """True for a device whose name reads like a kitchen probe, used only for
    devices ``identify`` did not match, so an unsupported thermometer surfaces
    as "seen nearby, not supported yet" rather than disappearing."""
    low = (name or "").strip().lower()
    if not low:
        return False
    if low == "kt" or low.startswith("kt-") or low.startswith("kt "):
        return True
    if low.startswith(_UNSUPPORTED_PROBE_PREFIXES):
        return True
    return any(hint in low for hint in _UNSUPPORTED_PROBE_HINTS)


def _govee_grill_value(manufacturer_data: dict | None) -> bytes | None:
    """The manufacturer frame that has the Govee grill shape (a known length,
    the 0x01 marker byte, and a model byte in the H518x range), or None.

    Picks the grill payload out of an advertisement that may also carry an
    unrelated key (the captured H5182 also advertises an Apple iBeacon), and
    lets an H5182 be recognized even when it broadcasts no local name."""
    for value in (manufacturer_data or {}).values():
        v = bytes(value or b"")
        if (len(v) in _GOVEE_GRILL_LENGTHS and v[1] == 0x01
                and 0x81 <= v[0] <= 0x85):
            return v
    return None


def _govee_grill_signature(manufacturer_data: dict | None) -> bool:
    """True when an advertisement carries a Govee grill manufacturer frame."""
    return _govee_grill_value(manufacturer_data) is not None


def identify(name: str | None, manufacturer_data: dict | None = None,
             service_uuids: list | None = None) -> str | None:
    """Classify an advertisement as one of the supported thermometer protocols.

    Matches Combustion by its registered manufacturer id and Govee grills by
    their manufacturer-data shape (they may advertise no name); everything
    else goes by the advertised local name. Returns a protocol name or None
    for anything unrecognized. Ambient room hygrometers are never a match.
    """
    if is_room_sensor(name, manufacturer_data):
        return None
    if manufacturer_data and COMBUSTION_MANUFACTURER_ID in manufacturer_data:
        return PROTOCOL_COMBUSTION
    low = (name or "").strip().lower()
    if low:
        if low.startswith(_INKBIRD_NAME_PREFIXES):
            return PROTOCOL_INKBIRD
        if low.startswith(_TEMPSPIKE_NAME_PREFIXES):
            return PROTOCOL_TEMPSPIKE
        if low.startswith(_THERMOPRO_NAME_PREFIXES):
            return PROTOCOL_THERMOPRO
        if low.startswith(_GOVEE_GRILL_NAME_PREFIXES):
            return PROTOCOL_GOVEE_GRILL
        if low.startswith("bluedot"):
            return PROTOCOL_BLUEDOT
    if _govee_grill_signature(manufacturer_data):
        return PROTOCOL_GOVEE_GRILL
    uuids = [str(u).lower() for u in (service_uuids or [])]
    if IBBQ_SERVICE_UUID in uuids:
        return PROTOCOL_INKBIRD
    if TP25_SERVICE_UUID in uuids:
        return PROTOCOL_THERMOPRO
    return None
