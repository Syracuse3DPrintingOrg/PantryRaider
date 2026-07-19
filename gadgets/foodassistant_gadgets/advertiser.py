"""BLE status broadcast: the Pi advertises a tiny Cub summary packet.

Phase 2 of the Bandit Cub work (docs/design/bandit-cub.md, "BLE broadcast
payload"): the gadgets agent polls the local app's ``GET /cub/summary`` and
re-broadcasts the glanceable numbers as a legacy 31-byte BLE advertisement,
so a battery e-ink display or a radio-only ESP32 can show kitchen status
without wifi, pairing, or any per-receiver state on the server.

Layout (23 bytes total, multi-byte fields little-endian):

  offset  size  field
  0       3     Flags AD (0x02 0x01 0x06: LE general discoverable, no BR/EDR)
  3       1     MSD AD length (0x13 = 19 bytes follow: type + company + 16)
  4       1     MSD AD type (0xFF, Manufacturer Specific Data)
  5       2     Company ID 0xFFFF (unregistered, confirmed for now)
  7       1     Payload format version (1)
  8       1     Sequence counter (receivers skip repaints when unchanged)
  9       1     View hint low nibble (0 idle/clock, 1 expiring, 2 timers,
                3 probe) + alert flags high nibble (0x10 timer ringing,
                0x20 probe at target, 0x40 attention: protection alarms live)
  10      1     Expired item count (clamped to 255)
  11      1     Expiring-soon count, includes items expiring today (clamped)
  12      1     Pending scan count (clamped)
  13      1     Active timer count (clamped)
  14      2     Soonest timer remaining, seconds, u16 (0xFFFF none)
  16      2     Probe temperature, tenths of a degree C, i16 (0x7FFF none)
  18      1     Probe delta to target, whole degrees C, i8 (0x7F none;
                computed as target minus current, so positive means degrees
                still to climb toward an "above" target)
  19      4     Install tag: first 4 bytes of sha256(device_id), so a
                receiver in a two-server household locks onto one sender

Never-leak rule: counts, seconds, and temperatures only. No item names, no
tokens, nothing identifying ever goes in the packet (it is cleartext to
anyone in radio range).

The packing side here is pure (dict in, bytes out) and mirrored by
``unpack_status`` so the ESPHome receiver has a tested reference to copy.
The D-Bus/bluez plumbing lives in ``BroadcastAdvertiser`` below and is only
imported when the broadcast actually runs, so this module (and its tests)
never need dbus or a radio.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import struct
import time
from typing import Awaitable, Callable

log = logging.getLogger("foodassistant.gadgets.advertiser")

FORMAT_VERSION = 1
COMPANY_ID = 0xFFFF

# Sentinels per the design table.
NO_TIMER = 0xFFFF
NO_TEMP = 0x7FFF
NO_DELTA = 0x7F

# View hint nibble values. Anything else the summary might say ("clock",
# "rotation", "alert", or a view from a future server) packs as 0 (idle);
# an "alert" state rides the attention flag bit, not the view nibble.
VIEW_HINTS = {"expiring": 1, "timers": 2, "probe": 3}

# Alert flag bits (the high nibble of the view byte).
FLAG_TIMER_RINGING = 0x10
FLAG_PROBE_AT_TARGET = 0x20
FLAG_ATTENTION = 0x40

_FLAGS_AD = bytes((0x02, 0x01, 0x06))
_MSD_HEADER = bytes((0x13, 0xFF, 0xFF, 0xFF))  # len 19, type MSD, id 0xFFFF

# Where the sequence byte sits in the full packet, for change comparison.
_SEQ_OFFSET = 8
_TOTAL_LEN = 23


def _clamp_u8(value) -> int:
    try:
        return max(0, min(255, int(value)))
    except (TypeError, ValueError):
        return 0


def install_tag(device_id: str) -> bytes:
    """First 4 bytes of sha256(device_id): the sender-identity tag."""
    return hashlib.sha256((device_id or "").encode("utf-8")).digest()[:4]


def _pick_probe(probes: list) -> dict | None:
    """The one probe row worth broadcasting: prefer a fresh probe with a
    target armed (that is the one a receiver cares about), else the first
    fresh probe with a temperature at all."""
    rows = [p for p in probes if isinstance(p, dict)
            and not p.get("stale")
            and isinstance(p.get("temp_c"), (int, float))]
    for row in rows:
        if isinstance(row.get("target_c"), (int, float)):
            return row
    return rows[0] if rows else None


def _probe_at_target(probe: dict | None) -> bool:
    if not probe or not isinstance(probe.get("target_c"), (int, float)):
        return False
    temp, target = probe.get("temp_c"), probe["target_c"]
    if not isinstance(temp, (int, float)):
        return False
    if probe.get("direction") == "below":
        return temp <= target
    return temp >= target


def status_fields(summary: dict, now: float) -> bytes:
    """The 10 status bytes (view byte through probe delta) from a
    /cub/summary dict. Pure; ``now`` is the epoch the timer countdowns are
    measured against. Sequence and install tag are added by the caller, so
    two calls on unchanged content compare equal byte for byte."""
    summary = summary if isinstance(summary, dict) else {}

    def rows(key: str) -> list[dict]:
        value = summary.get(key)
        if not isinstance(value, list):
            return []
        return [row for row in value if isinstance(row, dict)]

    timers = rows("timers")
    probes = rows("probes")
    expiring = summary.get("expiring")
    counts = summary.get("counts")
    expiring = expiring if isinstance(expiring, dict) else {}
    counts = counts if isinstance(counts, dict) else {}

    view = VIEW_HINTS.get(summary.get("view"), 0)
    flags = 0
    if any(t.get("expired") for t in timers):
        flags |= FLAG_TIMER_RINGING
    probe = _pick_probe(probes)
    if _probe_at_target(probe):
        flags |= FLAG_PROBE_AT_TARGET
    if summary.get("alerts"):
        flags |= FLAG_ATTENTION

    expired = _clamp_u8(expiring.get("expired", 0))
    # A receiver has two buckets, not three: items expiring today are "soon".
    soon = _clamp_u8(_clamp_u8(expiring.get("today", 0))
                     + _clamp_u8(expiring.get("soon", 0)))
    pending = _clamp_u8(counts.get("pending", 0))
    timer_count = _clamp_u8(len(timers))

    soonest: int | None = None
    for t in timers:
        deadline = t.get("deadline_epoch")
        if not isinstance(deadline, (int, float)):
            continue
        remaining = max(0, int(round(deadline - now)))
        soonest = remaining if soonest is None else min(soonest, remaining)
    soonest = NO_TIMER if soonest is None else min(soonest, NO_TIMER - 1)

    temp_tenths = NO_TEMP
    delta = NO_DELTA
    if probe:
        temp_c = probe["temp_c"]
        temp_tenths = max(-32768, min(NO_TEMP - 1, int(round(temp_c * 10))))
        target = probe.get("target_c")
        if isinstance(target, (int, float)):
            delta = max(-128, min(NO_DELTA - 1, int(round(target - temp_c))))

    return struct.pack("<BBBBBHhb", (flags & 0xF0) | (view & 0x0F),
                       expired, soon, pending, timer_count,
                       soonest, temp_tenths, delta)


def pack_status(summary: dict, device_id: str, seq: int,
                now: float | None = None) -> bytes:
    """The full 23-byte advertisement (Flags AD + Manufacturer Specific Data)
    for one /cub/summary dict. Pass ``now`` explicitly for a pure call; it
    defaults to the wall clock for daemon convenience."""
    if now is None:
        now = time.time()
    packet = (_FLAGS_AD + _MSD_HEADER + bytes((FORMAT_VERSION, seq & 0xFF))
              + status_fields(summary, now) + install_tag(device_id))
    assert len(packet) == _TOTAL_LEN
    return packet


def msd_payload(packet: bytes) -> bytes:
    """The bytes bluez wants as ManufacturerData[0xFFFF]: everything after
    the company id (version through install tag, 16 bytes). bluez adds the
    Flags AD, the MSD header, and the company id itself."""
    return packet[7:]


def status_changed(prev_packet: bytes | None, new_packet: bytes) -> bool:
    """Whether two packed packets differ anywhere except the sequence byte.
    Pure; this is the daemon's re-register decision."""
    if prev_packet is None:
        return True

    def masked(pkt: bytes) -> bytes:
        return pkt[:_SEQ_OFFSET] + b"\x00" + pkt[_SEQ_OFFSET + 1:]

    return masked(prev_packet) != masked(new_packet)


