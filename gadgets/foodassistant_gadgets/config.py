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
    # Optional static device list, merged with what the app sends. Each entry
    # is a table with "id" (the MAC address, or the serial for a Combustion
    # probe), "protocol" (inkbird / thermopro / combustion / bluedot), and an
    # optional "name". Normally left empty: devices are added in the web UI.
    devices: list = field(default_factory=list)

    def validated(self) -> "Config":
        self.base_url = self.base_url.rstrip("/")
        self.push_seconds = max(2, int(self.push_seconds))
        self.config_poll_seconds = max(5, int(self.config_poll_seconds))
        self.devices = [d for d in self.devices if isinstance(d, dict) and d.get("id")]
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
    for name in ("scan",):
        if isinstance(data.get(name), bool):
            setattr(cfg, name, data[name])
    for name in ("push_seconds", "config_poll_seconds"):
        if isinstance(data.get(name), int) and not isinstance(data.get(name), bool):
            setattr(cfg, name, data[name])
    if isinstance(data.get("devices"), list):
        cfg.devices = [d for d in data["devices"] if isinstance(d, dict)]
