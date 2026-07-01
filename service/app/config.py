import json
import socket
import secrets as _secrets
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# is_raspberry_pi() is lru_cached and reads no files until first called, so a
# module-level import has no import-time cost or side-effects. Importing the
# name here (rather than inside features()) keeps it off the per-render hot path
# and, as in routers.setup, lets tests monkeypatch it.
from .hardware import is_raspberry_pi

# Single source of truth for the app version (shown in the UI, used by the
# update checker, and reported by FastAPI). Bump on each tagged release.
APP_VERSION = "0.7.41"

# GitHub repo used by the in-app update checker.
GITHUB_REPO = "Syracuse3DPrinting/FoodAssistant"

# Amazon Associates tag for the Shop recommendations. This is the project
# owner's static tag (the same on every deployment, since the affiliate revenue
# is the project's), NOT a per-user setting. Set it here once. Empty leaves the
# links working but unmonetized. An AMAZON_ASSOCIATES_TAG env var overrides it.
import os as _os
AMAZON_ASSOCIATES_TAG = _os.environ.get("AMAZON_ASSOCIATES_TAG", "improvisedeng-20").strip()

# Public Amazon storefront (the project's curated "recommended items" page).
# Like the Associates tag this is the project owner's static link, not a
# per-user setting. Empty by default; set AMAZON_STOREFRONT_URL to surface the
# storefront link on the Shop page. No URL is invented here.
AMAZON_STOREFRONT_URL = _os.environ.get("AMAZON_STOREFRONT_URL", "https://www.amazon.com/shop/improvisedeng").strip()

# UI themes. Each entry carries the Bootstrap 5.3 colour mode (data-bs-theme)
# and an optional vendored Bootswatch stylesheet served from /static. When
# "stylesheet" is None the default Bootstrap CSS is used (native light/dark).
# Bootswatch files are vendored locally (no CDN) under static/vendor/themes/.
THEMES = {
    "dark":       {"label": "Dark (default)",      "mode": "dark",  "stylesheet": None, "overlay": None},
    "light":      {"label": "Light",               "mode": "light", "stylesheet": None, "overlay": None},
    "darkly":     {"label": "Darkly (fun, dark)",  "mode": "dark",
                   "stylesheet": "static/vendor/themes/darkly.min.css", "overlay": None},
    "cyborg":     {"label": "Cyborg (fun, dark)",  "mode": "dark",
                   "stylesheet": "static/vendor/themes/cyborg.min.css", "overlay": None},
    "flatly":     {"label": "Flatly (fun, light)", "mode": "light",
                   "stylesheet": "static/vendor/themes/flatly.min.css", "overlay": None},
    "synthwave":  {"label": "Synthwave (neon dark)", "mode": "dark", "stylesheet": None,
                   "overlay": "static/vendor/themes/synthwave.css"},
    "solarized":  {"label": "Solarized (warm light)", "mode": "light", "stylesheet": None,
                   "overlay": "static/vendor/themes/solarized.css"},
    "midnight":   {"label": "Midnight (high-contrast dark)", "mode": "dark", "stylesheet": None,
                   "overlay": "static/vendor/themes/midnight.css"},
    "forest":     {"label": "Forest (soft green dark)", "mode": "dark", "stylesheet": None,
                   "overlay": "static/vendor/themes/forest.css"},
    "ios-light":  {"label": "iOS Light (clean, Apple-like)", "mode": "light", "stylesheet": None,
                   "overlay": "static/vendor/themes/ios-light.css"},
    "ios-dark":   {"label": "iOS Dark (clean, Apple-like)", "mode": "dark", "stylesheet": None,
                   "overlay": "static/vendor/themes/ios-dark.css"},
    "outrun":     {"label": "Outrun (neon dark)", "mode": "dark", "stylesheet": None,
                   "overlay": "static/vendor/themes/outrun.css"},
    "vaporwave":  {"label": "Vaporwave (neon dark)", "mode": "dark", "stylesheet": None,
                   "overlay": "static/vendor/themes/vaporwave.css"},
    # The custom theme has no static stylesheet or overlay file: its colours come
    # from the user-edited swatches stored in Settings (custom_theme_*). base.html
    # emits an inline <style> from those values, layered after base Bootstrap, so
    # it behaves like an overlay built from settings rather than a vendored file.
    # "mode" here is a placeholder; the live mode follows custom_theme_base and is
    # resolved in theme_context (see resolve_theme()).
    "custom":     {"label": "Custom",                 "mode": "dark",  "stylesheet": None, "overlay": None},
}
_DEFAULT_THEME = "dark"

# UI scale presets. The factor is applied as a CSS zoom on the document root so
# the whole interface grows or shrinks uniformly. Lets one build look right on a
# tiny HDMI panel, a countertop touchscreen, or a large monitor without editing
# CSS. Selected in Settings (Interface) and the setup wizard.
UI_SCALES = {
    "small":  {"label": "Small (more fits, smaller text)",      "factor": 0.85},
    "normal": {"label": "Normal",                               "factor": 1.0},
    "large":  {"label": "Large (bigger text and buttons)",      "factor": 1.2},
    "xlarge": {"label": "Extra large (small touchscreens)",     "factor": 1.4},
}
_DEFAULT_UI_SCALE = "normal"

# Orientation of a hardware display attached to the appliance (the Pi's HDMI
# panel). Applied only to the kiosk display, never to a regular browser.
DISPLAY_ROTATIONS = (0, 90, 180, 270)
_DEFAULT_DISPLAY_ROTATION = 0

# Type of hardware display attached to the appliance. The first-boot
# provisioner reads this from settings.json to decide which (if any) panel
# specific boot overlay and touch udev rules to install:
#   generic         - a plain HDMI display (or USB HID touch monitor); no panel
#                     specific overlay is applied. This is the default and
#                     covers most setups.
#   waveshare_hdmi  - a Waveshare HDMI touchscreen Pi HAT. These need a panel
#                     dtoverlay in config.txt plus a touch udev rule so the
#                     controller is recognised as an input device.
#   dsi_7inch       - a MIPI DSI 7-inch panel (official Raspberry Pi 7-inch
#                     touchscreen and 800x480 driver-free clones like the
#                     Hosyond). Bookworm full KMS dropped DSI auto-detection, so
#                     firstboot writes dtoverlay=vc4-kms-dsi-7inch to light it.
#   ads7846_hdmi    - a resistive HDMI panel with an ADS7846 SPI touch
#                     controller (Waveshare 3.5-4 inch HDMI LCD and similar).
#                     The touch is invisible until SPI and the ads7846 overlay
#                     are configured, and auto-detect cannot see it because SPI
#                     is off at first boot, so firstboot writes both.
DISPLAY_TYPES = {
    "generic":        {"label": "Generic display"},
    "waveshare_hdmi": {"label": "Waveshare HDMI touchscreen (USB touch)"},
    "dsi_7inch":      {"label": "MIPI DSI 7-inch touchscreen (official / Hosyond clone)"},
    "ads7846_hdmi":   {"label": "Resistive HDMI touchscreen (ADS7846 SPI, e.g. Waveshare 3.5-4 inch)"},
}
_DEFAULT_DISPLAY_TYPE = "generic"

