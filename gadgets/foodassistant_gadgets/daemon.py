"""The BLE reader loop: scan, connect, decode, and post to the app.

Design rules:

* Never crash. Every task is wrapped so a flaky radio, a rebooting app, or a
  thermometer wandering out of range logs a line and retries with backoff.
* The app owns the configuration. The daemon polls ``GET /gadgets/config``
  for the enabled flag and the device list, so adding a thermometer in the
  web UI reaches the daemon without touching any file on the host.
* Readings flow one way: the daemon POSTs ``/gadgets/readings`` snapshots
  every few seconds. Unconfigured thermometers it can see ride along in the
  same payload as ``discovered`` entries so the UI can offer to add them.

Combustion probes need no connection at all (temperatures ride the BLE
advertisement); Inkbird, ThermoPro, and BlueDOT need a GATT connection and a
notification subscription, one background task per configured device.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
from bleak import BleakClient, BleakScanner

from . import decoders
from .config import Config

log = logging.getLogger("foodassistant.gadgets")


def _adapter_is_off(exc: Exception) -> bool:
    """Whether a scan error means the Bluetooth radio is off or missing.

    bleak raises BleakBluetoothNotAvailableError on a powered-off or absent
    adapter, but older releases surface it as a plain BleakError with a bluez
    "not ready / powered off" message. Match by class name (so we do not need
    the newer class to exist) and by message text.
    """
    if type(exc).__name__ == "BleakBluetoothNotAvailableError":
        return True
    text = str(exc).lower()
    return any(marker in text for marker in (
        "not available", "not ready", "powered off", "no powered",
        "no bluetooth adapters", "no such adapter", "rfkill",
    ))

# A reading older than this is dropped from the push payload (the device is
# gone or asleep; the app also applies its own staleness rules on top).
_READING_TTL = 90
# How long a discovered-but-unconfigured device stays in the payload after
# its last advertisement.
_DISCOVERED_TTL = 120
# Reconnect backoff bounds for the per-device connection tasks.
_BACKOFF_MIN = 5
_BACKOFF_MAX = 60
# How often to nudge connected devices that want periodic prompts (a battery
# request for Inkbird, a temperature-report request for ThermoPro).
_NUDGE_SECONDS = 60


def _norm_id(value: str) -> str:
    return str(value or "").strip().upper()


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # Latest state pulled from the app: {"enabled": bool, "devices": [...]}.
        self.enabled = False
        self.devices: dict[str, dict] = {}
        for dev in cfg.devices:
            self.devices[_norm_id(dev.get("id"))] = dict(dev)
        # Latest reading per device id: the dict POSTed inside "devices".
        self.readings: dict[str, dict] = {}
        # Discovered-but-unconfigured devices: id -> {name, protocol, rssi,
        # supported, ts}.
        self.discovered: dict[str, dict] = {}
        # Per-device connection tasks, keyed by normalized id.
        self._conn_tasks: dict[str, asyncio.Task] = {}
        # Bluetooth adapter health, threaded through to the app so the
        # Thermometers card can say "Bluetooth is turned off on this device"
        # instead of a silent empty list. Starts optimistic; the scan loop
        # flips it when the radio is off or missing.
        self.adapter_available = True
        self.adapter_detail = ""
        self._client = httpx.AsyncClient(timeout=10)

    # -- HTTP side ---------------------------------------------------------

    def _headers(self) -> dict:
        return {"X-API-Key": self.cfg.api_key} if self.cfg.api_key else {}

    async def _pull_config(self) -> None:
        resp = await self._client.get(
            f"{self.cfg.base_url}/gadgets/config", headers=self._headers()
        )
        resp.raise_for_status()
        data = resp.json()
        self.enabled = bool(data.get("enabled"))
        devices: dict[str, dict] = {}
        for dev in self.cfg.devices:
            devices[_norm_id(dev.get("id"))] = dict(dev)
        for dev in data.get("devices") or []:
            if isinstance(dev, dict) and dev.get("id"):
                devices[_norm_id(dev["id"])] = dict(dev)
        self.devices = devices

    async def config_loop(self) -> None:
        while True:
            try:
                await self._pull_config()
            except Exception as exc:
                log.warning("Could not pull config from the app: %s", exc)
            await asyncio.sleep(self.cfg.config_poll_seconds)

    def _snapshot(self) -> dict:
        now = time.time()
        fresh = [dict(r) for r in self.readings.values() if now - r.get("ts", 0) <= _READING_TTL]
        seen = [
            {k: v for k, v in d.items() if k != "ts"}
            for d in self.discovered.values()
            if now - d.get("ts", 0) <= _DISCOVERED_TTL
        ]
        return {
            "devices": fresh,
            "discovered": seen,
            "bluetooth": {"available": self.adapter_available,
                          "detail": self.adapter_detail},
        }

    async def push_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.push_seconds)
            # Discovered devices are pushed even while the feature is off in
            # the app: the "available to add" row on the Timers page is how a
            # user turns the feature on in the first place. A powered-off radio
            # is pushed too, even with nothing to report, so the app can say so.
            payload = self._snapshot()
            if (not payload["devices"] and not payload["discovered"]
                    and self.adapter_available):
                continue
            try:
                await self._client.post(
                    f"{self.cfg.base_url}/gadgets/readings",
                    json=payload, headers=self._headers(),
                )
            except Exception as exc:
                log.warning("Could not push readings to the app: %s", exc)

    # -- Reading bookkeeping -------------------------------------------------

    def record(self, device_id: str, protocol: str, name: str,
               probes: list, battery: int | None = None,
               rssi: int | None = None, extra: dict | None = None,
               roles: list | None = None,
               device_targets: list | None = None) -> None:
        """Store the latest reading for a device (probes are Celsius or None).

        ``roles`` (optional, parallel to ``probes``) tags each lead with a
        meaning like "internal" or "ambient"; ``device_targets`` (optional,
        parallel) carries a setpoint the device itself broadcasts (a Govee
        grill's on-device alarm). Both are omitted per-probe when None."""
        entry_probes = []
        for i, t in enumerate(probes):
            probe = {"index": i + 1, "temp_c": t}
            if roles and i < len(roles) and roles[i]:
                probe["role"] = roles[i]
            if device_targets and i < len(device_targets) and device_targets[i] is not None:
                probe["device_target_c"] = device_targets[i]
            entry_probes.append(probe)
        entry = {
            "id": _norm_id(device_id),
            "protocol": protocol,
            "name": name or "",
            "probes": entry_probes,
            "battery": battery,
            "rssi": rssi,
            "ts": time.time(),
        }
        if extra:
            entry.update(extra)
        self.readings[entry["id"]] = entry

    # -- Scanner (discovery + Combustion advertising decode) ----------------

    def _mark_discovered(self, dev_id: str, protocol: str, name: str,
                         rssi, supported: bool = True) -> None:
        self.discovered[dev_id] = {
            "id": dev_id, "protocol": protocol, "name": name or "",
            "rssi": rssi, "supported": supported, "ts": time.time(),
        }

    def _on_advertisement(self, device, adv) -> None:
        try:
            name = adv.local_name or device.name
            # Any advertisement is proof the radio is up; a scan callback fired.
            if not self.adapter_available:
                self.adapter_available = True
                self.adapter_detail = ""
            # Ambient room hygrometers (Govee GVH50xx and kin) are not cooking
            # probes; Dan does not want them in the thermometer list at all.
            if decoders.is_room_sensor(name, adv.manufacturer_data):
                return
            protocol = decoders.identify(
                name, adv.manufacturer_data, adv.service_uuids)
            if not protocol:
                # No decoder, but it reads like a probe (an iDevices "KT", an
                # iGrill, a Meater): surface it as seen-but-unsupported rather
                # than dropping it, so the user knows the reader saw it.
                if decoders.looks_like_probe(name):
                    self._mark_discovered(_norm_id(device.address), "", name,
                                          adv.rssi, supported=False)
                return
            if protocol == decoders.PROTOCOL_COMBUSTION:
                payload = adv.manufacturer_data.get(decoders.COMBUSTION_MANUFACTURER_ID)
                decoded = decoders.decode_combustion_advertising(payload or b"")
                if not decoded:
                    return
                dev_id = _norm_id(decoded["serial"])
                if dev_id in self.devices:
                    known = self.devices[dev_id]
                    self.record(
                        dev_id, protocol,
                        known.get("name") or f"Combustion {decoded['probe_id']}",
                        decoded["temps_c"],
                        battery=(5 if decoded["battery_low"] else 100),
                        rssi=adv.rssi,
                        extra={"instant_read": decoded["instant_read"]},
                    )
                else:
                    self._mark_discovered(
                        dev_id, protocol,
                        adv.local_name or f"Combustion probe {decoded['probe_id']}",
                        adv.rssi)
                return
            if protocol == decoders.PROTOCOL_TEMPSPIKE:
                dev_id = _norm_id(device.address)
                decoded = decoders.decode_tempspike_from_manufacturer(
                    adv.manufacturer_data)
                if not decoded:
                    self._mark_discovered(dev_id, protocol, name, adv.rssi)
                    return
                if dev_id in self.devices:
                    # Probe 1 is the tip in the food, probe 2 the ambient/pit.
                    self.record(dev_id, protocol,
                                self.devices[dev_id].get("name") or name,
                                [decoded["tip_c"], decoded["ambient_c"]],
                                battery=decoded["battery"], rssi=adv.rssi,
                                roles=["internal", "ambient"])
                else:
                    self._mark_discovered(dev_id, protocol, name, adv.rssi)
                return
            if protocol == decoders.PROTOCOL_GOVEE_GRILL:
                dev_id = _norm_id(device.address)
                value = decoders._govee_grill_value(adv.manufacturer_data)
                decoded = decoders.decode_govee_grill(value) if value else None
                if not decoded:
                    self._mark_discovered(dev_id, protocol, name, adv.rssi)
                    return
                if dev_id in self.devices:
                    self.record(dev_id, protocol,
                                self.devices[dev_id].get("name") or name,
                                decoded["probes"], rssi=adv.rssi,
                                device_targets=decoded.get("targets"))
                else:
                    self._mark_discovered(dev_id, protocol, name, adv.rssi)
                return
            dev_id = _norm_id(device.address)
            if dev_id in self.devices:
                # Track signal strength for connected protocols; the reading
                # itself arrives over the GATT connection.
                if dev_id in self.readings:
                    self.readings[dev_id]["rssi"] = adv.rssi
                return
            self._mark_discovered(dev_id, protocol, name, adv.rssi)
        except Exception:
            log.exception("Advertisement handling failed")

    async def scan_loop(self) -> None:
        if not self.cfg.scan:
            return
        while True:
            try:
                scanner = BleakScanner(detection_callback=self._on_advertisement)
                await scanner.start()
                self.adapter_available = True
                self.adapter_detail = ""
                try:
                    while True:
                        await asyncio.sleep(5)
                finally:
                    await scanner.stop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _adapter_is_off(exc):
                    # The radio is rfkill-blocked or powered off. Flag it so the
                    # app can tell the user, and keep retrying: a `bluetoothctl
                    # power on` or a reboot recovers without restarting us.
                    self.adapter_available = False
                    self.adapter_detail = str(exc)
                    log.warning("Bluetooth adapter unavailable (%s); "
                                "waiting for the radio to come up", exc)
                else:
                    log.warning("BLE scan failed (%s); retrying shortly", exc)
                await asyncio.sleep(15)

    # -- Per-device GATT connections -----------------------------------------

    async def connection_supervisor(self) -> None:
        """Keep one connection task alive per configured connectable device."""
        while True:
            wanted = {
                dev_id: dev for dev_id, dev in self.devices.items()
                if self.enabled and dev.get("protocol") in (
                    decoders.PROTOCOL_INKBIRD,
                    decoders.PROTOCOL_THERMOPRO,
                    decoders.PROTOCOL_BLUEDOT,
                )
            }
            for dev_id in list(self._conn_tasks):
                if dev_id not in wanted or self._conn_tasks[dev_id].done():
                    task = self._conn_tasks.pop(dev_id)
                    task.cancel()
            for dev_id, dev in wanted.items():
                if dev_id not in self._conn_tasks:
                    self._conn_tasks[dev_id] = asyncio.create_task(
                        self._device_loop(dev_id, dict(dev))
                    )
            await asyncio.sleep(5)

    async def _device_loop(self, dev_id: str, dev: dict) -> None:
        """Connect-decode-reconnect loop for one thermometer, forever."""
        backoff = _BACKOFF_MIN
        protocol = dev.get("protocol")
        name = dev.get("name") or dev_id
        while True:
            try:
                async with BleakClient(dev_id, timeout=20) as client:
                    log.info("Connected to %s (%s)", name, dev_id)
                    backoff = _BACKOFF_MIN
                    if protocol == decoders.PROTOCOL_INKBIRD:
                        await self._run_inkbird(client, dev_id, name)
                    elif protocol == decoders.PROTOCOL_THERMOPRO:
                        await self._run_thermopro(client, dev_id, name)
                    elif protocol == decoders.PROTOCOL_BLUEDOT:
                        await self._run_bluedot(client, dev_id, name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s (%s): %s; reconnecting in %ss",
                            name, dev_id, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(_BACKOFF_MAX, backoff * 2)

    async def _run_inkbird(self, client, dev_id: str, name: str) -> None:
        state = {"battery": None}

        def on_settings(_, data: bytearray):
            pct = decoders.decode_ibbq_battery(bytes(data))
            if pct is not None:
                state["battery"] = pct

        def on_realtime(_, data: bytearray):
            probes = decoders.decode_ibbq_realtime(bytes(data))
            self.record(dev_id, decoders.PROTOCOL_INKBIRD, name,
                        probes, battery=state["battery"],
                        rssi=self.readings.get(dev_id, {}).get("rssi"))

        await client.write_gatt_char(decoders.IBBQ_PAIR_UUID,
                                     decoders.IBBQ_CREDENTIALS, response=True)
        await client.start_notify(decoders.IBBQ_SETTINGS_RESULT_UUID, on_settings)
        await client.start_notify(decoders.IBBQ_REALTIME_UUID, on_realtime)
        await client.write_gatt_char(decoders.IBBQ_SETTINGS_UUID,
                                     decoders.IBBQ_UNITS_CELSIUS, response=True)
        await client.write_gatt_char(decoders.IBBQ_SETTINGS_UUID,
                                     decoders.IBBQ_ENABLE_REALTIME, response=True)
        while client.is_connected:
            await client.write_gatt_char(decoders.IBBQ_SETTINGS_UUID,
                                         decoders.IBBQ_REQUEST_BATTERY, response=True)
            await asyncio.sleep(_NUDGE_SECONDS)

    async def _run_thermopro(self, client, dev_id: str, name: str) -> None:
        def on_notify(_, data: bytearray):
            frame = decoders.decode_tp25_frame(bytes(data))
            if frame:
                self.record(dev_id, decoders.PROTOCOL_THERMOPRO, name,
                            frame["probes"], battery=frame["battery"],
                            rssi=self.readings.get(dev_id, {}).get("rssi"))

        await client.start_notify(decoders.TP25_NOTIFY_UUID, on_notify)
        await client.write_gatt_char(decoders.TP25_WRITE_UUID,
                                     decoders.TP25_HANDSHAKE, response=True)
        while client.is_connected:
            await client.write_gatt_char(decoders.TP25_WRITE_UUID,
                                         decoders.TP25_REQUEST_TEMPS, response=True)
            await asyncio.sleep(_NUDGE_SECONDS)

    async def _run_bluedot(self, client, dev_id: str, name: str) -> None:
        def on_notify(_, data: bytearray):
            frame = decoders.decode_bluedot(bytes(data))
            if frame:
                self.record(dev_id, decoders.PROTOCOL_BLUEDOT, name,
                            [frame["temp_c"]],
                            rssi=self.readings.get(dev_id, {}).get("rssi"),
                            extra={"alarm_active": frame["alarm_active"]})

        await client.start_notify(decoders.BLUEDOT_NOTIFY_UUID, on_notify)
        while client.is_connected:
            await asyncio.sleep(5)

    # -- Entry point ----------------------------------------------------------

    async def run(self) -> int:
        log.info("Gadget reader starting (app at %s)", self.cfg.base_url)
        try:
            await self._pull_config()
        except Exception as exc:
            log.warning("App not reachable yet (%s); will keep polling", exc)
        await asyncio.gather(
            self.config_loop(),
            self.push_loop(),
            self.scan_loop(),
            self.connection_supervisor(),
        )
        return 0


async def main_async(cfg: Config) -> int:
    return await Daemon(cfg).run()
