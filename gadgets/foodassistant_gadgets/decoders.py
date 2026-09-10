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

Hygrometers (temperature + humidity ambient sensors, a separate device class
from the cooking probes above; all advertising-only, no connections):

* Govee H5075 / H5072 and kin: the Bluetooth-Devices govee-ble parser and
  Thrilleratplay/GoveeWatcher. Manufacturer id 0xEC88; a 24-bit big-endian
  number packs temperature and humidity together, with the top bit as the
  temperature's sign.
* Govee H5074: same sources. Manufacturer id 0xEC88 with a 7-byte payload:
  little-endian int16 temperature and uint16 humidity, both in hundredths.
* Xiaomi LYWSD03MMC running the community ATC firmware, both formats:
  atc1441 (https://github.com/atc1441/ATC_MiThermometer, 13-byte service
  data on UUID 0x181A, big-endian tenths) and pvvx custom
  (https://github.com/pvvx/ATC_MiThermometer, 15 bytes, little-endian
  hundredths). The stock Xiaomi firmware's MiBeacon advertisements are
  encrypted and are NOT supported; flash the ATC firmware.
* SwitchBot Meter / Meter Plus / outdoor meter: the pySwitchbot parser
  (https://github.com/sblibs/pySwitchbot). Service data (UUID 0xFD3D, or
  0x0D00 on older firmware): BCD-ish temperature with a sign bit, whole
  percent humidity, battery in the flags byte.