# Placement for the on-screen navigation bar (FoodAssistant-bzuu, -i181).
# "off" hides it; the others dock a FIXED bar to that screen edge, reserving
# layout space so it never overlaps content. A per-device choice overrides this
# default via localStorage. Legacy corner values from the older draggable
# floating menu are still accepted and mapped to an edge by the client.
FLOATING_NAV_POSITIONS = ("off", "bottom", "left", "right")
_LEGACY_NAV_POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right")
FLOATING_NAV_ORIENTATIONS = ("vertical", "horizontal")

# Deployment modes chosen on the first wizard step. They steer the rest of
# setup and (on a Pi) what the first-boot provisioner installs:
#   server     - FoodAssistant on a general server; connect to separately
#                running Grocy/Mealie. The only non-Pi mode.
#   pi_hosted  - everything runs on this Pi (FoodAssistant + Grocy + Mealie),
#                with or without an attached display (kiosk auto-enables when a
#                display is present, and a display can be added later).
#   pi_remote  - thin client: this device only drives a Stream Deck and/or
#                kiosk pointed at an existing FoodAssistant server on the LAN.
#                No local Docker/Grocy/Mealie; runs on low-spec hardware.
DEPLOYMENT_MODES = {
    "server":    {"label": "Server hosted", "pi": False, "local_stack": True,
                  "remote": False},
    "pi_hosted": {"label": "Pi Hosted",     "pi": True,  "local_stack": True,
                  "remote": False},
    "pi_remote": {"label": "Pi Remote",     "pi": True,  "local_stack": False,
                  "remote": True},
}
_DEFAULT_DEPLOYMENT_MODE = "server"


# Curated, vision-capable model suggestions per provider, newest first. The
# setup UI offers these in a dropdown with the note as guidance and always
# keeps a free-text override for a model not listed here. Update as providers
# ship new models; the override means an outdated list never blocks anyone.
AI_MODELS = {
    "gemini": [
        {"id": "gemini-2.5-flash",       "note": "Fast, low cost, multimodal. Best default for photos and barcodes."},
        {"id": "gemini-2.5-pro",         "note": "Highest accuracy, slower and pricier. For tricky receipts."},
        {"id": "gemini-2.5-flash-lite",  "note": "Cheapest and fastest; fine for simple labels."},
        {"id": "gemini-2.0-flash",       "note": "Previous generation, still capable."},
    ],
    "openai": [
        {"id": "gpt-4o-mini", "note": "Fast, cheap, multimodal. Good default."},
        {"id": "gpt-4o",      "note": "Higher accuracy multimodal; costs more."},
        {"id": "gpt-4.1-mini","note": "Strong cost/quality balance."},
        {"id": "gpt-4.1",     "note": "Most capable for hard images."},
    ],
    "anthropic": [
        {"id": "claude-haiku-4-5-20251001", "note": "Fast, cheap, vision-capable. Good default."},
        {"id": "claude-sonnet-4-6",         "note": "Balanced accuracy and cost."},
        {"id": "claude-opus-4-8",           "note": "Most capable; for difficult receipts."},
    ],
    "ollama": [
        {"id": "llava:7b",              "note": "Lightweight local vision. Low RAM."},
        {"id": "llava:13b",             "note": "Better accuracy, needs more RAM."},
        {"id": "llama3.2-vision:11b",   "note": "Newer local vision model."},
        {"id": "moondream",             "note": "Tiny and fast; lower accuracy."},
    ],
}


def theme_info(name: str) -> dict:
    """Resolve a theme name to its descriptor, falling back to the default.

    For the "custom" theme the returned ``mode`` follows the stored
    ``custom_theme_base`` ("light"/"dark") so data-bs-theme matches the chosen
    base, rather than the placeholder mode in the THEMES table.
    """
    if name == "custom" or (isinstance(name, str) and name.startswith("custom:")):
        # A custom theme (the live editor "custom", or a saved "custom:<id>")
        # has no vendored stylesheet/overlay; its colours come from swatches.
        colors = resolve_custom_colors(name)
        base = colors.get("base", "dark") if colors else "dark"
        return {"label": "Custom", "mode": base if base in ("light", "dark") else "dark",
                "stylesheet": None, "overlay": None}
    return dict(THEMES.get(name, THEMES[_DEFAULT_THEME]))


# -- Named custom themes (FoodAssistant-nw49) -------------------------------

_CUSTOM_COLOR_KEYS = ("primary", "accent", "bg", "surface", "text")


def _custom_themes_list() -> list[dict]:
    raw = getattr(settings, "custom_themes", []) or []
    return [t for t in raw if isinstance(t, dict) and t.get("id")]


def custom_theme_by_id(theme_id: str) -> dict | None:
    """Return the saved custom theme with this id, or None."""
    for t in _custom_themes_list():
        if t.get("id") == theme_id:
            return t
    return None


def resolve_custom_colors(name: str | None = None) -> dict | None:
    """Colours (base + five swatches) for the active custom theme.

    For ``ui_theme == "custom"`` this is the live swatch buffer; for
    ``"custom:<id>"`` it is the named saved theme's stored colours. Returns
    None when ``name`` is not a custom theme, so callers can branch cheaply.
    """
    if name is None:
        name = getattr(settings, "ui_theme", "")
    if name == "custom":
        return {
            "base":    getattr(settings, "custom_theme_base", "dark"),
            "primary": settings.custom_theme_primary,
            "accent":  settings.custom_theme_accent,
            "bg":      settings.custom_theme_bg,
            "surface": settings.custom_theme_surface,
            "text":    settings.custom_theme_text,
        }
    if isinstance(name, str) and name.startswith("custom:"):
        saved = custom_theme_by_id(name.split(":", 1)[1])
        if saved:
            return {
                "base":    saved.get("base", "dark"),
                "primary": saved.get("primary", settings.custom_theme_primary),
                "accent":  saved.get("accent", settings.custom_theme_accent),
                "bg":      saved.get("bg", settings.custom_theme_bg),
                "surface": saved.get("surface", settings.custom_theme_surface),
                "text":    saved.get("text", settings.custom_theme_text),
            }
    return None


def active_custom_name() -> str:
    """Display name of the active saved custom theme, or '' when none/legacy."""
    name = getattr(settings, "ui_theme", "")
    if isinstance(name, str) and name.startswith("custom:"):
        saved = custom_theme_by_id(name.split(":", 1)[1])
        if saved:
            return str(saved.get("name", ""))
    return ""


def ui_scale_factor(name: str) -> float:
    """Resolve a UI scale name to its zoom factor, falling back to the default."""
    return UI_SCALES.get(name, UI_SCALES[_DEFAULT_UI_SCALE])["factor"]

