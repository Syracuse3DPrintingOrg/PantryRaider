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

# The only rotations we support, in degrees clockwise.
ALLOWED_ROTATIONS: tuple[int, ...] = (0, 90, 180, 270)

# Where the deck physically sits relative to the kiosk panel for the shared
# screensaver canvas (FoodAssistant-3fdq). "off" keeps the deck out of the
# screensaver; the rest name the side of the panel the deck is mounted on.
ALLOWED_SCREENSAVER_LAYOUTS: tuple[str, ...] = ("off", "above", "below", "left", "right")

# Key face rendering style. "rich" (the default) draws a subtle vertical
# gradient with an inner border; "glass" draws a glassmorphism panel; "minimal"
# keeps the old flat fill; "clean" draws no coloured background (a dark face with
# a faint accent border) so a full-colour icon stands out.
ALLOWED_KEY_STYLES: tuple[str, ...] = ("rich", "minimal", "glass", "clean")
DEFAULT_KEY_STYLE = "rich"

# Glyph colouring. "full" tints the monochrome glyph in the action's vivid
# accent; "mono" keeps the luminance-adapted text colour; "color" composites a
# bundled full-colour icon (assets/emoji), best paired with the "clean" style.
ALLOWED_ICON_COLORS: tuple[str, ...] = ("full", "mono", "color")
DEFAULT_ICON_COLOR = "full"


@dataclass
class Config:
    base_url: str = "http://127.0.0.1:9284"
    api_key: str = ""
    brightness: int = 60
    poll_seconds: int = 30
    soon_days: int = 7
    # Optional Chrome DevTools endpoint of a local kiosk browser, e.g.
    # "http://localhost:9222". When set, nav keys steer that browser.
    kiosk_cdp_url: str = "http://127.0.0.1:9222"
    # Clockwise rotation of the rendered key faces, in degrees. Only the four
    # values in ALLOWED_ROTATIONS are accepted; anything else falls back to 0.
    rotation: int = 0
    keys: list[str] = field(default_factory=lambda: list(DEFAULT_ORDER))
    # Active web UI theme name (FoodAssistant-gxl). Recolours the key faces to
    # match the app theme; empty or "dark" keeps the default per-action colours.
    # Stamped into config.toml by the app, so the deck follows the server theme.
    theme: str = "dark"
    # Key face rendering style and glyph colouring. Defaults make the richer,
    # less washed-out look land immediately, even before the app-side toggle
    # that pushes these into config.toml is wired up. See ALLOWED_KEY_STYLES /
    # ALLOWED_ICON_COLORS; validated() clamps an unknown value to the default.
    key_style: str = DEFAULT_KEY_STYLE
    icon_color: str = DEFAULT_ICON_COLOR
    # Weather widget. Uses wttr.in (no API key needed).
    # location: city name, zip, or "lat,lon". Empty = auto-detect from device IP.
    # units: "f" (Fahrenheit) or "c" (Celsius).
    # weather_poll_minutes: background refresh cadence (default 15).
    weather_location: str = ""
    weather_units: str = "f"
    weather_poll_minutes: int = 15
    # Home Assistant entity keys. ha_base_url is the HA instance URL;
    # ha_token is a long-lived access token (Profile > Security in HA).
    # ha_slots is an array of tables in TOML:
    #   [[ha_slots]]
    #   entity_id = "light.kitchen"
    #   service = "light.toggle"
    #   label = "Kitchen"
    #   color_on = "#f59e0b"   # optional, default green
    #   color_off = "#475569"  # optional, default gray
    # Slots map to keys ha_1..ha_5 in order.
    ha_base_url: str = ""
    ha_token: str = ""
    ha_slots: list = field(default_factory=list)
    # How often to refresh HA entity states (seconds). 0 = only on press.
    ha_poll_seconds: int = 30
    # Idle timeout in minutes. 0 = disabled. After this many minutes without
    # a key press the deck is blanked; any key press wakes it.
    idle_timeout_minutes: int = 0
    # Host bridge URL (Pi appliance only). The deck reports key presses here so
    # the kiosk display wakes too, and polls it so a screen touch wakes the deck
    # (shared activity, separate timeouts -- FoodAssistant-otiy). Empty disables.
    host_bridge_url: str = "http://127.0.0.1:9299"
    # Path of the shared auth token the bridge writes at startup
    # (FoodAssistant-pxcm). The deck runs on the same host as the bridge, so
    # the well-known default works on both a pi_hosted and a pi_remote
    # install; override here only for a non-standard INSTALL_DIR.
    bridge_token_path: str = "/opt/foodassistant/data/bridge-token"
    # Advanced per-key overrides configured in the web setup page. Each entry is
    # a dict with "slot" (grid index), "type" (ha_action | timer | weather |
    # default) and type-specific fields. Overrides are applied on top of the
    # default "keys" layout, replacing the action at the given slot. See
    # actions.overrides_to_specs.
    key_overrides: list = field(default_factory=list)
    # Camera snapshot sources pushed by the app. Each entry is a dict with
    # "name" and "snapshot_url" (a still-frame JPEG endpoint). The first entry
    # feeds the single-key camera face and the full-deck overlay. Stamped into
    # config.toml by the app's camera setup page (a parallel task owns that side).
    cameras: list = field(default_factory=list)
    # How often the full-deck camera overlay refreshes its frame, in seconds.
    # Clamped to at least 1 so a stray 0 cannot spin the refresh loop.
    camera_full_refresh_seconds: int = 5
    # Screensaver canvas position (FoodAssistant-3fdq): where this deck sits
    # relative to the kiosk panel. When not "off" and the kiosk screensaver's
    # bouncing logo is up, the controller polls the app for the logo position
    # and renders the slice crossing the deck instead of blanking. Stamped into
    # config.toml by the app from the Stream Deck settings page.
    screensaver_layout: str = "off"

    def validated(self) -> "Config":
        """Drop unknown action names and clamp numbers into sane ranges."""
        self.keys = [k for k in self.keys if k in ACTIONS or k == "blank"] or list(DEFAULT_ORDER)
        self.brightness = _clamp(self.brightness, 5, 100)
        self.poll_seconds = max(5, int(self.poll_seconds))
        self.soon_days = _clamp(self.soon_days, 0, 365)
        self.base_url = self.base_url.rstrip("/")
        if self.rotation not in ALLOWED_ROTATIONS:
            self.rotation = 0
        self.ha_base_url = self.ha_base_url.rstrip("/")
        self.ha_poll_seconds = max(0, int(self.ha_poll_seconds))
        self.idle_timeout_minutes = max(0, int(self.idle_timeout_minutes))
        if self.key_style not in ALLOWED_KEY_STYLES:
            self.key_style = DEFAULT_KEY_STYLE
        if self.icon_color not in ALLOWED_ICON_COLORS:
            self.icon_color = DEFAULT_ICON_COLOR
        self.camera_full_refresh_seconds = max(1, int(self.camera_full_refresh_seconds))
        if self.screensaver_layout not in ALLOWED_SCREENSAVER_LAYOUTS:
            self.screensaver_layout = "off"
        return self


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config.toml"