* Inkbird IBS-TH1 / IBS-TH2: the Bluetooth-Devices inkbird-ble parser
  (https://github.com/Bluetooth-Devices/inkbird-ble). Like the TempSpike,
  the sensor stuffs data into the manufacturer company id itself, so the
  9-byte frame is decoded by restoring the id in front of the value:
  little-endian int16 temperature and uint16 humidity in hundredths, with
  the battery percentage at byte 7.

Buttons (stick-anywhere BLE push buttons whose presses ride the
advertisement; a third device class, all passive, no connections):

* BTHome v2 button events: the published BTHome standard
  (https://bthome.io/format/), used by the Shelly BLU Button1 / BLU RC
  Button 4 and any DIY ESPHome/ATC device broadcasting BTHome. Service data
  on UUID 0xFCD2: a device-info byte, then measurement objects; object 0x3A
  is a button event (single/double/triple/long and kin), object 0x00 a
  packet id used for dedupe, object 0x01 the battery percent. Encrypted
  BTHome (device-info bit 0) needs a bindkey and is NOT supported; Shelly
  buttons ship with encryption off.
* Xiaomi MiBeacon button events, unencrypted frames only: the community
  MiBeacon documentation (https://github.com/custom-components/ble_monitor
  and https://github.com/Bluetooth-Devices/xiaomi-ble). Service data on UUID
  0xFE95: a frame-control word, product id, frame counter (dedupe), optional
  MAC/capability fields, then one object; object id 0x1001 is a button event
  (button number + press type). Frames with the encrypted bit set need a
  per-device bindkey and are NOT supported; many Xiaomi buttons encrypt
  after being bound to the Mi Home app, so this decoder is best-effort for
  devices still broadcasting plain MiBeacon.
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
# Hygrometers (temperature + humidity ambient sensors; advertising-only)
# --------------------------------------------------------------------------

# Protocol names the daemon and the app agree on for the hygrometer class.
PROTOCOL_GOVEE_HYGRO = "govee_hygro"
PROTOCOL_XIAOMI_ATC = "xiaomi_atc"
PROTOCOL_SWITCHBOT_METER = "switchbot_meter"
PROTOCOL_INKBIRD_HYGRO = "inkbird_hygro"

HYGRO_PROTOCOLS = (PROTOCOL_GOVEE_HYGRO, PROTOCOL_XIAOMI_ATC,
                   PROTOCOL_SWITCHBOT_METER, PROTOCOL_INKBIRD_HYGRO)

# The ATC community firmware (both atc1441 and pvvx) advertises its readings
# as service data on the Environmental Sensing UUID.
ATC_SERVICE_UUID = "0000181a-0000-1000-8000-00805f9b34fb"
# SwitchBot meters: 0xFD3D on current firmware, 0x0D00 on older ones.
SWITCHBOT_SERVICE_UUIDS = ("0000fd3d-0000-1000-8000-00805f9b34fb",
                           "00000d00-0000-1000-8000-00805f9b34fb")
# SwitchBot device-type bytes that are meters: 'T' Meter, 'i' Meter Plus,
# 'w' the outdoor meter. Everything else (bots, curtains) is not ours.
_SWITCHBOT_METER_TYPES = (0x54, 0x69, 0x77)

# Sanity band for an ambient sensor: a fridge, freezer, or room. A decoded
# value outside it is a mis-detected advertisement, not a reading.
_HYGRO_MIN_C = -60.0
_HYGRO_MAX_C = 100.0

_INKBIRD_HYGRO_NAME_PREFIXES = ("sps", "tps", "ibs-th", "ith-")
_ATC_NAME_PREFIXES = ("atc_", "atc-", "lywsd03")


def _hygro_result(temp_c, humidity_pct, battery_pct) -> dict | None:
    """Clamp and package one hygrometer reading, or None when the temperature
    is outside the ambient sanity band. Humidity outside 0-100 reads as
    unknown (None) rather than poisoning the reading; battery clamps."""
    if temp_c is None or not (_HYGRO_MIN_C <= temp_c <= _HYGRO_MAX_C):
        return None
    if humidity_pct is not None and not (0.0 <= humidity_pct <= 100.0):
        humidity_pct = None
    if battery_pct is not None:
        battery_pct = max(0, min(100, int(battery_pct)))
    return {"temp_c": round(float(temp_c), 2),
            "humidity_pct": (round(float(humidity_pct), 1)
                             if humidity_pct is not None else None),
            "battery_pct": battery_pct}


def decode_govee_hygrometer(value: bytes) -> dict | None:
    """Decode a Govee ambient hygrometer's 0xEC88 manufacturer payload.

    Two layouts (govee-ble / GoveeWatcher): the H5075 family is 6 bytes with
    a 24-bit big-endian number at bytes 1-3 packing temperature and humidity
    (temp = value/10000 C, humidity = (value % 1000)/10 %, top bit set means
    a negative temperature) and the battery percent at byte 4. The H5074 is
    7 bytes: pad, int16 LE temperature and uint16 LE humidity in hundredths,
    battery, pad. Returns {"temp_c", "humidity_pct", "battery_pct"} or None.
    """
    n = len(value)
    if n == 6:
        raw = int.from_bytes(value[1:4], "big")
        base = raw & 0x7FFFFF
        temp = base / 10000.0
        if raw & 0x800000:
            temp = -temp
        return _hygro_result(temp, (base % 1000) / 10.0, value[4])
    if n == 7:
        temp_raw, hum_raw, battery = struct.unpack_from("<hHB", value, 1)
        return _hygro_result(temp_raw / 100.0, hum_raw / 100.0, battery)
    return None


def decode_atc_advertisement(value: bytes) -> dict | None:
    """Decode an ATC-firmware LYWSD03MMC service-data frame (UUID 0x181A).

    Two community formats, told apart by length: atc1441 is 13 bytes (MAC,
    then BIG-endian int16 temperature in tenths, humidity %, battery %,
    battery mV, counter); pvvx custom is 15 bytes (reversed MAC, then
    LITTLE-endian int16 temperature and uint16 humidity in hundredths,
    battery mV, battery %, counter, flags). The endianness really does flip
    between the two. Returns the reading dict or None.
    """
    n = len(value)
    if n == 13:
        temp = int.from_bytes(value[6:8], "big", signed=True) / 10.0
        return _hygro_result(temp, float(value[8]), value[9])
    if n == 15:
        temp_raw, hum_raw = struct.unpack_from("<hH", value, 6)
        return _hygro_result(temp_raw / 100.0, hum_raw / 100.0, value[12])
    return None


def decode_switchbot_meter(value: bytes) -> dict | None:
    """Decode a SwitchBot Meter / Meter Plus service-data payload.

    Layout (pySwitchbot): device type (must be a meter), flags, battery in
    the low 7 bits of byte 2, then the temperature as a decimal digit
    (byte 3 low nibble) plus whole degrees (byte 4 low 7 bits) with byte 4's
    top bit SET meaning at-or-above zero and clear meaning negative, then
    humidity in the low 7 bits of byte 5. Returns the reading dict or None.
    """
    if len(value) < 6:
        return None
    if (value[0] & 0x7F) not in _SWITCHBOT_METER_TYPES:
        return None
    battery = value[2] & 0x7F
    temp = (value[4] & 0x7F) + (value[3] & 0x0F) / 10.0
    if not (value[4] & 0x80):
        temp = -temp
    return _hygro_result(temp, float(value[5] & 0x7F), battery)


def decode_inkbird_hygrometer(frame: bytes) -> dict | None:
    """Decode an Inkbird IBS-TH1/TH2 frame with its 2-byte company id
    restored in front (the sensor packs the temperature's low bytes into the
    company id itself, like the TempSpike). Layout, little-endian: int16
    temperature and uint16 humidity in hundredths, a probe-type byte, a
    16-bit CRC, then the battery percent at byte 7. A temperature-only model
    reports humidity 0, which comes back as None. Returns the dict or None.
    """
    if len(frame) != 9:
        return None
    temp_raw, hum_raw = struct.unpack_from("<hH", frame, 0)
    humidity = (hum_raw / 100.0) if hum_raw else None
    return _hygro_result(temp_raw / 100.0, humidity, frame[7])


def decode_inkbird_hygro_from_manufacturer(manufacturer_data: dict | None) -> dict | None:
    """Decode an Inkbird hygrometer from a bleak manufacturer_data dict,
    restoring the company id in front of the value (see above)."""
    if not manufacturer_data:
        return None
    try:
        company_id = list(manufacturer_data)[-1]
        value = bytes(manufacturer_data.get(company_id) or b"")
        frame = int(company_id).to_bytes(2, "little") + value
    except (ValueError, TypeError, OverflowError):
        return None
    return decode_inkbird_hygrometer(frame)


def _service_value(service_data: dict | None, uuids) -> bytes | None:
    """The first service-data value under any of the given UUIDs (matched
    case-insensitively), or None."""
    if not service_data:
        return None
    wanted = {str(u).lower() for u in uuids}
    for key, value in service_data.items():
        if str(key).lower() in wanted:
            return bytes(value or b"")
    return None


def identify_hygrometer(name: str | None, manufacturer_data: dict | None = None,
                        service_data: dict | None = None) -> str | None:
    """Classify an advertisement as one of the supported hygrometer protocols.

    Matches Govee by its 0xEC88 hygrometer company id (with a payload of the
    right shape) or its GVH50xx-style name, the ATC Xiaomi firmware and
    SwitchBot meters by their service data, and Inkbird IBS-TH sensors by
    their advertised name. Returns a protocol name or None. Pure.
    """
    if manufacturer_data and GOVEE_HYGROMETER_MANUFACTURER_ID in manufacturer_data:
        value = bytes(manufacturer_data.get(GOVEE_HYGROMETER_MANUFACTURER_ID) or b"")
        if len(value) in (6, 7):
            return PROTOCOL_GOVEE_HYGRO
    atc = _service_value(service_data, (ATC_SERVICE_UUID,))
    if atc is not None and len(atc) in (13, 15):
        return PROTOCOL_XIAOMI_ATC
    sb = _service_value(service_data, SWITCHBOT_SERVICE_UUIDS)
    if sb is not None and len(sb) >= 6 and (sb[0] & 0x7F) in _SWITCHBOT_METER_TYPES:
        return PROTOCOL_SWITCHBOT_METER
    low = (name or "").strip().lower()
    if low:
        if low.startswith(_ROOM_SENSOR_NAME_PREFIXES):
            return PROTOCOL_GOVEE_HYGRO
        if low.startswith(_ATC_NAME_PREFIXES):
            return PROTOCOL_XIAOMI_ATC
        if low.startswith(_INKBIRD_HYGRO_NAME_PREFIXES):
            return PROTOCOL_INKBIRD_HYGRO
    return None


def decode_hygrometer(protocol: str, manufacturer_data: dict | None = None,
                      service_data: dict | None = None) -> dict | None:
    """Decode one advertisement for an already-identified hygrometer protocol.

    Returns {"temp_c", "humidity_pct", "battery_pct"} or None when this
    particular advertisement carries no reading (many devices interleave
    frames). Pure: advertisement data in, reading out.
    """
    if protocol == PROTOCOL_GOVEE_HYGRO:
        value = (manufacturer_data or {}).get(GOVEE_HYGROMETER_MANUFACTURER_ID)
        return decode_govee_hygrometer(bytes(value or b"")) if value else None
    if protocol == PROTOCOL_XIAOMI_ATC:
        value = _service_value(service_data, (ATC_SERVICE_UUID,))
        return decode_atc_advertisement(value) if value else None
    if protocol == PROTOCOL_SWITCHBOT_METER:
        value = _service_value(service_data, SWITCHBOT_SERVICE_UUIDS)
        return decode_switchbot_meter(value) if value else None
    if protocol == PROTOCOL_INKBIRD_HYGRO:
        return decode_inkbird_hygro_from_manufacturer(manufacturer_data)
    return None


# --------------------------------------------------------------------------
# Buttons (BLE push buttons whose presses ride the advertisement)
# --------------------------------------------------------------------------

# Protocol names the daemon and the app agree on for the button class.
PROTOCOL_BTHOME_BUTTON = "bthome_button"
PROTOCOL_XIAOMI_BUTTON = "xiaomi_button"

BUTTON_PROTOCOLS = (PROTOCOL_BTHOME_BUTTON, PROTOCOL_XIAOMI_BUTTON)

# BTHome v2 broadcasts as service data on the BTHome UUID (0xFCD2,
# registered to Allterco Robotics, Shelly's parent).
BTHOME_SERVICE_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"
# Xiaomi MiBeacon service data UUID.
XIAOMI_SERVICE_UUID = "0000fe95-0000-1000-8000-00805f9b34fb"

# BTHome v2 object payload lengths (https://bthome.io/format/), needed to
# walk past the measurements we do not care about and reach the button
# events. Objects are sorted by id in the frame, so everything up to 0x3A
# must be skippable. 0x53 (text) and 0x54 (raw) are length-prefixed and
# handled separately.
_BTHOME_OBJECT_LENGTHS = {
    0x00: 1, 0x01: 1, 0x02: 2, 0x03: 2, 0x04: 3, 0x05: 3, 0x06: 2, 0x07: 2,
    0x08: 2, 0x09: 1, 0x0A: 3, 0x0B: 3, 0x0C: 2, 0x0D: 2, 0x0E: 2, 0x0F: 1,
    0x10: 1, 0x11: 1, 0x12: 2, 0x13: 2, 0x14: 2, 0x15: 1, 0x16: 1, 0x17: 1,
    0x18: 1, 0x19: 1, 0x1A: 1, 0x1B: 1, 0x1C: 1, 0x1D: 1, 0x1E: 1, 0x1F: 1,
    0x20: 1, 0x21: 1, 0x22: 1, 0x23: 1, 0x24: 1, 0x25: 1, 0x26: 1, 0x27: 1,
    0x28: 1, 0x29: 1, 0x2A: 1, 0x2B: 1, 0x2C: 1, 0x2D: 1, 0x2E: 1, 0x2F: 1,
    0x3A: 1, 0x3C: 2, 0x3D: 2, 0x3E: 4, 0x3F: 2, 0x40: 2, 0x41: 2, 0x42: 3,
    0x43: 2, 0x44: 2, 0x45: 2, 0x46: 1, 0x47: 2, 0x48: 2, 0x49: 2, 0x4A: 2,
    0x4B: 3, 0x4C: 4, 0x4D: 4, 0x4E: 4, 0x4F: 4, 0x50: 4, 0x51: 2, 0x52: 2,
}

# BTHome 0x3A button event values. "none" carries no press (a periodic
# advert from a button that was not pressed); triple and the long variants
# decode faithfully even though the app maps only single/double/long.
_BTHOME_BUTTON_EVENTS = {
    0x00: "none", 0x01: "single", 0x02: "double", 0x03: "triple",
    0x04: "long", 0x05: "long_double", 0x06: "long_triple", 0x80: "hold",
}

# MiBeacon 0x1001 press types.
_MIBEACON_PRESS_TYPES = {0x00: "single", 0x01: "double", 0x02: "long"}


def decode_bthome_button(value: bytes) -> dict | None:
    """Decode a BTHome v2 service-data frame that carries button events.

    ``value`` is the 0xFCD2 service data. Returns None for anything that is
    not plaintext BTHome v2 with at least one button object: encrypted
    frames (device-info bit 0; those need a bindkey), other BTHome versions,
    and frames with no 0x3A object (a BTHome sensor, not a button). The
    result is {"battery", "counter", "buttons", "events"} where events is
    one {"button": 1-based index, "event": name} per 0x3A object in frame
    order (a multi-button device sends one object per button, "none" for
    the buttons that were not pressed). Pure.
    """
    if len(value) < 2:
        return None
    info = value[0]
    if info & 0x01:
        return None  # encrypted BTHome needs a bindkey; unsupported
    if (info >> 5) & 0x07 != 2:
        return None  # not BTHome v2
    battery: int | None = None
    counter: int | None = None
    events: list[dict] = []
    i = 1
    while i < len(value):
        obj = value[i]
        i += 1
        if obj in (0x53, 0x54):  # text/raw: length-prefixed, skip
            if i >= len(value):
                break
            i += 1 + value[i]
            continue
        length = _BTHOME_OBJECT_LENGTHS.get(obj)
        if length is None or i + length > len(value):
            break  # unknown or truncated object; keep what parsed so far
        data = value[i:i + length]
        i += length
        if obj == 0x00:
            counter = data[0]
        elif obj == 0x01:
            battery = data[0]
        elif obj == 0x3A:
            events.append({"button": len(events) + 1,
                           "event": _BTHOME_BUTTON_EVENTS.get(data[0], "none")})
    if not events:
        return None
    return {"battery": battery, "counter": counter,
            "buttons": len(events), "events": events}


def decode_mibeacon_button(value: bytes) -> dict | None:
    """Decode an unencrypted Xiaomi MiBeacon frame carrying a button event.

    ``value`` is the 0xFE95 service data. Frame: frame control (uint16 LE;
    bit 3 encrypted, bit 4 MAC included, bit 5 capability included, bit 6
    object included), product id (uint16 LE), frame counter, then the
    optional MAC (6) and capability fields, then the object: id (uint16 LE),
    length, data. Object 0x1001 is a button event: button number (uint16 LE)
    and press type. Returns {"battery": None, "counter", "buttons", "events"}
    (matching the BTHome shape) or None for encrypted, object-less, or
    non-button frames. Pure.
    """
    if len(value) < 5:
        return None
    fc = int.from_bytes(value[0:2], "little")
    if fc & 0x0008:
        return None  # encrypted MiBeacon needs a bindkey; unsupported
    if not (fc & 0x0040):
        return None  # no object payload in this frame
    counter = value[4]
    i = 5
    if fc & 0x0010:
        i += 6  # MAC address
    if fc & 0x0020:
        if i >= len(value):
            return None
        capability = value[i]
        i += 1
        if capability & 0x20:
            i += 2  # IO capability extension
    if i + 3 > len(value):
        return None
    obj_id = int.from_bytes(value[i:i + 2], "little")
    length = value[i + 2]
    i += 3
    if obj_id != 0x1001 or length < 3 or i + length > len(value):
        return None
    button_no = int.from_bytes(value[i:i + 2], "little")
    event = _MIBEACON_PRESS_TYPES.get(value[i + 2])
    if event is None:
        return None
    # Button numbers are 0-based on the wire; oddball encodings (some
    # dimmer remotes reuse the field) collapse to button 1.
    button = button_no + 1 if button_no < 0x10 else 1
    return {"battery": None, "counter": counter, "buttons": 1,
            "events": [{"button": button, "event": event}]}


def identify_button(name: str | None, manufacturer_data: dict | None = None,
                    service_data: dict | None = None) -> str | None:
    """Classify an advertisement as one of the supported button protocols.

    A BTHome frame counts only when it carries a button object, so a BTHome
    temperature sensor never lands in the button list; a MiBeacon frame
    counts only when it carries a decodable button event (which is exactly
    when a button was pressed, which suits the press-to-add capture flow).
    Returns a protocol name or None. Pure.
    """
    value = _service_value(service_data, (BTHOME_SERVICE_UUID,))
    if value is not None and decode_bthome_button(value) is not None:
        return PROTOCOL_BTHOME_BUTTON
    value = _service_value(service_data, (XIAOMI_SERVICE_UUID,))
    if value is not None and decode_mibeacon_button(value) is not None:
        return PROTOCOL_XIAOMI_BUTTON
    return None


def decode_button(protocol: str,
                  service_data: dict | None = None) -> dict | None:
    """Decode one advertisement for an already-identified button protocol.
    Returns the {"battery", "counter", "buttons", "events"} dict or None.
    Pure: advertisement data in, events out."""
    if protocol == PROTOCOL_BTHOME_BUTTON:
        value = _service_value(service_data, (BTHOME_SERVICE_UUID,))
        return decode_bthome_button(value) if value is not None else None
    if protocol == PROTOCOL_XIAOMI_BUTTON:
        value = _service_value(service_data, (XIAOMI_SERVICE_UUID,))
        return decode_mibeacon_button(value) if value is not None else None
    return None


# One physical press arrives as a burst of identical advertisements, and
# bleak may hand the same packet to the callback more than once, so events
# are deduped before they leave the daemon. With a packet counter the rule
# is exact (same counter = same press); without one, repeats of the same
# event inside this window are treated as one press.
BUTTON_DEDUPE_WINDOW = 2.0
# A repeated counter older than this is a wrapped counter, not a repeat.
_BUTTON_COUNTER_TTL = 60.0
# Dedupe bookkeeping is pruned past this age so the map cannot grow forever.
_BUTTON_DEDUPE_PRUNE = 600.0


def dedupe_button_events(state: dict, device_id: str, decoded: dict,
                         now: float,
                         window: float = BUTTON_DEDUPE_WINDOW) -> list[dict]:
    """Filter one decoded advertisement down to genuinely new press events.

    ``state`` is a mutable {key: {"counter", "ts"}} map the caller keeps
    between advertisements (mutated in place, old entries pruned).
    ``decoded`` is a decode_button result. Returns the events that represent
    a new physical press: "none" placeholders are dropped, a repeated packet
    counter within its TTL is a radio repeat, and with no counter a repeat
    of the same (device, button, event) inside ``window`` seconds is one
    press. Pure apart from mutating ``state``: no clocks, no I/O.
    """
    for key in [k for k, v in state.items()
                if now - (v or {}).get("ts", 0) > _BUTTON_DEDUPE_PRUNE]:
        del state[key]
    fresh: list[dict] = []
    counter = (decoded or {}).get("counter")
    for ev in (decoded or {}).get("events") or []:
        event = ev.get("event")
        if event in (None, "", "none"):
            continue
        key = f"{device_id}:{ev.get('button')}:{event}"
        prev = state.get(key) or {}
        prev_ts = prev.get("ts", 0)
        if counter is not None:
            if prev.get("counter") == counter and now - prev_ts <= _BUTTON_COUNTER_TTL:
                continue
        elif now - prev_ts <= window:
            continue
        state[key] = {"counter": counter, "ts": now}
        fresh.append(dict(ev))
    return fresh


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


# --------------------------------------------------------------------------
# Door/window contact sensors (advertising-only; FoodAssistant-5c61)
# --------------------------------------------------------------------------
#
# A third device class next to the probes and the hygrometers: a magnet
# sensor on a fridge or freezer door that broadcasts open/closed. Sources for
# the byte layouts:
#
# * BTHome v2 (Shelly BLU Door/Window and any other BTHome v2 device that
#   carries an opening/door/window object): the published open standard at
#   https://bthome.io/format/. Service data on UUID 0xFCD2: a device-info
#   byte (version in the top three bits, encryption in bit 0), then a flat
#   list of (object id, value) measurements. Encrypted BTHome frames are NOT
#   supported (they need a per-device key).
# * SwitchBot Contact Sensor: the pySwitchbot parser
#   (https://github.com/sblibs/pySwitchbot, adv_parser). Service data on the
#   same 0xFD3D UUID as the meters, device-type byte 'd' (0x64); the open
#   bit and a held-open-timeout bit live in byte 3, battery in byte 2.
# * Xiaomi MiBeacon door/window sensors, UNENCRYPTED frames only: the
#   community MiBeacon documentation (ble_monitor,
#   https://github.com/custom-components/ble_monitor). Service data on UUID
#   0xFE95; frames with the encryption bit set (every bindkey device, which
#   includes most current Xiaomi door sensors) are NOT supported.

PROTOCOL_BTHOME_CONTACT = "bthome_contact"
PROTOCOL_SWITCHBOT_CONTACT = "switchbot_contact"
PROTOCOL_XIAOMI_CONTACT = "xiaomi_contact"

CONTACT_PROTOCOLS = (PROTOCOL_BTHOME_CONTACT, PROTOCOL_SWITCHBOT_CONTACT,
                     PROTOCOL_XIAOMI_CONTACT)

BTHOME_SERVICE_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"
XIAOMI_SERVICE_UUID = "0000fe95-0000-1000-8000-00805f9b34fb"

# The SwitchBot device-type byte for the Contact Sensor ('d' in pySwitchbot).
_SWITCHBOT_CONTACT_TYPE = 0x64

# BTHome v2 object ids that mean "something is open": opening (0x11),
# door (0x1A), garage door (0x1B), window (0x2D). All are 1-byte binaries
# (0 closed, 1 open). The Shelly BLU Door/Window uses the window object.
_BTHOME_OPEN_IDS = (0x11, 0x1A, 0x1B, 0x2D)
_BTHOME_BATTERY_ID = 0x01

# Value lengths per BTHome v2 object id (https://bthome.io/format/), needed
# to walk the flat measurement list. Ids not listed here stop the walk (an
# unknown id makes every later offset unknowable). Binary sensors
# (0x0F-0x30) are all 1 byte.
_BTHOME_OBJECT_LEN = {
    0x00: 1, 0x01: 1, 0x02: 2, 0x03: 2, 0x04: 3, 0x05: 3, 0x06: 2, 0x07: 2,
    0x08: 2, 0x09: 1, 0x0A: 3, 0x0B: 3, 0x0C: 2, 0x0D: 2, 0x0E: 2,
    0x12: 2, 0x13: 2, 0x14: 2,
    0x3A: 1, 0x3C: 2, 0x3D: 2, 0x3E: 4, 0x3F: 2,
    0x40: 2, 0x41: 2, 0x42: 3, 0x43: 2, 0x44: 2, 0x45: 2, 0x46: 1, 0x47: 2,
    0x48: 2, 0x49: 2, 0x4A: 2, 0x4B: 3, 0x4C: 4, 0x4D: 4, 0x4E: 4, 0x4F: 4,
    0x50: 4, 0x51: 2, 0x52: 2,
}
# Every 1-byte binary sensor object (generic boolean through window).
for _oid in range(0x0F, 0x31):
    _BTHOME_OBJECT_LEN.setdefault(_oid, 1)
del _oid


def decode_bthome_v2(value: bytes) -> dict | None:
    """Decode a BTHome v2 service-data payload into the fields we care about.

    Returns {"open": bool | None, "battery_pct": int | None} with "open" set
    when the frame carries an opening/door/garage/window object. Returns None
    for an encrypted frame, a non-v2 frame, or an empty payload. The walk
    stops quietly at the first unknown object id (later offsets would be
    guesses), keeping whatever was already decoded. Pure.
    """
    if not value:
        return None
    info = value[0]
    if info & 0x01:          # encrypted: needs a key we do not have
        return None
    if (info >> 5) & 0x07 != 2:   # not BTHome v2
        return None
    open_state: bool | None = None
    battery: int | None = None
    i = 1
    while i < len(value):
        obj = value[i]
        length = _BTHOME_OBJECT_LEN.get(obj)
        if length is None or i + 1 + length > len(value):
            break
        chunk = value[i + 1:i + 1 + length]
        if obj in _BTHOME_OPEN_IDS:
            open_state = bool(chunk[0])
        elif obj == _BTHOME_BATTERY_ID:
            battery = max(0, min(100, int(chunk[0])))
        i += 1 + length
    return {"open": open_state, "battery_pct": battery}


def decode_switchbot_contact(value: bytes) -> dict | None:
    """Decode a SwitchBot Contact Sensor service-data payload.

    Layout (pySwitchbot): device type (must be 'd'), a motion/flags byte,
    battery in the low 7 bits of byte 2, then byte 3 with the contact-open
    bit (0x02) and the held-open-timeout bit (0x04, the sensor's own "open
    too long"); either bit reads as open. Returns
    {"open", "battery_pct", "timeout"} or None. Pure.
    """
    if len(value) < 4:
        return None
    if (value[0] & 0x7F) != _SWITCHBOT_CONTACT_TYPE:
        return None
    battery = value[2] & 0x7F
    timeout = bool(value[3] & 0x04)
    opened = bool(value[3] & 0x02) or timeout
    return {"open": opened, "battery_pct": battery, "timeout": timeout}


# MiBeacon frame-control bits (ble_monitor docs).
_MIBEACON_ENCRYPTED = 0x0008
_MIBEACON_HAS_MAC = 0x0010
_MIBEACON_HAS_CAPABILITY = 0x0020
_MIBEACON_HAS_OBJECT = 0x0040
# MiBeacon object ids: door/window state and battery percent.
_MIBEACON_OBJ_DOOR = 0x1019
_MIBEACON_OBJ_BATTERY = 0x100A


def decode_xiaomi_contact(value: bytes) -> dict | None:
    """Decode an UNENCRYPTED Xiaomi MiBeacon frame's door/battery objects.

    Frame (ble_monitor): frame control (uint16 LE), product id (uint16),
    frame counter, then a MAC (6, if flagged), a capability byte (if
    flagged), then one object: id (uint16 LE), length, payload. The door
    object (0x1019) reads 0 = open, 1 = closed, 2 = left open past the
    sensor's own timeout (still open), 3 = device reset (ignored). Frames
    with the encryption bit set return None; that covers every bindkey
    device. Returns {"open": bool | None, "battery_pct": int | None} or
    None. Pure.
    """
    if len(value) < 5:
        return None
    frame_ctl = int.from_bytes(value[0:2], "little")
    if frame_ctl & _MIBEACON_ENCRYPTED:
        return None
    if not frame_ctl & _MIBEACON_HAS_OBJECT:
        return None
    i = 5
    if frame_ctl & _MIBEACON_HAS_MAC:
        i += 6
    if frame_ctl & _MIBEACON_HAS_CAPABILITY:
        i += 1
    if i + 3 > len(value):
        return None
    obj_id = int.from_bytes(value[i:i + 2], "little")
    obj_len = value[i + 2]
    payload = value[i + 3:i + 3 + obj_len]
    if len(payload) < obj_len or not payload:
        return None
    open_state: bool | None = None
    battery: int | None = None
    if obj_id == _MIBEACON_OBJ_DOOR:
        state = payload[0]
        if state in (0, 2):        # open, or open past the sensor's timeout
            open_state = True
        elif state == 1:
            open_state = False
        else:
            return None            # a reset event carries no door state
    elif obj_id == _MIBEACON_OBJ_BATTERY:
        battery = max(0, min(100, int(payload[0])))
    else:
        return None
    return {"open": open_state, "battery_pct": battery}


def identify_contact(name: str | None, manufacturer_data: dict | None = None,
                     service_data: dict | None = None) -> str | None:
    """Classify an advertisement as one of the supported contact protocols.

    BTHome v2 devices only match when the frame carries an opening-type
    object, so a BTHome hygrometer or button is never claimed as a door
    sensor. SwitchBot matches on the contact device-type byte (the meters
    have their own types and stay hygrometers). Xiaomi matches only an
    unencrypted MiBeacon frame with a door/window object. Pure.
    """
    bthome = _service_value(service_data, (BTHOME_SERVICE_UUID,))
    if bthome is not None:
        decoded = decode_bthome_v2(bthome)
        if decoded and decoded.get("open") is not None:
            return PROTOCOL_BTHOME_CONTACT
    sb = _service_value(service_data, SWITCHBOT_SERVICE_UUIDS)
    if sb is not None and len(sb) >= 4 and (sb[0] & 0x7F) == _SWITCHBOT_CONTACT_TYPE:
        return PROTOCOL_SWITCHBOT_CONTACT
    mi = _service_value(service_data, (XIAOMI_SERVICE_UUID,))
    if mi is not None:
        decoded = decode_xiaomi_contact(mi)
        if decoded and decoded.get("open") is not None:
            return PROTOCOL_XIAOMI_CONTACT
    return None


def decode_contact(protocol: str, manufacturer_data: dict | None = None,
                   service_data: dict | None = None) -> dict | None:
    """Decode one advertisement for an already-identified contact protocol.

    Returns {"open": bool | None, "battery_pct": int | None} ("open" None
    when this particular frame carries no door state, e.g. a Xiaomi
    battery-only frame), or None when nothing decodes. Pure.
    """
    if protocol == PROTOCOL_BTHOME_CONTACT:
        value = _service_value(service_data, (BTHOME_SERVICE_UUID,))
        return decode_bthome_v2(value) if value else None
    if protocol == PROTOCOL_SWITCHBOT_CONTACT:
        value = _service_value(service_data, SWITCHBOT_SERVICE_UUIDS)
        return decode_switchbot_contact(value) if value else None
    if protocol == PROTOCOL_XIAOMI_CONTACT:
        value = _service_value(service_data, (XIAOMI_SERVICE_UUID,))
        return decode_xiaomi_contact(value) if value else None
    return None


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