_SAVEABLE = [
    "vision_provider", "gemini_api_key", "gemini_model",
    "ollama_base_url", "ollama_model",
    "openai_api_key", "openai_model",
    "anthropic_api_key", "anthropic_model",
    "ai_extra_keys",
    "scanner_type", "barcode_global_capture", "extra_api_key_names",
    "barcode_enrichment", "barcode_llm_fallback", "barcode_autocheck_shopping", "enrich_provider", "enrich_model",
    "grocy_base_url", "grocy_api_key", "grocy_public_url",
    "mealie_base_url", "mealie_api_key", "mealie_public_url",
    "device_hostname",
    "recipe_source", "themealdb_api_key", "spoonacular_api_key",
    "staple_items", "cook_ai_context", "kitchen_appliances",
    "perishable_days", "expiring_soon_days", "suggest_per_tier",
    "nav_order", "nav_hidden", "custom_nav_tabs", "nav_parents",
    "custom_storage_categories", "ui_theme",
    "custom_theme_base", "custom_theme_primary", "custom_theme_accent",
    "custom_theme_bg", "custom_theme_surface", "custom_theme_text",
    "custom_themes",
    "background_image_url", "background_opacity",
    "ui_scale", "display_rotation",
    "display_type",
    "has_streamdeck", "streamdeck_key_count", "display_touch",
    "start_page_enabled", "start_page_keys", "start_page_layout",
    "display_idle_timeout", "streamdeck_idle_timeout", "streamdeck_key_overrides",
    "streamdeck_weather_location", "streamdeck_weather_units", "weather_api_base",
    "streamdeck_key_style", "streamdeck_icon_color",
    "streamdeck_cameras",
    "streamdeck_ha_base_url", "streamdeck_ha_token", "streamdeck_ha_slots",
    "ha_events_enabled", "ha_camera_popup_seconds", "convert_custom_rows",
    "quiet_mode",
    "floating_nav_position", "floating_nav_orientation", "floating_nav_autohide_streamdeck",
    "deployment_mode", "remote_server_url", "remote_server_ip", "remote_server_host", "upstream_api_key", "kiosk_pin", "kiosk_readonly_when_locked",
    "satellite_sync_minutes", "satellite_last_sync", "device_id",
    "secret_key", "auth_password", "totp_secret", "api_key", "extra_api_keys", "auth_required",
    "rclone_remote", "rclone_schedule_hours",
    "tunnel_mode", "tunnel_token", "tunnel_url",
    "debug_logging", "auto_update", "lan_scan_cidr",
]

# Settings a satellite (pi_remote) pulls from its main server and mirrors
# locally so it can talk to Grocy/Mealie/AI directly. These are READ-ONLY on a
# satellite: edit them on the server. Device-local concerns (auth, UI theme,
# hardware, tunnel, the upstream link itself) are deliberately excluded.
SATELLITE_PULL_FIELDS = [
    "vision_provider", "gemini_api_key", "gemini_model",
    "ollama_base_url", "ollama_model",
    "openai_api_key", "openai_model",
    "anthropic_api_key", "anthropic_model",
    "barcode_enrichment", "barcode_llm_fallback", "barcode_autocheck_shopping",
    "enrich_provider", "enrich_model",
    "grocy_base_url", "grocy_api_key", "grocy_public_url",
    "mealie_base_url", "mealie_api_key", "mealie_public_url",
    "recipe_source", "themealdb_api_key", "spoonacular_api_key",
    "staple_items", "cook_ai_context", "kitchen_appliances",
    "perishable_days", "expiring_soon_days", "suggest_per_tier",
    "custom_storage_categories", "ui_theme",
    # Stream Deck weather widget config, so a satellite's deck matches the
    # server's location/units without separate local setup (FoodAssistant-bra).
    "streamdeck_weather_location", "streamdeck_weather_units",
    # NOTE: streamdeck_key_style and streamdeck_icon_color are deliberately NOT
    # pulled. They are a per-deck visual choice owned by the device the deck is
    # attached to, so a satellite can pick its own key style / icon mode (e.g.
    # the full-colour emoji set) and have it stick instead of being overwritten
    # by the server on the next sync (FoodAssistant-ys79).
    # Custom Stream Deck keys (timers, quick-adds, HA actions, macros, ...), so a
    # custom button built on the main server appears on every satellite's deck,
    # not just the device it was made on (FoodAssistant-n0r1).
    "streamdeck_key_overrides",
    # Cameras, so a satellite's kiosk page and deck mirror the server's feeds.
    "streamdeck_cameras",
    # Home Assistant credentials + key map, so a satellite's deck drives the same
    # HA entities and can build camera URLs without re-entering the token.
    "streamdeck_ha_base_url", "streamdeck_ha_token", "streamdeck_ha_slots",
    # On-screen HA event channel, so a satellite kiosk behaves like the server
    # (events are still pushed per-instance by HA). convert_custom_rows is left
    # device-local on purpose: each kiosk keeps its own conversion cheat sheet.
    "ha_events_enabled", "ha_camera_popup_seconds",
    # Fleet-wide auto-update flag, so a satellite obeys the main server's choice
    # and the whole fleet updates (or holds) together (FoodAssistant-k2kk).
    "auto_update",
]

# Kitchen appliances the user owns, used to steer the AI cook suggestions so it
# only proposes dishes the kitchen can actually make, and (later) to drive
# affiliate product recommendations (FoodAssistant-k2kv). Each entry is
# (key, label, group, default_on). "group" is "major" or "minor" purely for how
# the checklist is laid out; "default_on" pre-checks the everyday items so a new
# install is useful before the user touches anything. Keys are stable ids stored
# in settings.kitchen_appliances; labels can be reworded without a migration.
KITCHEN_APPLIANCES = [
    # Major: the big, common, mostly-assumed kitchen workhorses.
    ("stove", "Stovetop", "major", True),
    ("oven", "Oven", "major", True),
    ("microwave", "Microwave", "major", True),
    ("refrigerator", "Refrigerator", "major", True),
    ("freezer", "Freezer", "major", True),
    ("toaster", "Toaster", "major", True),
    ("blender", "Blender", "major", True),
    ("dishwasher", "Dishwasher", "major", False),
    ("toaster_oven", "Toaster oven", "major", False),
    ("air_fryer", "Air fryer", "major", False),
    ("slow_cooker", "Slow cooker", "major", False),
    ("pressure_cooker", "Pressure cooker / Instant Pot", "major", False),
    ("rice_cooker", "Rice cooker", "major", False),
    ("stand_mixer", "Stand mixer", "major", False),
    ("food_processor", "Food processor", "major", False),
    ("sous_vide", "Sous vide", "major", False),
    ("deep_fryer", "Deep fryer", "major", False),
    ("grill", "Grill / BBQ", "major", False),
    ("smoker", "Smoker", "major", False),
    ("bread_machine", "Bread machine", "major", False),
    ("dehydrator", "Dehydrator", "major", False),
    ("ice_cream_maker", "Ice cream maker", "major", False),
    # Minor: smaller tools and specialty cookware.
    ("immersion_blender", "Immersion blender", "minor", False),
    ("hand_mixer", "Hand mixer", "minor", False),
    ("cast_iron", "Cast iron skillet", "minor", False),
    ("wok", "Wok", "minor", False),
    ("dutch_oven", "Dutch oven", "minor", False),
    ("griddle", "Griddle", "minor", False),
    ("waffle_iron", "Waffle iron", "minor", False),
    ("panini_press", "Panini press", "minor", False),
    ("bun_steamer", "Bun / food steamer", "minor", False),
    ("pizza_stone", "Pizza stone", "minor", False),
    ("mandoline", "Mandoline", "minor", False),
    ("spiralizer", "Spiralizer", "minor", False),
    ("kitchen_scale", "Kitchen scale", "minor", False),
    ("meat_thermometer", "Meat thermometer", "minor", False),
    ("mortar_pestle", "Mortar and pestle", "minor", False),
    ("microplane", "Microplane / zester", "minor", False),
    ("garlic_press", "Garlic press", "minor", False),
    ("rolling_pin", "Rolling pin", "minor", False),
    ("pastry_bag", "Piping / pastry bag", "minor", False),
    ("torch", "Kitchen torch", "minor", False),
    ("pasta_roller", "Pasta roller", "minor", False),
    ("pasta_extruder", "Pasta extruder", "minor", False),
    # Stand-mixer attachments: the common KitchenAid-style add-ons. Grouped on
    # their own so the checklist can label them clearly; each is only useful if
    # you own a compatible stand mixer.
    ("sm_pasta_roller_cutter", "Pasta roller and cutter set", "attachment", False),
    ("sm_meat_grinder", "Meat grinder", "attachment", False),
    ("sm_spiralizer", "Spiralizer attachment", "attachment", False),
    ("sm_food_processor", "Food processor attachment", "attachment", False),
    ("sm_ice_cream_maker", "Ice cream maker attachment", "attachment", False),
    ("sm_grain_mill", "Grain mill", "attachment", False),
    ("sm_sausage_stuffer", "Sausage stuffer", "attachment", False),
]