def next_seq(prev_packet: bytes | None, new_packet: bytes, seq: int) -> int:
    """Pure sequence step: bump (mod 256) only when the packet content
    changed. On the very first packet the starting seq is kept as is."""
    if prev_packet is not None and status_changed(prev_packet, new_packet):
        return (seq + 1) & 0xFF
    return seq


def unpack_status(packet: bytes) -> dict:
    """Inverse of pack_status: the reference parser the ESPHome receiver
    mirrors. Raises ValueError on anything that is not our packet."""
    if len(packet) != _TOTAL_LEN:
        raise ValueError(f"expected {_TOTAL_LEN} bytes, got {len(packet)}")
    if packet[:3] != _FLAGS_AD:
        raise ValueError("bad Flags AD")
    if packet[3:7] != _MSD_HEADER:
        raise ValueError("bad MSD header or company id")
    version, seq = packet[7], packet[8]
    if version != FORMAT_VERSION:
        raise ValueError(f"unknown format version {version}")
    (view_byte, expired, soon, pending, timer_count,
     soonest, temp_tenths, delta) = struct.unpack("<BBBBBHhb", packet[9:19])
    return {
        "version": version,
        "seq": seq,
        "view": view_byte & 0x0F,
        "flags": {
            "timer_ringing": bool(view_byte & FLAG_TIMER_RINGING),
            "probe_at_target": bool(view_byte & FLAG_PROBE_AT_TARGET),
            "attention": bool(view_byte & FLAG_ATTENTION),
        },
        "expired": expired,
        "soon": soon,
        "pending": pending,
        "timer_count": timer_count,
        "soonest_timer_s": None if soonest == NO_TIMER else soonest,
        "probe_temp_c": None if temp_tenths == NO_TEMP else temp_tenths / 10.0,
        "probe_delta_c": None if delta == NO_DELTA else delta,
        "install_tag": packet[19:23].hex(),
    }


