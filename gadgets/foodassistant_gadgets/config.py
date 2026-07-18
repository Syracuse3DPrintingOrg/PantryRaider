"""Gadget daemon configuration.

Settings come from a TOML file (default ``config.toml`` next to the package,
overridable with ``--config`` or ``FOODASSISTANT_GADGETS_CONFIG``), then the
environment. Everything has a sane default, so a daemon on the same host as
the app works with an empty file: it pulls the device list from the app's
``GET /gadgets/config``, so which thermometers to read is configured in the
web UI, not here.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ENV_CONFIG = "FOODASSISTANT_GADGETS_CONFIG"
ENV_API_KEY = "FOODASSISTANT_API_KEY"
ENV_BASE_URL = "FOODASSISTANT_BASE_URL"


@dataclass
class Config:
    base_url: str = "http://127.0.0.1:9284"
    api_key: str = ""
    # How often readings are pushed to the app (seconds).
    push_seconds: int = 5
    # How often the device list / enabled flag is re-pulled from the app.
    config_poll_seconds: int = 30
    # Whether to keep a passive BLE scan running. The scan is what discovers
    # new thermometers and what reads Combustion probes (their temperatures
    # ride the advertisement, no connection needed).
    scan: bool = True
    # Whether this host may broadcast the Cub status advertisement at all.
    # The actual on/off lives on the server (the cub_ble_advertise setting,
    # pulled with the rest of GET /gadgets/config); this local flag is only
    # a host-level opt-out for a box that should never touch advertising.
    advertise: bool = True
    # Optional static device list, merged with what the app sends. Each entry
    # is a table with "id" (the MAC address, or the serial for a Combustion
    # probe), "protocol" (inkbird / thermopro / combustion / bluedot), and an
    # optional "name". Normally left empty: devices are added in the web UI.
    devices: list = field(default_factory=list)
    # Optional static hygrometer list, same shape and same merge rules as
    # "devices" but for the separate hygrometer class (Govee H5075, ATC
    # Xiaomi, SwitchBot Meter, Inkbird IBS-TH). Normally left empty too:
    # hygrometers are added in the web UI and pulled from the app.
    hygrometers: list = field(default_factory=list)
    # Optional static button list, same shape and merge rules again but for
    # the BLE push-button class (BTHome v2 buttons like the Shelly BLU
    # Button1, unencrypted Xiaomi MiBeacon switches). Normally left empty:
    # buttons are added in the web UI and pulled from the app.
    buttons: list = field(default_factory=list)
    # Optional static door/window contact sensor list, same shape and merge
    # rules again (FoodAssistant-5c61: left-open alarms). Normally left
    # empty: contacts are added in the web UI and pulled from the app.
    contacts: list = field(default_factory=list)
    # Whether this host may use its I2C bus for plug-in STEMMA QT / Qwiic
    # accessories (FoodAssistant-etsc). Like "scan" and "advertise", this is
    # only a host-level opt-out: which accessories to drive is configured in
    # the web UI and pulled with the rest of GET /gadgets/config. A machine
    # with no bus needs no setting at all; the module reports the missing bus
    # and stays quiet.
    i2c: bool = True
    # The I2C bus number, if this host's QT connector is not on the Pi's
    # usual /dev/i2c-1.
    i2c_bus: int = 1

    def validated(self) -> "Config":
        self.base_url = self.base_url.rstrip("/")
        self.push_seconds = max(2, int(self.push_seconds))
        self.config_poll_seconds = max(5, int(self.config_poll_seconds))
        self.devices = [d for d in self.devices if isinstance(d, dict) and d.get("id")]
        self.hygrometers = [d for d in self.hygrometers
                            if isinstance(d, dict) and d.get("id")]
        self.buttons = [d for d in self.buttons
                        if isinstance(d, dict) and d.get("id")]
        self.contacts = [d for d in self.contacts
                         if isinstance(d, dict) and d.get("id")]
        self.i2c_bus = max(0, int(self.i2c_bus))
        return self


def default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config.toml"


def resolved_config_path(path: str | os.PathLike | None = None) -> Path:
    return (
        Path(path)
        if path
        else Path(os.environ[ENV_CONFIG])
        if os.environ.get(ENV_CONFIG)
        else default_config_path()
    )


def load(path: str | os.PathLike | None = None) -> Config:
    """Load configuration: built-in defaults, then the TOML file, then env."""
    cfg = Config()
    resolved = resolved_config_path(path)
    if resolved.exists():
        data = tomllib.loads(resolved.read_text())
        _apply(cfg, data)
    if os.environ.get(ENV_BASE_URL):
        cfg.base_url = os.environ[ENV_BASE_URL]
    if os.environ.get(ENV_API_KEY):
        cfg.api_key = os.environ[ENV_API_KEY]
    return cfg.validated()


def _apply(cfg: Config, data: dict) -> None:
    for name in ("base_url", "api_key"):
        if isinstance(data.get(name), str):
            setattr(cfg, name, data[name])
    for name in ("scan", "advertise", "i2c"):
        if isinstance(data.get(name), bool):
            setattr(cfg, name, data[name])
    for name in ("push_seconds", "config_poll_seconds", "i2c_bus"):
        if isinstance(data.get(name), int) and not isinstance(data.get(name), bool):
            setattr(cfg, name, data[name])
    if isinstance(data.get("devices"), list):
        cfg.devices = [d for d in data["devices"] if isinstance(d, dict)]
    if isinstance(data.get("hygrometers"), list):
        cfg.hygrometers = [d for d in data["hygrometers"] if isinstance(d, dict)]
    if isinstance(data.get("buttons"), list):
        cfg.buttons = [d for d in data["buttons"] if isinstance(d, dict)]
    if isinstance(data.get("contacts"), list):
        cfg.contacts = [d for d in data["contacts"] if isinstance(d, dict)]