# Stable id -> label, and the default-on id list, derived once from the catalog.
KITCHEN_APPLIANCE_LABELS = {key: label for key, label, _g, _d in KITCHEN_APPLIANCES}
KITCHEN_APPLIANCE_KEYS = tuple(key for key, *_rest in KITCHEN_APPLIANCES)
DEFAULT_KITCHEN_APPLIANCES = [key for key, _l, _g, default_on in KITCHEN_APPLIANCES if default_on]


def appliances_clause(keys) -> str:
    """A short sentence listing the owned appliances for the AI cook prompt.

    Returns "" when nothing is selected (so the prompt is left unconstrained).
    Unknown ids are dropped, so a stale saved key never leaks into the prompt.
    Pure, so it is unit-testable without settings.
    """
    labels = [KITCHEN_APPLIANCE_LABELS[k] for k in (keys or [])
              if k in KITCHEN_APPLIANCE_LABELS]
    if not labels:
        return ""
    return (
        "Available kitchen appliances: " + ", ".join(labels)
        + ". Prefer recipes that can be made with this equipment; do not require "
        "appliances that are not listed."
    )


# Stream Deck key rendering style (FoodAssistant-fygv). Pushed into the deck's
# config.toml so the controller picks them up. "rich" is a subtle gradient,
# "glass" a glassmorphism panel, "minimal" the flat legacy fill. icon_color
# "full" tints glyphs with the action accent; "mono" keeps them monochrome.
STREAMDECK_KEY_STYLES = ("rich", "minimal", "glass", "clean")
STREAMDECK_ICON_COLORS = ("full", "mono", "color")

# Settings that hold credentials. These are redacted from backups unless the
# user explicitly opts in, and never rendered back into the setup page.
SECRET_SETTING_KEYS = [
    "gemini_api_key", "openai_api_key", "anthropic_api_key", "ai_extra_keys",
    "grocy_api_key", "mealie_api_key",
    "themealdb_api_key", "spoonacular_api_key",
    "auth_password", "totp_secret", "api_key", "extra_api_keys", "secret_key", "kiosk_pin",
    "streamdeck_ha_token",
]

_DEFAULT_GROCY_URL = "http://grocy:80"

_LOCALHOST_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
# Docker-compose service hostnames for the local stack (pi_hosted / server).
# These resolve only inside the compose network, so a browser-facing link
# pointing at one (e.g. http://grocy) is never reachable from a phone or PC.
# Treat them like localhost when rewriting browser links (FoodAssistant-r9r7):
# the API wiring still uses the raw base_url, only the browser link is rewritten
# to the LAN host:port.
_INTERNAL_SERVICE_HOSTS = {"grocy", "mealie", "ollama"}

# The Pi host bridge (see routers/setup.py) reports the real host hostname. On a
# pi_hosted appliance the app runs in a host-network container whose own
# socket.gethostname() can be the container name (e.g. "foodassistant-service")
# rather than the Pi's LAN hostname, so <that>.local would not resolve. Asking
# the bridge gives the device's actual hostname, whatever the user named it.
_HOST_BRIDGE_URL = "http://127.0.0.1:9299"


def _bridge_hostname() -> str:
    """Real host hostname from the Pi host bridge, or '' if unavailable.

    Only consulted on a Raspberry Pi appliance (where the bridge runs on
    127.0.0.1:9299 and answers instantly); skipped elsewhere so a missing bridge
    never adds latency to a page render or connection test. Best-effort with a
    short timeout regardless.
    """
    try:
        from .hardware import is_raspberry_pi
        if not is_raspberry_pi():
            return ""
    except Exception:
        return ""
    try:
        import httpx
        r = httpx.get(f"{_HOST_BRIDGE_URL}/hostname", timeout=1.5)
        if r.status_code == 200:
            name = (r.json() or {}).get("hostname") or ""
            return name.strip()
    except Exception:
        pass
    return ""


