"""Controller configuration.

Settings come from a TOML file (default ``config.toml`` next to the package,
overridable with ``--config`` or ``FOODASSISTANT_STREAMDECK_CONFIG``).
Everything has a sane default, so a deck plugged into a fresh appliance works
with an empty file as long as the app is on localhost without auth.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .actions import ACTIONS, DEFAULT_ORDER

ENV_CONFIG = "FOODASSISTANT_STREAMDECK_CONFIG"
ENV_API_KEY = "FOODASSISTANT_API_KEY"
ENV_BASE_URL = "FOODASSISTANT_BASE_URL"

# Brightness steps cycled by the "brightness" key, low to high.
BRIGHTNESS_STEPS: tuple[int, ...] = (20, 40, 60, 80, 100)


@dataclass
class Config:
    base_url: str = "http://localhost:9284"
    api_key: str = ""
    brightness: int = 60
    poll_seconds: int = 30
    soon_days: int = 7
    # Optional Chrome DevTools endpoint of a local kiosk browser, e.g.
    # "http://localhost:9222". When set, nav keys steer that browser.
    kiosk_cdp_url: str = ""
    keys: list[str] = field(default_factory=lambda: list(DEFAULT_ORDER))

    def validated(self) -> "Config":
        """Drop unknown action names and clamp numbers into sane ranges."""
        self.keys = [k for k in self.keys if k in ACTIONS] or list(DEFAULT_ORDER)
        self.brightness = _clamp(self.brightness, 5, 100)
        self.poll_seconds = max(5, int(self.poll_seconds))
        self.soon_days = _clamp(self.soon_days, 0, 365)
        self.base_url = self.base_url.rstrip("/")
        return self


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config.toml"


def load(path: str | os.PathLike | None = None) -> Config:
    """Load configuration, layering file values over defaults then env vars.

    Resolution order, lowest priority first: built-in defaults, the TOML file,
    then the environment (so a systemd unit can inject the API key without
    writing it to disk).
    """
    cfg = Config()

    resolved = (
        Path(path)
        if path
        else Path(os.environ[ENV_CONFIG])
        if os.environ.get(ENV_CONFIG)
        else default_config_path()
    )
    if resolved.exists():
        data = tomllib.loads(resolved.read_text())
        _apply(cfg, data)

    if os.environ.get(ENV_BASE_URL):
        cfg.base_url = os.environ[ENV_BASE_URL]
    if os.environ.get(ENV_API_KEY):
        cfg.api_key = os.environ[ENV_API_KEY]

    return cfg.validated()


def _apply(cfg: Config, data: dict) -> None:
    for name in ("base_url", "api_key", "kiosk_cdp_url"):
        if isinstance(data.get(name), str):
            setattr(cfg, name, data[name])
    for name in ("brightness", "poll_seconds", "soon_days"):
        if isinstance(data.get(name), int):
            setattr(cfg, name, data[name])

    # Keys may be given as a plain list of action names, or as an array of
    # tables each with an "action" field, to match the documented example.
    raw_keys = data.get("keys")
    if isinstance(raw_keys, list):
        names: list[str] = []
        for entry in raw_keys:
            if isinstance(entry, str):
                names.append(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("action"), str):
                names.append(entry["action"])
        if names:
            cfg.keys = names