def resolved_config_path(path: str | os.PathLike | None = None) -> Path:
    """The TOML path load() would read for the given (optional) override.

    Lets a caller watch the same file load() used, so a controller can detect
    a setup-page rewrite and re-init the deck for the new rotation.
    """
    return (
        Path(path)
        if path
        else Path(os.environ[ENV_CONFIG])
        if os.environ.get(ENV_CONFIG)
        else default_config_path()
    )


def load(path: str | os.PathLike | None = None) -> Config:
    """Load configuration, layering file values over defaults then env vars.

    Resolution order, lowest priority first: built-in defaults, the TOML file,
    then the environment (so a systemd unit can inject the API key without
    writing it to disk).
    """
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
    for name in ("base_url", "api_key", "kiosk_cdp_url", "weather_location", "weather_units",
                 "theme", "ha_base_url", "ha_token", "host_bridge_url",
                 "bridge_token_path",
                 "key_style", "icon_color", "screensaver_layout"):
        if isinstance(data.get(name), str):
            setattr(cfg, name, data[name])
    for name in ("brightness", "poll_seconds", "soon_days", "rotation",
                 "weather_poll_minutes", "ha_poll_seconds", "idle_timeout_minutes",
                 "camera_full_refresh_seconds"):
        if isinstance(data.get(name), int):
            setattr(cfg, name, data[name])

    raw_cameras = data.get("cameras")
    if isinstance(raw_cameras, list):
        cfg.cameras = [c for c in raw_cameras if isinstance(c, dict)]

    raw_slots = data.get("ha_slots")
    if isinstance(raw_slots, list):
        cfg.ha_slots = [s for s in raw_slots if isinstance(s, dict)]

    raw_overrides = data.get("key_overrides")
    if isinstance(raw_overrides, list):
        cfg.key_overrides = [o for o in raw_overrides if isinstance(o, dict)]

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