# Shared test vectors: representative summaries with the exact packet each
# must produce. Exported as data so the ESPHome receiver's parser tests can
# reuse them; tests/test_cub_payload.py verifies every entry against
# pack_status and writes the set to tests/data/cub_ble_vectors.json.
VECTORS: list[dict] = [
    {
        "name": "idle clock, nothing happening",
        "summary": {"view": "clock", "timers": [], "probes": [], "alerts": [],
                    "expiring": {"expired": 0, "today": 0, "soon": 0},
                    "counts": {"pending": 0}},
        "device_id": "aabbccdd00112233",
        "seq": 0,
        "now": 1750000000,
        "hex": "02010613ffffff01000000000000ffffff7f7f5eae1602",
    },
    {
        "name": "expiring view with counts",
        "summary": {"view": "expiring", "timers": [], "probes": [],
                    "alerts": [],
                    "expiring": {"expired": 2, "today": 1, "soon": 4},
                    "counts": {"pending": 3}},
        "device_id": "aabbccdd00112233",
        "seq": 7,
        "now": 1750000000,
        "hex": "02010613ffffff01070102050300ffffff7f7f5eae1602",
    },
    {
        "name": "two timers, soonest 754 s",
        "summary": {"view": "timers",
                    "timers": [
                        {"id": "a", "deadline_epoch": 1750000754,
                         "expired": False},
                        {"id": "b", "deadline_epoch": 1750003600,
                         "expired": False}],
                    "probes": [], "alerts": [],
                    "expiring": {"expired": 0, "today": 0, "soon": 1},
                    "counts": {"pending": 0}},
        "device_id": "aabbccdd00112233",
        "seq": 12,
        "now": 1750000000,
        "hex": "02010613ffffff010c0200010002f202ff7f7f5eae1602",
    },
    {
        "name": "timer ringing",
        "summary": {"view": "timers",
                    "timers": [{"id": "a", "deadline_epoch": 1749999980,
                                "expired": True}],
                    "probes": [], "alerts": [],
                    "expiring": {"expired": 0, "today": 0, "soon": 0},
                    "counts": {"pending": 0}},
        "device_id": "aabbccdd00112233",
        "seq": 13,
        "now": 1750000000,
        "hex": "02010613ffffff010d12000000010000ff7f7f5eae1602",
    },
    {
        "name": "probe climbing to an above target",
        "summary": {"view": "probe", "timers": [], "alerts": [],
                    "probes": [{"id": "P1", "probe": 1, "temp_c": 57.5,
                                "target_c": 93.0, "direction": "above",
                                "stale": False}],
                    "expiring": {"expired": 0, "today": 0, "soon": 0},
                    "counts": {"pending": 0}},
        "device_id": "aabbccdd00112233",
        "seq": 40,
        "now": 1750000000,
        "hex": "02010613ffffff01280300000000ffff3f02245eae1602",
    },
    {
        "name": "probe at target, flag set, delta 0",
        "summary": {"view": "probe", "timers": [], "alerts": [],
                    "probes": [{"id": "P1", "probe": 1, "temp_c": 93.4,
                                "target_c": 93.0, "direction": "above",
                                "stale": False}],
                    "expiring": {"expired": 0, "today": 0, "soon": 0},
                    "counts": {"pending": 0}},
        "device_id": "aabbccdd00112233",
        "seq": 41,
        "now": 1750000000,
        "hex": "02010613ffffff01292300000000ffffa603005eae1602",
    },
    {
        "name": "protection alarm live: attention flag, idle nibble",
        "summary": {"view": "alert", "timers": [], "probes": [],
                    "alerts": [{"kind": "hygrometer", "id": "F1",
                                "message": "Fridge above range"}],
                    "expiring": {"expired": 0, "today": 0, "soon": 0},
                    "counts": {"pending": 0}},
        "device_id": "aabbccdd00112233",
        "seq": 42,
        "now": 1750000000,
        "hex": "02010613ffffff012a4000000000ffffff7f7f5eae1602",
    },
    {
        "name": "everything clamped: counts over 255, timer over u16",
        "summary": {"view": "expiring",
                    "timers": [{"id": str(i),
                                "deadline_epoch": 1750000000 + 70000 + i,
                                "expired": False} for i in range(300)],
                    "probes": [{"id": "P1", "probe": 1, "temp_c": -4000.0,
                                "target_c": 4000.0, "direction": "above",
                                "stale": False}],
                    "alerts": [],
                    "expiring": {"expired": 999, "today": 200, "soon": 200},
                    "counts": {"pending": 400}},
        "device_id": "other-install",
        "seq": 255,
        "now": 1750000000,
        "hex": "02010613ffffff01ff01fffffffffeff00807ee1ed4a34",
    },
    {
        "name": "stale probe ignored, negative delta below target",
        "summary": {"view": "probe", "timers": [], "alerts": [],
                    "probes": [
                        {"id": "OLD", "probe": 1, "temp_c": 99.0,
                         "target_c": 50.0, "stale": True},
                        {"id": "P2", "probe": 1, "temp_c": 8.4,
                         "target_c": 4.0, "direction": "below",
                         "stale": False}],
                    "expiring": {"expired": 0, "today": 0, "soon": 0},
                    "counts": {"pending": 0}},
        "device_id": "aabbccdd00112233",
        "seq": 3,
        "now": 1750000000,
        "hex": "02010613ffffff01030300000000ffff5400fc5eae1602",
    },
]


