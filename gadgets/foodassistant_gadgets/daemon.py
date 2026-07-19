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
Hygrometers (Govee H5075-class, ATC Xiaomi, SwitchBot Meter, Inkbird IBS-TH)
are a separate device class, decoded passively from the same scan loop and
pushed in the same payload with kind="hygrometer".

Buttons (BTHome v2 buttons like the Shelly BLU Button1, unencrypted Xiaomi
MiBeacon switches) are a third class, also decoded passively from the same
scan. A press is an event, not a reading: it is deduped against the radio's
repeat bursts and POSTed to the app immediately as a kind="button" entry
carrying the event, so the mapped action fires without waiting for the next
periodic push. Battery and last-seen ride the normal snapshot.

The daemon can also transmit: when the server's cub_ble_advertise setting is
on, a broadcast task polls the local /cub/summary and advertises a tiny
status packet over BLE for battery displays (see advertiser.py). Off by
default, and a failure there never touches the reader loops.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
from bleak import BleakClient, BleakScanner

from . import decoders
from .advertiser import BroadcastAdvertiser
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
# How often the accessory LEDs reconcile with the app's state
# (FoodAssistant-kh1m). A press repaints them immediately, so this only has
# to catch a mode changed on another surface; a couple of seconds there is
# imperceptible and keeps the endpoint's cost at nothing.
_OUTPUTS_POLL_SECONDS = 2.5
# UART barcode scanner (FoodAssistant-x61t). How often the scanner loop checks
# whether the feature is enabled while it is off, and how often it re-checks the
# session flag while enabled but dark. Both are lazy: the module does nothing
# until the app turns it on, and stays dark until a scan session is active.
_SCANNER_IDLE_POLL_SECONDS = 5.0
_SCANNER_DARK_POLL_SECONDS = 0.5
# The pause between scan commands while a session is active. The module reads
# fast enough that this only paces the loop; the read timeout inside SerialScanner
# does most of the waiting.
_SCANNER_SCAN_INTERVAL = 0.15
# How long the same barcode is ignored after it is accepted. Command-mode
# continuous scanning re-reads a code that sits in view, so this window drops
# the repeats before they reach the app.
_SCANNER_DEDUP_SECONDS = 2.0
# Reconnect backoff bounds when the serial port cannot be opened or dies.
_SCANNER_BACKOFF_MIN = 5
_SCANNER_BACKOFF_MAX = 60


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
        # Configured hygrometers (the separate ambient temperature + humidity
        # class), keyed like devices. Advertising-only: never connected to.
        self.hygrometers: dict[str, dict] = {}
        for dev in cfg.hygrometers:
            self.hygrometers[_norm_id(dev.get("id"))] = dict(dev)
        # Configured buttons (the BLE push-button class), keyed like devices.
        # Advertising-only: never connected to.
        self.buttons: dict[str, dict] = {}
        for dev in cfg.buttons:
            self.buttons[_norm_id(dev.get("id"))] = dict(dev)
        # Button press dedupe bookkeeping (decoders.dedupe_button_events).
        self._button_seen: dict[str, dict] = {}
        # Configured door/window contact sensors (FoodAssistant-5c61), keyed
        # like devices. Advertising-only: never connected to.
        self.contacts: dict[str, dict] = {}
        for dev in cfg.contacts:
            self.contacts[_norm_id(dev.get("id"))] = dict(dev)
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
        # BLE status broadcast (FoodAssistant-yl6u): off until the server
        # says otherwise. The server's cub_ble_advertise setting and its
        # install id (the payload's sender tag) both arrive with the config
        # pull; cfg.advertise is the host-level opt-out.
        self.advertise = False
        self.server_device_id = ""
        # The I2C module (FoodAssistant-etsc): plug-in STEMMA QT / Qwiic
        # accessories on /dev/i2c-1. Built here but silent until it finds a
        # bus, so a host with no I2C (a Docker server, a Pi with the
        # interface off) is unaffected. cfg.i2c is the host-level opt-out.
        self._i2c = None
        if cfg.i2c:
            from .i2c import I2CModule
            self._i2c = I2CModule(
                cfg.i2c_bus,
                on_press=self._on_stemma_press,
                on_heartbeat=self._on_stemma_heartbeat,
                on_discovered=self._on_stemma_discovered,
            )
        # The UART barcode scanner (FoodAssistant-x61t): a 3.3V scan engine
        # wired to the Pi's GPIO UART, read here and POSTed to /pending/scan.
        # The whole block is pulled from the app: "scanner_uart" carries
        # {enabled, port, baud}, and the top-level scan_active / scanner_mode
        # say when to scan and what a scan means. Off (module untouched) until
        # the app turns it on; dark until a scan session is active.
        self._scanner_uart = {"enabled": False, "port": "/dev/serial0",
                              "baud": 9600}
        self._scan_active = False
        self._scanner_mode = ""
        # The latest GET /gadgets/outputs answer drives the accessory LEDs.
        self._advertiser = BroadcastAdvertiser(
            fetch_summary=self._fetch_cub_summary,
            is_enabled=lambda: self.cfg.advertise and self.advertise,
            get_device_id=lambda: self.server_device_id,
            poll_seconds=self.cfg.push_seconds,
        )

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
        hygrometers: dict[str, dict] = {}
        for dev in self.cfg.hygrometers:
            hygrometers[_norm_id(dev.get("id"))] = dict(dev)
        for dev in data.get("hygrometers") or []:
            if isinstance(dev, dict) and dev.get("id"):
                hygrometers[_norm_id(dev["id"])] = dict(dev)
        self.hygrometers = hygrometers
        buttons: dict[str, dict] = {}
        for dev in self.cfg.buttons:
            buttons[_norm_id(dev.get("id"))] = dict(dev)
        for dev in data.get("buttons") or []:
            if isinstance(dev, dict) and dev.get("id"):
                buttons[_norm_id(dev["id"])] = dict(dev)
        self.buttons = buttons
        contacts: dict[str, dict] = {}
        for dev in self.cfg.contacts:
            contacts[_norm_id(dev.get("id"))] = dict(dev)
        for dev in data.get("contacts") or []:
            if isinstance(dev, dict) and dev.get("id"):
                contacts[_norm_id(dev["id"])] = dict(dev)
        self.contacts = contacts
        self.advertise = bool(data.get("cub_ble_advertise"))
        self.server_device_id = str(data.get("device_id") or "")
        # Plug-in STEMMA QT / Qwiic accessories (FoodAssistant-etsc) ride the
        # same pull in their own block. Not merged with any upstream list: a
        # QT board is plugged into THIS host, so the app it talks to owns it.
        if self._i2c is not None:
            self._i2c.apply_config(data.get("stemma"))
        # The UART barcode scanner block (FoodAssistant-x61t) rides the same
        # pull. The port belongs to THIS host, so like the STEMMA block it is
        # not merged with any upstream list. scan_active and scanner_mode are
        # top-level: a scan session and its meaning are the app's state, shared
        # across every surface, and the scanner just follows it.
        block = data.get("scanner_uart")
        block = block if isinstance(block, dict) else {}
        self._scanner_uart = {
            "enabled": bool(block.get("enabled")),
            "port": str(block.get("port") or "/dev/serial0"),
            "baud": int(block.get("baud") or 9600),
        }
        self._scan_active = bool(data.get("scan_active"))
        self._scanner_mode = str(data.get("scanner_mode") or "")

    async def _fetch_cub_summary(self) -> dict | None:
        """One /cub/summary poll for the BLE broadcast; None when the app is
        unreachable so the advertiser keeps the last packet on the air."""
        try:
            resp = await self._client.get(
                f"{self.cfg.base_url}/cub/summary", headers=self._headers()
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception as exc:
            log.debug("Could not pull /cub/summary for the broadcast: %s", exc)
            return None

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
        payload = {
            "devices": fresh,
            "discovered": seen,
            "bluetooth": {"available": self.adapter_available,
                          "detail": self.adapter_detail},
        }
        if self._i2c is not None:
            # I2C bus health (FoodAssistant-etsc), so the accessories card can
            # say "I2C is not turned on here" rather than showing an empty
            # list and leaving the user to guess.
            payload["i2c"] = self._i2c.health()
        return payload

    async def push_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.push_seconds)
            # Discovered devices are pushed even while the feature is off in
            # the app: the "available to add" row on the Timers page is how a
            # user turns the feature on in the first place. A powered-off radio
            # is pushed too, even with nothing to report, so the app can say so.
            payload = self._snapshot()
            i2c_ok = payload.get("i2c", {}).get("available", True)
            if (not payload["devices"] and not payload["discovered"]
                    and self.adapter_available and i2c_ok):
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

    def record_hygrometer(self, device_id: str, protocol: str, name: str,
                          reading: dict, rssi: int | None = None) -> None:
        """Store the latest hygrometer reading (a decoders.decode_hygrometer
        dict). Hygrometers ride the same push payload as thermometers but are
        typed with kind="hygrometer" and carry humidity instead of probes."""
        self.readings[_norm_id(device_id)] = {
            "id": _norm_id(device_id),
            "kind": "hygrometer",
            "protocol": protocol,
            "name": name or "",
            "temp_c": reading.get("temp_c"),
            "humidity": reading.get("humidity_pct"),
            "battery": reading.get("battery_pct"),
            "rssi": rssi,
            "ts": time.time(),
        }

    def record_contact(self, device_id: str, protocol: str, name: str,
                       decoded: dict, rssi: int | None = None) -> None:
        """Store the latest door/window contact reading (kind="contact").

        Many sensors interleave frames (a Xiaomi battery-only frame carries no
        door state), so a frame missing "open" or the battery keeps the last
        value seen instead of blanking it."""
        dev_id = _norm_id(device_id)
        prev = self.readings.get(dev_id) or {}
        opened = decoded.get("open")
        if opened is None:
            opened = prev.get("open")
        battery = decoded.get("battery_pct")
        if battery is None:
            battery = prev.get("battery")
        if opened is None:
            return  # nothing to report yet (no door state seen so far)
        self.readings[dev_id] = {
            "id": dev_id,
            "kind": "contact",
            "protocol": protocol,
            "name": name or "",
            "open": bool(opened),
            "battery": battery,
            "rssi": rssi,
            "ts": time.time(),
        }

    def record_button(self, device_id: str, protocol: str, name: str,
                      battery: int | None, rssi: int | None) -> None:
        """Store a button's presence snapshot (kind="button"): battery and
        last-seen ride the periodic push like any other device. A frame with
        no battery keeps the last one seen, since many buttons only include
        the battery object in some advertisements."""
        dev_id = _norm_id(device_id)
        if battery is None:
            prev = self.readings.get(dev_id) or {}
            battery = prev.get("battery")
        self.readings[dev_id] = {
            "id": dev_id,
            "kind": "button",
            "protocol": protocol,
            "name": name or "",
            "battery": battery,
            "rssi": rssi,
            "ts": time.time(),
        }

    def _post_button_event(self, dev_id: str, protocol: str, name: str,
                           event: dict, counter: int | None,
                           battery: int | None, rssi: int | None) -> None:
        """POST one deduped press to the app right away (fire-and-forget).

        A press is an edge, not a level: waiting for the next periodic push
        would add seconds of lag between pressing the button and the item
        landing on the shopping list, so events take their own immediate POST
        through the same /gadgets/readings ingest path."""
        payload = {"devices": [{
            "id": dev_id,
            "kind": "button",
            "protocol": protocol,
            "name": name or "",
            "battery": battery,
            "rssi": rssi,
            "event": {"button": event.get("button"),
                      "type": event.get("event"),
                      "counter": counter},
        }]}

        async def _send() -> None:
            try:
                await self._client.post(
                    f"{self.cfg.base_url}/gadgets/readings",
                    json=payload, headers=self._headers(),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not push a button press to the app: %s", exc)

        try:
            asyncio.get_running_loop().create_task(_send())
        except RuntimeError:
            log.warning("No event loop to push a button press from")

    # -- I2C accessories (FoodAssistant-etsc, -kh1m) ------------------------

    def _on_stemma_press(self, mode: str) -> None:
        """A NeoKey key was pressed: change the scanner mode, now.

        Called on the event loop (the poll thread bounces it here), and the
        POST is its own immediate request rather than waiting for the next
        periodic push: a press is an edge, the same argument the BLE shelf
        buttons make. This lands on the scanner-mode API the kiosk and the
        Stream Deck already use, which means the state file syncs every
        worker and a satellite forwards to the main server for free, so a
        NeoKey on a Bandit sets the whole fleet's mode with no new routing.
        """
        async def _send() -> None:
            try:
                await self._client.post(
                    f"{self.cfg.base_url}/pending/scanner-mode",
                    json={"mode": mode, "source": "neokey"},
                    headers=self._headers(),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not set the scanner mode from a key: %s", exc)

        try:
            asyncio.get_running_loop().create_task(_send())
        except RuntimeError:
            log.warning("No event loop to send a key press from")

    def _on_stemma_heartbeat(self, beats: list) -> None:
        """Record each configured accessory's last-seen for the periodic
        push. Bus-powered, so answering the bus IS the whole reading."""
        for beat in beats or []:
            dev_id = str(beat.get("id") or "")
            if dev_id:
                self.readings[dev_id] = dict(beat, ts=time.time())

    def _on_stemma_discovered(self, found: list) -> None:
        """Report boards the sweep saw that are not configured yet, so the
        Settings pane can offer to add them."""
        for entry in found or []:
            dev_id = str(entry.get("id") or "")
            if dev_id:
                self.discovered[dev_id] = dict(entry, ts=time.time())

    async def outputs_loop(self) -> None:
        """Keep the accessory LEDs honest about the app's state.

        A press already recolored the keys optimistically, so this is for
        changes made anywhere else: the kiosk, the Stream Deck, another
        NeoKey. GET /gadgets/outputs is built from local app state only, so a
        2 to 3 second poll costs the app microseconds and the lit key follows
        a mode change from any surface within one poll.
        """
        if self._i2c is None:
            return
        while True:
            try:
                resp = await self._client.get(
                    f"{self.cfg.base_url}/gadgets/outputs",
                    headers=self._headers())
                resp.raise_for_status()
                self._i2c.apply_outputs(resp.json())
            except Exception as exc:  # noqa: BLE001
                log.debug("Could not pull outputs: %s", exc)
            await asyncio.sleep(_OUTPUTS_POLL_SECONDS)

    # -- UART barcode scanner (FoodAssistant-x61t) --------------------------

    async def _post_scan(self, barcode: str) -> None:
        """POST one decoded barcode to the app, routed by the current mode.

        Lands on /pending/scan, the same endpoint the kiosk's on-screen scanner
        posts to, so the scan flows through whatever the scanner mode is (stock
        up, use up, shopping list, audit) and shows on every surface. On a
        satellite the app forwards it to the main server for free.
        """
        try:
            await self._client.post(
                f"{self.cfg.base_url}/pending/scan",
                json={"barcode": barcode, "mode": self._scanner_mode},
                headers=self._headers())
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not POST a scanned barcode to the app: %s", exc)

    async def scanner_loop(self) -> None:
        """Drive the UART barcode scanner from the app's session state.

        The serial reads are blocking, so they run in a worker thread
        (asyncio.to_thread) to keep this off the event loop the BLE readers
        share. The module is untouched while the feature is off, opened and put
        in command mode (dark) when it is on, asked to scan continuously while a
        session is active, and left dark otherwise. A missing or broken port
        backs off and retries; it never takes the other loops down.

        Import is local so a host that never enables the scanner does not pay
        for pyserial or the module at all.
        """
        from .scanner_uart import ScannerUnavailable, SerialScanner

        scanner: SerialScanner | None = None
        backoff = _SCANNER_BACKOFF_MIN
        last_code = ""
        last_ts = 0.0
        try:
            while True:
                block = self._scanner_uart
                if not block.get("enabled"):
                    # Feature off: release the port and leave the module alone.
                    if scanner is not None:
                        await asyncio.to_thread(scanner.close)
                        scanner = None
                    await asyncio.sleep(_SCANNER_IDLE_POLL_SECONDS)
                    continue
                # (Re)build the handle if the port or baud changed.
                if (scanner is None or scanner.port != block["port"]
                        or scanner.baud != block["baud"]):
                    if scanner is not None:
                        await asyncio.to_thread(scanner.close)
                    scanner = SerialScanner(block["port"], block["baud"])
                if not scanner.available:
                    opened = await asyncio.to_thread(scanner.open)
                    if not opened:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _SCANNER_BACKOFF_MAX)
                        continue
                    await asyncio.to_thread(scanner.configure)
                    backoff = _SCANNER_BACKOFF_MIN
                if not self._scan_active:
                    # Enabled but no session: keep it dark, send nothing.
                    await asyncio.sleep(_SCANNER_DARK_POLL_SECONDS)
                    continue
                try:
                    codes = await asyncio.to_thread(scanner.scan)
                except ScannerUnavailable as exc:
                    log.warning("UART scanner dropped out: %s", exc)
                    scanner = None
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _SCANNER_BACKOFF_MAX)
                    continue
                for code in codes:
                    now = time.time()
                    # The module re-reads a code that sits in view; drop the
                    # repeats inside the dedupe window before they hit the app.
                    if code == last_code and now - last_ts < _SCANNER_DEDUP_SECONDS:
                        continue
                    last_code, last_ts = code, now
                    log.info("UART scanner read %s (mode %s)", code,
                             self._scanner_mode or "-")
                    await self._post_scan(code)
                await asyncio.sleep(_SCANNER_SCAN_INTERVAL)
        finally:
            if scanner is not None:
                await asyncio.to_thread(scanner.close)

    # -- Scanner (discovery + Combustion advertising decode) ----------------

    def _mark_discovered(self, dev_id: str, protocol: str, name: str,
                         rssi, supported: bool = True,
                         kind: str = "") -> None:
        entry = {
            "id": dev_id, "protocol": protocol, "name": name or "",
            "rssi": rssi, "supported": supported, "ts": time.time(),
        }
        if kind:
            entry["kind"] = kind
        self.discovered[dev_id] = entry

    def _on_advertisement(self, device, adv) -> None:
        try:
            name = adv.local_name or device.name
            # Any advertisement is proof the radio is up; a scan callback fired.
            if not self.adapter_available:
                self.adapter_available = True
                self.adapter_detail = ""
            # Hygrometers (Govee H5075, ATC Xiaomi, SwitchBot Meter, Inkbird
            # IBS-TH) are their own device class: decoded passively from the
            # advertisement and pushed with kind="hygrometer", never mixed
            # into the cooking-probe list.
            service_data = getattr(adv, "service_data", None)
            hygro = decoders.identify_hygrometer(
                name, adv.manufacturer_data, service_data)
            if hygro:
                dev_id = _norm_id(device.address)
                if dev_id in self.hygrometers:
                    decoded = decoders.decode_hygrometer(
                        hygro, adv.manufacturer_data, service_data)
                    if decoded:
                        known = self.hygrometers[dev_id]
                        self.record_hygrometer(
                            dev_id, hygro, known.get("name") or name,
                            decoded, rssi=adv.rssi)
                else:
                    self._mark_discovered(dev_id, hygro, name, adv.rssi,
                                          kind="hygrometer")
                return
            # Door/window contact sensors (BTHome v2 like the Shelly BLU
            # Door/Window, SwitchBot Contact, unencrypted Xiaomi MiBeacon)
            # are another passive class: decoded from the advertisement and
            # pushed with kind="contact" (FoodAssistant-5c61).
            contact = decoders.identify_contact(name, adv.manufacturer_data,
                                                service_data)
            if contact:
                dev_id = _norm_id(device.address)
                if dev_id in self.contacts:
                    decoded = decoders.decode_contact(
                        contact, adv.manufacturer_data, service_data)
                    if decoded:
                        known = self.contacts[dev_id]
                        self.record_contact(
                            dev_id, contact, known.get("name") or name,
                            decoded, rssi=adv.rssi)
                else:
                    self._mark_discovered(dev_id, contact, name, adv.rssi,
                                          kind="contact")
                return
            # Buttons (BTHome v2, unencrypted Xiaomi MiBeacon) are the third
            # passive class: a press is deduped against the radio's repeat
            # burst and POSTed to the app immediately as an event.
            button = decoders.identify_button(name, adv.manufacturer_data,
                                              service_data)
            if button:
                dev_id = _norm_id(device.address)
                decoded = decoders.decode_button(button, service_data) or {}
                if dev_id in self.buttons:
                    known = self.buttons[dev_id]
                    label = known.get("name") or name
                    self.record_button(dev_id, button, label,
                                       decoded.get("battery"), adv.rssi)
                    for ev in decoders.dedupe_button_events(
                            self._button_seen, dev_id, decoded, time.time()):
                        self._post_button_event(
                            dev_id, button, label, ev,
                            decoded.get("counter"), decoded.get("battery"),
                            adv.rssi)
                else:
                    self._mark_discovered(dev_id, button, name, adv.rssi,
                                          kind="button")
                return
            # Other ambient room sensors are not cooking probes; keep them out
            # of the thermometer list entirely.
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
        if self._i2c is not None:
            # The keypad wants a 25ms scan, which an asyncio loop shared with
            # BLE connects cannot promise, so the bus gets one dedicated
            # thread that hands its events back here (see i2c/module.py).
            self._i2c.start(asyncio.get_running_loop())
        try:
            await asyncio.gather(
                self.config_loop(),
                self.push_loop(),
                self.scan_loop(),
                self.connection_supervisor(),
                self.outputs_loop(),
                self.scanner_loop(),
                self._advertiser.run(),
            )
        finally:
            if self._i2c is not None:
                self._i2c.stop()
        return 0


async def main_async(cfg: Config) -> int:
    return await Daemon(cfg).run()