def _lan_ip() -> str:
    """This host's outbound LAN address, or '' if it cannot be determined.

    Used as a fallback browser host when no stable hostname is available. It is
    the current address (it can change when DHCP reassigns), so it is only a last
    resort behind the mDNS hostname.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return ""


def device_hostname() -> str:
    """The device's own hostname for building browser-facing links.

    Resolution order, most stable first:
      1. a user-set override (settings.device_hostname), trimmed of any scheme;
      2. the real host hostname reported by the Pi host bridge;
      3. socket.gethostname() (the process view; correct off-appliance).
    Never returns a localhost alias. May return '' if nothing usable is found.
    """
    override = (getattr(settings, "device_hostname", "") or "").strip()
    if override:
        # Accept a bare name or a full host; keep only the hostname portion.
        from urllib.parse import urlparse
        if "://" in override:
            override = urlparse(override).hostname or override
        return override.rstrip("/")
    bridge = _bridge_hostname()
    if bridge and bridge.lower() not in _LOCALHOST_HOSTS:
        return bridge
    name = socket.gethostname().strip()
    if name and name.lower() not in _LOCALHOST_HOSTS:
        return name
    return ""


def browser_host() -> str:
    """Best stable hostname (no port) for LAN browser links.

    Prefers <hostname>.local (stable across DHCP) and falls back to the current
    LAN IP only when no hostname is resolvable. Returns '' if neither is found.
    """
    name = device_hostname()
    if name:
        # Avoid doubling the suffix if the user already entered one.
        if "." in name:
            return name
        return f"{name}.local"
    return _lan_ip()


def _mdns_rewrite(url: str, port: int) -> str:
    """If url points to localhost, rewrite it to a LAN-reachable browser URL.

    This makes browser-facing links work from other devices on the LAN without
    requiring a static IP. It prefers the current LAN IP, because that works even
    on networks where mDNS (<hostname>.local) does not resolve (FoodAssistant-pmcu,
    FoodAssistant-wjua), and falls back to the mDNS hostname when the IP cannot be
    determined. These links are regenerated on every page render, so a DHCP IP
    change is picked up the next time the page loads. If no host can be resolved
    the original URL is returned unchanged. The loopback address is kept for the
    behind-the-scenes API wiring (grocy_base_url / mealie_base_url); only the
    browser-facing link is rewritten.
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.hostname in _LOCALHOST_HOSTS or parsed.hostname in _INTERNAL_SERVICE_HOSTS:
        host = _lan_ip() or browser_host()
        if host:
            return f"http://{host}:{port}"
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Vision provider: gemini | ollama | openai | anthropic
    vision_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "llava:7b"

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"

    # Additional API keys per cloud provider, beyond the primary key stored in
    # <provider>_api_key above. Maps provider name -> ordered list of extra
    # keys, e.g. {"gemini": ["AIza...second", "AIza...third"]}. The primary key
    # is always tried first; the extras give the app spare keys to fall back to
    # when one is rate-limited or revoked. Set in the setup wizard.
    ai_extra_keys: dict = {}

    # How barcodes are scanned: "usb" = USB/BT HID keyboard-wedge, "camera" =
    # Pi camera / scan engine, "" = not set (user picks on Add Food page).
    scanner_type: str = ""

    # When True, a keyboard-wedge barcode scanned on ANY page is captured,
    # saved to the pending list, and the browser jumps to the Add Food page.
    # When False, wedge capture only happens on the Add Food page itself.
    barcode_global_capture: bool = True

    # Barcode-scan enrichment: "llm" cleans up name/category/storage/shelf-life
    # via the LLM; "off" uses Open Food Facts heuristics only.
    barcode_enrichment: str = "llm"
    # When a barcode is not found in Open Food Facts, try the LLM to identify
    # the product by barcode/UPC number. Results are low-confidence guesses
    # for rare or regional products. Default off: enable when enrichment is on.
    barcode_llm_fallback: bool = False
    # When an item is committed to Grocy and Mealie is configured, auto-check
    # any matching unchecked Mealie shopping-list items (token-matched by name).
    barcode_autocheck_shopping: bool = False
    # Which provider enriches scans: gemini | ollama | openai | anthropic, or
    # "" to follow vision_provider. Set to "ollama" (or VISION_PROVIDER=ollama)
    # for a fully local pipeline.
    enrich_provider: str = ""
    # Model override for enrichment; "" uses the chosen provider's model above.
    enrich_model: str = ""

    grocy_base_url: str = _DEFAULT_GROCY_URL
    grocy_api_key: str = ""
    # Browser-facing Grocy URL (reverse-proxy / public address). Empty = use base URL.
    grocy_public_url: str = ""

    # Optional override for the device's own hostname, used to build browser
    # links to locally-hosted backends (Grocy, Mealie) as <hostname>.local. Empty
    # means auto-detect (host bridge, then socket.gethostname()). Lets a user pin
    # a stable name when there are several appliances on one LAN, so the link
    # never depends on the device being named "foodassistant".
    device_hostname: str = ""

    def _server_host_url(self, port: int) -> str:
        """A LAN browser URL for a backend that lives on the main server.

        On a satellite, Grocy and Mealie run on the main server, not on this
        device, so browser links must point at the server's host (taken from
        remote_server_url) on the backend's port, never this device's own mDNS
        hostname. Returns '' if the server host cannot be determined.
        """
        from urllib.parse import urlparse
        host = urlparse((self.remote_server_url or "").rstrip("/")).hostname
        return f"http://{host}:{port}" if host else ""

    def grocy_link_url(self) -> str:
        """URL for browser-facing Grocy links (public address if set, else base).

        When no public URL is set and the base URL is localhost, rewrites to the
        device hostname (<hostname>.local, or the LAN IP as a fallback) so links
        work from other devices on the LAN regardless of the current IP. On a
        satellite, Grocy runs on the main server, so links resolve to the
        server's host instead of this device.
        """
        if self.grocy_public_url:
            return self.grocy_public_url.rstrip("/")
        if self.is_satellite():
            url = self._server_host_url(9383)
            if url:
                return url
        return _mdns_rewrite(self.grocy_base_url.rstrip("/"), 9383)

    # Mealie recipe manager (optional): enables the Recipes, Meal Plan and
    # Shopping List pages. base_url is for API calls (LAN/docker address);
    # public_url is only used for browser links and falls back to base_url.
    mealie_base_url: str = ""
    mealie_api_key: str = ""
    mealie_public_url: str = ""

    def mealie_configured(self) -> bool:
        return bool(self.mealie_base_url and self.mealie_api_key)

    def mealie_link_url(self) -> str:
        """URL for browser-facing links (public address if set, else base).

        When no public URL is set and the base URL is localhost, rewrites to the
        mDNS hostname so links work from other devices on the LAN. On a
        satellite, Mealie runs on the main server, so links resolve to the
        server's host instead of this device (which is why an "Open Mealie"
        button used to point at this device's own foodassistant.local).
        """
        if self.mealie_public_url:
            return self.mealie_public_url.rstrip("/")
        if self.is_satellite():
            url = self._server_host_url(9285)
            if url:
                return url
        return _mdns_rewrite(self.mealie_base_url.rstrip("/"), 9285)

    # External recipe suggestions: themealdb | spoonacular | off.
    # TheMealDB's public test key "1" is free; a premium (supporter) key or
    # a Spoonacular key unlocks bigger catalogs.
    recipe_source: str = "themealdb"
    themealdb_api_key: str = "1"
    spoonacular_api_key: str = ""

    # Suggestion tuning. staple_items: comma-separated pantry items assumed
    # on hand (empty = built-in list). Thresholds in days.
    staple_items: str = ""
    cook_ai_context: str = ""
    # Kitchen appliances the user owns (list of stable ids from
    # KITCHEN_APPLIANCES). Steers the AI cook suggestions and, later, affiliate
    # recommendations. Defaults to the everyday items so a fresh install is
    # useful before the user edits anything; an explicit empty list (user
    # unchecked everything) is preserved distinctly from the unset default.
    kitchen_appliances: list = DEFAULT_KITCHEN_APPLIANCES
    # Amazon Associates tag (the user's own Associate ID), used to monetize the
    # Shop page's product links (FoodAssistant-k2kv). Empty means links are still
    # built and useful, just not tagged. Shared to satellites so the whole fleet
    # uses one tag (see SATELLITE_PULL_FIELDS).
    perishable_days: int = 14
    expiring_soon_days: int = 5
    suggest_per_tier: int = 8

    # Navigation: comma-separated tab keys. nav_order sets display order
    # (unlisted tabs follow in default order); nav_hidden hides tabs.
    nav_order: str = ""
    nav_hidden: str = ""

    # Custom navigation tabs (FoodAssistant-9gdz). User-created top-level entries
    # or submenu children, each a dict {id, label, icon, url, parent}. "id" is a
    # stable key (auto-prefixed "custom_"), "url" is a root-relative href or a
    # full external link, "parent" is the key of the tab this nests under (empty
    # for a top-level tab). See navigation.py for validation and tree building.
    custom_nav_tabs: list = []

    # Parent assignments for nesting EXISTING (built-in) tabs under another tab,
    # as a child key -> parent key map. A built-in tab listed here renders inside
    # its parent's dropdown instead of as a top-level link. Custom tabs carry
    # their own "parent" field instead, so this map only covers built-ins.
    nav_parents: dict = {}

    # UI colour theme. One of the keys in THEMES (dark | light | bootswatch | custom).
    ui_theme: str = _DEFAULT_THEME

    # Custom theme builder (FoodAssistant-hatd). When ui_theme == "custom" the
    # app emits an inline stylesheet from these swatches instead of loading a
    # vendored theme file. custom_theme_base picks the Bootstrap light/dark base
    # the overrides layer onto (so data-bs-theme matches). The five colours map
    # onto the most visible Bootstrap variables: primary (buttons/links/active),
    # accent (secondary accent), bg (page background), surface (cards/inputs/
    # tertiary chrome) and text (body text). A small curated set, not full
    # freeform, so any combination stays cohesive. Defaults are a tasteful slate
    # dark palette that mirrors the stock dark theme.
    custom_theme_base: str = "dark"
    custom_theme_primary: str = "#4f9dff"
    custom_theme_accent: str = "#34d399"
    custom_theme_bg: str = "#0d1117"
    custom_theme_surface: str = "#161b22"
    custom_theme_text: str = "#e6edf3"

    # Saved, named custom themes (FoodAssistant-nw49). Each entry is a dict:
    # {"id", "name", "base", "primary", "accent", "bg", "surface", "text"}. A
    # theme is selected by setting ui_theme to "custom:<id>"; the live swatches
    # above remain the working/editor buffer and back the legacy bare "custom"
    # theme. resolve_custom_colors() picks the right colour set for the active
    # ui_theme so templates do not need to branch.
    custom_themes: list = []

    # Background image (FoodAssistant-e2t6). An optional image painted behind the
    # whole UI on a fixed layer. background_image_url is either an external URL
    # or the internal serve route "setup/background/image?v=<hash>" set when a
    # file is uploaded (the file lives at data_dir/background.<ext>).
    # background_opacity (0-100) is the image layer's opacity; lower keeps text
    # readable. Empty url = no background image (the theme colour shows as before).
    background_image_url: str = ""
    background_opacity: int = 40

    # UI scale. One of the keys in UI_SCALES; applied as a document zoom on the
    # kiosk display only, so the interface fits a small or large hardware panel
    # without changing what other browsers see.
    ui_scale: str = _DEFAULT_UI_SCALE

    # Rotation (degrees) of the attached hardware display. One of
    # DISPLAY_ROTATIONS; applied to the kiosk display only.
    display_rotation: int = _DEFAULT_DISPLAY_ROTATION

    # Type of the attached hardware display. One of DISPLAY_TYPES. The first-boot
    # provisioner reads this to install panel specific boot overlays and touch
    # udev rules (e.g. a Waveshare HDMI touchscreen HAT). "generic" applies none.
    display_type: str = _DEFAULT_DISPLAY_TYPE

    # Hardware declared in the wizard (Pi modes only). has_streamdeck enables
    # the controller setup hints; streamdeck_key_count is 6, 15, or 32.
    # display_touch flags a touch-compatible kiosk screen for future UI hints.
    has_streamdeck: bool = False
    streamdeck_key_count: int = 0
    display_touch: bool = False

    # On-screen Start Page (FoodAssistant): an optional full-screen launcher that
    # works like an on-screen Stream Deck. start_page_enabled turns it on (off by
    # default); start_page_keys is the grid size (6, 15, or 32); start_page_layout
    # is an ordered list of slot tokens, each a built-in action key, a
    # "custom:<id>" reference into streamdeck_key_overrides (shared with the
    # physical deck), or "" for a blank key. The keys scale to fill the screen.
    start_page_enabled: bool = False
    start_page_keys: int = 15
    start_page_layout: list = []

    # Idle timeouts (minutes). 0 = disabled. display_idle_timeout puts the
    # kiosk display to sleep after N minutes without user interaction.
    # streamdeck_idle_timeout blanks the Stream Deck after N minutes without
    # a key press.
    display_idle_timeout: int = 0
    streamdeck_idle_timeout: int = 0

    # On-screen floating navigation menu (FoodAssistant-bzuu). position is the
    # server default ("off" hides it; otherwise a corner: top-left, top-right,
    # bottom-left, bottom-right). A drag on the device overrides it per-device
    # via localStorage. orientation is "vertical" (a column) or "horizontal" (a
    # row) and is also overridable per-device (FoodAssistant-76mw), since a tall
    # phone and a wide wall display want different shapes.
    # floating_nav_autohide_streamdeck hides it when a Stream Deck is connected,
    # since the deck already provides navigation.
    floating_nav_position: str = "off"
    floating_nav_orientation: str = "vertical"
    floating_nav_autohide_streamdeck: bool = False

    # Stream Deck weather widget. Held at the app level (not just in the
    # controller's config.toml) so a satellite can pull them from the main
    # server via the satellite config sync (FoodAssistant-bra). location is a
    # city, zip, or "lat,lon" (empty = auto-detect from device IP); units is
    # "f" or "c". Mirrored into config.toml when the deck config is written.
    streamdeck_weather_location: str = ""
    streamdeck_weather_units: str = "f"
    # Weather data server (FoodAssistant). The base URL of an Open-Meteo API,
    # default the public instance. Advanced users can point this at a self-hosted
    # Open-Meteo. The "/v1/forecast" path is appended by the weather service.
    # Used by both the on-screen Weather page and the Stream Deck weather key.
    weather_api_base: str = "https://api.open-meteo.com"

    # Stream Deck key visual style, pushed into the deck's config.toml
    # (FoodAssistant-fygv). key_style: rich | minimal | glass. icon_color:
    # full (accent-tinted glyphs) | mono (monochrome).
    streamdeck_key_style: str = "rich"
    streamdeck_icon_color: str = "full"

    # Cameras shown on the kiosk camera page and pushed to the Stream Deck
    # (FoodAssistant-oewn). A list of dicts, each {name, stream_url,
    # snapshot_url}: stream_url is the live feed for the kiosk page (an .m3u8 HLS
    # stream or an MJPEG URL), snapshot_url is a still image the deck can show.
    # Held at the app level so a satellite mirrors the server's cameras via the
    # satellite config sync (see SATELLITE_PULL_FIELDS).
    streamdeck_cameras: list = []

    # Home Assistant connection, shared by the Stream Deck HA keys and the camera
    # discovery helper (FoodAssistant-cr50). Held at the app level (not just the
    # deck's config.toml) so it can be set from the server or a Pi, has one source
    # of truth, and is pulled by a satellite so a second Pi remote inherits the
    # credentials without re-entering them. ha_base_url is the HA instance URL;
    # ha_token is a long-lived access token (HA profile, Security). ha_slots maps
    # the five deck keys ha_1..ha_5 to entities (each {entity_id, service, label}).
    streamdeck_ha_base_url: str = ""
    streamdeck_ha_token: str = ""
    streamdeck_ha_slots: list = []

    # On-screen Home Assistant event channel (FoodAssistant-*): when enabled, the
    # kiosk and web UI poll for events pushed from HA (a rest_command in an
    # automation) and show notification toasts and camera pop-ups on the display.
    # ha_camera_popup_seconds is the default time a popped-up camera stays up.
    ha_events_enabled: bool = False
    ha_camera_popup_seconds: int = 20

    # Quiet mode (FoodAssistant-soj1): silence the audible timer chime so alerts
    # are visual only (the timer window still highlights a finished timer).
    # Device-local on purpose, so each kiosk decides its own noise.
    quiet_mode: bool = False

    # User-defined reference rows shown on the Conversions page, each a dict
    # {label, value} (for example {"label": "Stick of butter", "value": "113 g /
    # 8 tbsp"}). Pure reference text alongside the built-in cheat sheet.
    convert_custom_rows: list = []

    # Advanced Stream Deck per-key overrides set in the setup page. A JSON list
    # where each entry is a dict with "slot" (grid index), "type" (ha_action |
    # timer | weather | default) and type-specific fields (entity_id/service,
    # minutes, location, label, icon). Mirrored into the controller's config.toml
    # as "key_overrides" so the deck applies them on top of the default layout.
    streamdeck_key_overrides: list = []

    # Deployment mode chosen in the wizard (one of DEPLOYMENT_MODES). Empty
    # until the user picks one. "pi_remote" is a SATELLITE: it runs the full
    # app but installs no local Grocy/Mealie stack. It pulls all backend config
    # (Grocy/Mealie/AI keys and the expiry defaults) from a main server and then
    # talks to those backends directly. See SATELLITE_PULL_FIELDS.
    deployment_mode: str = ""
    # Satellite only: base URL of the main FoodAssistant server to pull config
    # from (e.g. http://192.168.1.50:9284), plus the API key used to authenticate
    # that pull. Unused in the other modes.
    remote_server_url: str = ""
    upstream_api_key: str = ""
    # Satellite only: last known-good LAN IP of the main server, cached on each
    # successful sync. Used as a fallback when the configured .local hostname
    # stops resolving (mDNS down on the network), so the satellite stays wired
    # to its server (FoodAssistant-xwn0). Written only when the IP changes.
    remote_server_ip: str = ""
    # Satellite only: the main server's hostname, learned from the sync response.
    # Lets a satellite configured with a bare IP fall back to <host>.local when
    # DHCP reassigns the server's IP, so it reconnects without a manual edit
    # (FoodAssistant-k9a8). Written only when it changes.
    remote_server_host: str = ""
    # Satellite only: an optional numeric PIN that gates the kiosk UI. A
    # satellite turns the UI password off by default (the main server owns
    # access control), so this is a lightweight, touchscreen-friendly lock for
    # the local screen. Empty means no PIN gate.
    kiosk_pin: str = ""
    # When True and the kiosk is PIN-locked, allow unauthenticated users to
    # browse read-only (GET requests pass through without a PIN). POST/PUT/
    # PATCH/DELETE from unauthenticated users are rejected with 403.
    kiosk_readonly_when_locked: bool = False

    def pin_lock_active(self) -> bool:
        """True when the numeric kiosk PIN should gate the UI (satellite only)."""
        return self.is_satellite() and bool(self.kiosk_pin)

    # Satellite only: how often to re-pull backend config from the main server,
    # in minutes. 0 disables the periodic refresh (boot + manual sync only).
    satellite_sync_minutes: int = 15

    # Satellite only: result of the most recent pull from the main server, used
    # to show sync health in the setup page. Keys: "at" (ISO-8601 UTC string),
    # "ok" (bool), "applied" (list of field names), "defaults" (int),
    # "error" (str or None). Empty dict means no sync has run yet.
    satellite_last_sync: dict = {}

    # Stable per-device identifier. Auto-generated on first run and persisted so
    # a satellite presents the same identity across reboots and IP changes, and
    # so the main server can track it as one device in its remotes list.
    device_id: str = ""

    def is_remote_mode(self) -> bool:
        return self.deployment_mode == "pi_remote"

    # Clearer name for the same thing: pi_remote == a satellite of a main server.
    def is_satellite(self) -> bool:
        return self.deployment_mode == "pi_remote"

    def is_pi_appliance(self) -> bool:
        """True for the Pi appliance modes (Pi Hosted or Pi Remote), which run
        the host bridge and so support the in-app OTA update. Mode based, not
        hardware detected, so it is stable in tests and headless contexts."""
        return self.deployment_mode in ("pi_hosted", "pi_remote")

    def manages_local_stack(self) -> bool:
        """True when this device runs/controls its own Grocy/Mealie Docker
        stack (server, pi_hosted). A satellite points at a remote stack."""
        return not self.is_satellite()

    def features(self) -> "dict[str, bool]":
        """Which capability groups are active for this deployment_mode.

        Templates and routers use these flags to show or hide sections.
        is_raspberry_pi() is lru_cached (one /proc read for the process life)
        and degrades to False off-Pi, so this stays a cheap dict build even on
        the per-render hot path.
        """
        is_pi = is_raspberry_pi()
        satellite = self.is_satellite()
        return {
            # manages_stack: this device runs local Grocy/Mealie Docker, so it
            # shows the "start/stop local stack" controls. A satellite does not.
            "manages_stack": not satellite,
            # satellite: pulls backend config from a main server; backend config
            # panes are shown read-only and the upstream link pane is shown.
            "satellite": satellite,
            # peripherals: kiosk display + Stream Deck panes (Pi only)
            "peripherals": is_pi,
            # streamdeck: Stream Deck pane visible (Pi + has a deck declared)
            "streamdeck": is_pi and bool(self.has_streamdeck),
            # ai: vision/LLM provider config (always available)
            "ai": True,
        }

    # User-defined storage categories beyond the four built-ins. Each is a
    # dict {key,label,icon,color,bg,location,match}. See storage_categories.py.
    custom_storage_categories: list = []

    data_dir: str = "/app/data"
    secret_key: str = ""

    # Secure by default: a standalone install must set a password before it is
    # usable (enforced via is_configured). Set AUTH_REQUIRED=false when an
    # outer layer already handles auth: e.g. the HA add-on (Ingress) or a
    # zero-trust proxy like Pangolin: to avoid a redundant second login.
    auth_required: bool = True
    auth_password: str = ""
    totp_secret: str = ""   # base32 secret; empty = TOTP disabled
    api_key: str = ""
    extra_api_keys: list[str] = []   # additional keys; each satellite can use its own
    # Optional human labels for the extra keys above, aligned by index (so
    # extra_api_key_names[2] names extra_api_keys[2]). Lets the admin tell keys
    # apart (e.g. "kitchen pi", "pantry scanner"). Missing/short = unnamed.
    extra_api_key_names: list[str] = []
    rclone_remote: str = ""          # e.g. "s3:mybucket/foodassistant"
    rclone_schedule_hours: int = 0   # 0 = disabled; 24 = daily

    # Verbose logging to a rotating file under data_dir/logs for support bundles
    # (FoodAssistant-asra). Off by default; raises the app log level to DEBUG.
    debug_logging: bool = False

    # Fleet-wide automatic updates (FoodAssistant-k2kk). On by default. This is a
    # GLOBAL flag: it is pulled by satellites (see SATELLITE_PULL_FIELDS) so a
    # main server and its remotes share one setting and converge on the same
    # version. A Pi appliance auto-applies via the host-bridge OTA; a non-Pi
    # server applies via the Watchtower container in docker-compose.prod.yml.
    auto_update: bool = True

    # Remembered LAN range for the satellite-discovery scan, so a containerized
    # server (which only sees its Docker subnet) does not have to be told the
    # range every time. Set automatically the first time a real LAN range is
    # scanned or a satellite checks in (FoodAssistant).
    lan_scan_cidr: str = ""

    # Remote access tunnel. tunnel_mode: "" | "cloudflare" | "subscription"
    tunnel_mode: str = ""
    tunnel_token: str = ""
    tunnel_url: str = ""

    def provider_key(self, provider: str) -> str:
        """Primary API key for a cloud provider; '' for local/unknown providers."""
        return {
            "gemini": self.gemini_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
        }.get(provider, "ollama-no-key-needed" if provider == "ollama" else "")

    def provider_keys(self, provider: str) -> list[str]:
        """Ordered list of usable API keys for a provider: the primary key
        first, then any extras from ai_extra_keys. Blanks and duplicates are
        dropped. Returns [] for providers with no key (e.g. an unset cloud
        provider); ollama returns its sentinel so callers can treat it like
        any other provider.
        """
        keys: list[str] = []
        for k in [self.provider_key(provider), *self._extra_keys(provider)]:
            k = (k or "").strip()
            if k and k not in keys:
                keys.append(k)
        return keys

    def _extra_keys(self, provider: str) -> list[str]:
        raw = self.ai_extra_keys.get(provider, []) if isinstance(self.ai_extra_keys, dict) else []
        return [k for k in raw if isinstance(k, str)]

    def valid_api_keys(self) -> list[str]:
        """All currently accepted satellite/headless-client API keys.

        The primary api_key is listed first for backward compatibility.
        Extra keys let each satellite use its own key so one can be rotated
        or removed without touching the others.
        """
        keys: list[str] = []
        if self.api_key:
            keys.append(self.api_key)
        for k in (self.extra_api_keys if isinstance(self.extra_api_keys, list) else []):
            if k and k not in keys:
                keys.append(k)
        return keys

    def ai_configured(self) -> bool:
        """True when a vision provider key is present and usable."""
        return bool(self.provider_key(self.vision_provider))

    def is_configured(self) -> bool:
        """True when the minimum required settings have been supplied."""
        # A satellite pulls its backend config from a main server, so the only
        # things it must be given are that server's URL and an API key to
        # authenticate the pull. Grocy/Mealie/AI then arrive via that sync.
        if self.is_satellite():
            if not self.remote_server_url or not self.upstream_api_key:
                return False
            if self.auth_required and not self.auth_password:
                return False
            return True
        if not self.grocy_api_key:
            return False
        if not self.grocy_base_url or self.grocy_base_url == _DEFAULT_GROCY_URL:
            return False
        # Secure by default: refuse to be usable without a password unless the
        # operator has explicitly delegated auth to an outer layer.
        if self.auth_required and not self.auth_password:
            return False
        return True

    def apply(self, data: dict) -> None:
        for k, v in data.items():
            if k in _SAVEABLE and hasattr(self, k) and v is not None:
                object.__setattr__(self, k, v)

    def save(self, data: dict) -> None:
        """Merge data into settings.json and apply values to the live object.

        Hardened against data loss (FoodAssistant): if the existing settings file
        is present but cannot be read or parsed, it is preserved aside as
        settings.json.corrupt.<n> rather than silently overwritten with only the
        new fields, and the write is atomic (temp file plus rename) so an
        interrupted write (for example a container restart mid-save) can never
        truncate the live file and lose every other setting.
        """
        import logging as _logging
        sf = Path(self.data_dir) / "settings.json"
        sf.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        good_raw = None  # the last valid on-disk content, kept for a rollback copy
        if sf.exists():
            raw = None
            try:
                raw = sf.read_text()
            except Exception as exc:
                raw = None
                _logging.getLogger("foodassistant.config").error(
                    "settings.json could not be read (%s); preserving it before saving", exc)
            if raw is not None and raw.strip():
                try:
                    existing = json.loads(raw)
                    good_raw = raw
                except Exception as exc:
                    _logging.getLogger("foodassistant.config").error(
                        "settings.json is corrupt (%s); preserving it as a backup", exc)
                    raw = None  # force the preserve-aside path below
            if raw is None and sf.exists():
                # Unreadable or corrupt and non-empty: move it aside so the data is
                # recoverable instead of being clobbered by this partial save.
                for n in range(1, 1000):
                    bak = sf.with_name(f"settings.json.corrupt.{n}")
                    if not bak.exists():
                        try:
                            sf.replace(bak)
                        except Exception:
                            pass
                        break
        # Reject an unknown theme rather than persisting a broken value. A
        # "custom:<id>" value is valid when that named theme exists, either in
        # this same save (data["custom_themes"]) or already on disk.
        if data.get("ui_theme") is not None and data["ui_theme"] not in THEMES:
            ut = data["ui_theme"]
            ok = False
            if isinstance(ut, str) and ut.startswith("custom:"):
                tid = ut.split(":", 1)[1]
                pools = []
                if isinstance(data.get("custom_themes"), list):
                    pools.append(data["custom_themes"])
                if isinstance(existing.get("custom_themes"), list):
                    pools.append(existing["custom_themes"])
                ok = any(isinstance(t, dict) and t.get("id") == tid
                         for pool in pools for t in pool)
            if not ok:
                data["ui_theme"] = _DEFAULT_THEME
        # Hash the login password and kiosk PIN at rest (FoodAssistant-ufwz) so a
        # leaked settings.json or backup never exposes the secret. A value that
        # is already hashed (a re-save) is left alone to avoid double hashing.
        from .passwords import hash_secret, looks_hashed
        for _sk in ("auth_password", "kiosk_pin"):
            if data.get(_sk) and not looks_hashed(data[_sk]):
                data[_sk] = hash_secret(data[_sk])
        existing.update({k: v for k, v in data.items() if k in _SAVEABLE and v is not None})
        # Keep a one-step rollback copy of the last good settings, so even a logic
        # bug that blanks a field leaves yesterday's values recoverable from
        # settings.json.bak. Best effort: never let it block the save.
        if good_raw is not None:
            try:
                bak = sf.with_name("settings.json.bak")
                bak.write_text(good_raw)
                bak.chmod(0o600)
            except Exception:
                pass
        # Atomic write: write a temp file in the same dir, then rename over the
        # target so a crash mid-write leaves the old file intact.
        tmp = sf.with_name("settings.json.tmp")
        tmp.write_text(json.dumps(existing, indent=2))
        tmp.chmod(0o600)  # settings.json holds API keys: owner-only
        import os as _os
        _os.replace(tmp, sf)
        self.apply(existing)