# -- The broadcast loop (bluez over D-Bus, fail-soft everywhere) ---------------

_ADV_PATH = "/com/foodassistant/advertisement0"
_BACKOFF_MIN = 15
_BACKOFF_MAX = 300


class BroadcastAdvertiser:
    """Polls the local app's /cub/summary and keeps a bluez broadcast
    advertisement registered with the packed status, re-registering only
    when the bytes change (bluez reads the advertisement's properties at
    registration time, so an update is unregister + register).

    All radio and D-Bus failures degrade to a logged line and a backoff;
    this loop must never take the daemon's scan loop down with it.
    """

    def __init__(self, fetch_summary: Callable[[], Awaitable[dict | None]],
                 is_enabled: Callable[[], bool],
                 get_device_id: Callable[[], str],
                 poll_seconds: int = 5):
        self._fetch_summary = fetch_summary
        self._is_enabled = is_enabled
        self._get_device_id = get_device_id
        self._poll_seconds = max(2, int(poll_seconds))
        self._seq = 0
        self._packet: bytes | None = None
        self._bus = None
        self._adv = None
        self._manager = None
        self._registered = False
        self._backoff = _BACKOFF_MIN
        self._warned_unavailable = False

    async def run(self) -> None:
        while True:
            try:
                await self._tick()
                self._backoff = _BACKOFF_MIN
            except asyncio.CancelledError:
                await self._teardown()
                raise
            except Exception as exc:
                # One warning, then quiet retries: a box with no adapter or
                # an old bluez should not fill the journal every 5 seconds.
                if not self._warned_unavailable:
                    log.warning("BLE broadcast unavailable (%s); will keep "
                                "retrying quietly", exc)
                    self._warned_unavailable = True
                await self._teardown()
                await asyncio.sleep(self._backoff)
                self._backoff = min(_BACKOFF_MAX, self._backoff * 2)
                continue
            await asyncio.sleep(self._poll_seconds)

    async def _tick(self) -> None:
        device_id = self._get_device_id()
        if not self._is_enabled() or not device_id:
            await self._teardown()
            return
        summary = await self._fetch_summary()
        if not isinstance(summary, dict):
            return  # app unreachable this round; keep the last packet on air
        packet = pack_status(summary, device_id, self._seq)
        self._seq = next_seq(self._packet, packet, self._seq)
        packet = pack_status(summary, device_id, self._seq)
        if self._registered and not status_changed(self._packet, packet):
            return
        await self._register(packet)
        self._packet = packet
        if self._warned_unavailable:
            log.info("BLE broadcast recovered")
            self._warned_unavailable = False

    # -- bluez plumbing ---------------------------------------------------------

    async def _ensure_bus(self):
        if self._bus is not None:
            return
        # Imported lazily: dbus_fast ships with bleak on Linux, but the pure
        # packer above must import (and test) without it.
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._bus = bus
        self._adv = _make_advertisement_interface()
        bus.export(_ADV_PATH, self._adv)
        self._manager = await self._find_adv_manager(bus)

    async def _find_adv_manager(self, bus):
        """The first adapter that can advertise, via bluez's ObjectManager."""
        introspection = await bus.introspect("org.bluez", "/")
        root = bus.get_proxy_object("org.bluez", "/", introspection)
        objects = await root.get_interface(
            "org.freedesktop.DBus.ObjectManager").call_get_managed_objects()
        for path, ifaces in objects.items():
            if "org.bluez.LEAdvertisingManager1" in ifaces:
                node = await bus.introspect("org.bluez", path)
                proxy = bus.get_proxy_object("org.bluez", path, node)
                return proxy.get_interface("org.bluez.LEAdvertisingManager1")
        raise RuntimeError("no Bluetooth adapter with LE advertising support")

    async def _register(self, packet: bytes) -> None:
        await self._ensure_bus()
        if self._registered:
            try:
                await self._manager.call_unregister_advertisement(_ADV_PATH)
            except Exception:
                pass  # bluez may have dropped it already (adapter bounce)
            self._registered = False
        self._adv.set_payload(msd_payload(packet))
        await self._manager.call_register_advertisement(_ADV_PATH, {})
        self._registered = True

    async def _teardown(self) -> None:
        if self._registered and self._manager is not None:
            try:
                await self._manager.call_unregister_advertisement(_ADV_PATH)
            except Exception:
                pass
        self._registered = False
        if self._bus is not None:
            try:
                self._bus.disconnect()
            except Exception:
                pass
        self._bus = None
        self._adv = None
        self._manager = None
        self._packet = None