settings = Settings()

# Overlay: fill any empty fields from data/settings.json (env vars always win)
_sf = Path(settings.data_dir) / "settings.json"
if _sf.exists():
    try:
        _saved = json.loads(_sf.read_text())
        for _k, _v in _saved.items():
            if _k in _SAVEABLE and _k not in settings.model_fields_set:
                object.__setattr__(settings, _k, _v)
        # Self-heal the satellite link fields. The systemd unit may pass these as
        # env vars, and an EMPTY env value (e.g. REMOTE_SERVER_URL= when the URL
        # was entered later in the web wizard) counts as "set" and would shadow
        # the saved value, bouncing the device back to setup on every reboot. A
        # non-empty env var still wins; we only fill a blank live value here.
        for _k in ("remote_server_url", "upstream_api_key"):
            if not getattr(settings, _k, "") and _saved.get(_k):
                object.__setattr__(settings, _k, _saved[_k])
    except Exception as _exc:
        # A corrupt settings.json must be loud, not silent: a swallowed read here
        # previously let the app come up looking unconfigured and then overwrite
        # the file on the next save. save() now preserves a corrupt file aside.
        import logging as _logging
        _logging.getLogger("foodassistant.config").error(
            "Could not load settings.json at startup (%s); it will be preserved on the next save", _exc)

# Auto-generate SECRET_KEY on first run so it stays stable across restarts.
# Persisting is best-effort: if data_dir is not writable (CI, tests, or an
# import before the volume is mounted) keep the in-memory key for this process
# rather than crashing on import.
if not settings.secret_key:
    object.__setattr__(settings, "secret_key", _secrets.token_hex(32))
    try:
        settings.save({"secret_key": settings.secret_key})
    except OSError:
        pass

# Auto-generate a stable device id on first run (used by satellite heartbeat and
# the server's remotes list). Short hex is plenty: it only needs to be unique
# among a household's devices, not unguessable.
if not settings.device_id:
    object.__setattr__(settings, "device_id", _secrets.token_hex(8))
    try:
        settings.save({"device_id": settings.device_id})
    except OSError:
        pass