def _make_advertisement_interface():
    """Build the org.bluez.LEAdvertisement1 D-Bus object (dbus_fast service
    interface). In a function so importing this module never needs dbus."""
    from dbus_fast import Variant
    from dbus_fast.service import (PropertyAccess, ServiceInterface,
                                   dbus_property, method)

    class Advertisement(ServiceInterface):
        def __init__(self):
            super().__init__("org.bluez.LEAdvertisement1")
            self._payload = b""

        def set_payload(self, payload: bytes) -> None:
            self._payload = bytes(payload)

        @method()
        def Release(self):  # noqa: N802 (D-Bus method name)
            pass

        @dbus_property(access=PropertyAccess.READ)
        def Type(self) -> "s":  # noqa: F821, N802
            # Broadcast: non-connectable, exactly what a status beacon wants.
            return "broadcast"

        @dbus_property(access=PropertyAccess.READ)
        def ManufacturerData(self) -> "a{qv}":  # noqa: F821, F722, N802
            return {COMPANY_ID: Variant("ay", self._payload)}

        @dbus_property(access=PropertyAccess.READ)
        def Includes(self) -> "as":  # noqa: F821, F722, N802
            return []

        # A 1 s interval per the design doc: receivers scanning ~2 s are
        # near-certain to catch a packet. Older bluez ignores unknown
        # advertisement properties, so this degrades to the default there.
        @dbus_property(access=PropertyAccess.READ)
        def MinInterval(self) -> "u":  # noqa: F821, N802
            return 1000

        @dbus_property(access=PropertyAccess.READ)
        def MaxInterval(self) -> "u":  # noqa: F821, N802
            return 1000

    return Advertisement()
