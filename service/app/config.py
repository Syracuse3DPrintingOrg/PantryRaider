import json
import secrets as _secrets
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Single source of truth for the app version (shown in the UI, used by the
# update checker, and reported by FastAPI). Bump on each tagged release.
APP_VERSION = "1.6.0"

# GitHub repo used by the in-app update checker.
GITHUB_REPO = "Syracuse3DPrinting/FoodAssistant"

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
}
_DEFAULT_THEME = "dark"

# UI scale presets. The factor is applied as a CSS zoom on the document root so
# the whole interface grows or shrinks uniformly. Lets one build look right on a
# tiny HDMI panel, a countertop touchscreen, or a large monitor without editing
# CSS. Selected in Settings (Interface) and the setup wizard.
UI_SCALES = {
    "small":  {"label": "Small",       "factor": 0.85},
    "normal": {"label": "Normal",      "factor": 1.0},
    "large":  {"label": "Large",       "factor": 1.2},
    "xlarge": {"label": "Extra large", "factor": 1.4},
}
_DEFAULT_UI_SCALE = "normal"

# Orientation of a hardware display attached to the appliance (the Pi's HDMI
# panel). Applied only to the kiosk display, never to a regular browser.
DISPLAY_ROTATIONS = (0, 90, 180, 270)
_DEFAULT_DISPLAY_ROTATION = 0

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


def theme_info(name: str) -> dict:
    """Resolve a theme name to its descriptor, falling back to the default."""
    return THEMES.get(name, THEMES[_DEFAULT_THEME])


def ui_scale_factor(name: str) -> float:
    """Resolve a UI scale name to its zoom factor, falling back to the default."""
    return UI_SCALES.get(name, UI_SCALES[_DEFAULT_UI_SCALE])["factor"]

_SAVEABLE = [
    "vision_provider", "gemini_api_key", "gemini_model",
    "ollama_base_url", "ollama_model",
    "openai_api_key", "openai_model",
    "anthropic_api_key", "anthropic_model",
    "scanner_type",
    "barcode_enrichment", "barcode_llm_fallback", "barcode_autocheck_shopping", "enrich_provider", "enrich_model",
    "grocy_base_url", "grocy_api_key", "grocy_public_url",
    "mealie_base_url", "mealie_api_key", "mealie_public_url",
    "recipe_source", "themealdb_api_key", "spoonacular_api_key",
    "staple_items", "cook_ai_context", "perishable_days", "expiring_soon_days", "suggest_per_tier",
    "nav_order", "nav_hidden", "custom_storage_categories", "ui_theme", "ui_scale", "display_rotation",
    "has_streamdeck", "streamdeck_key_count", "display_touch",
    "deployment_mode", "remote_server_url", "upstream_api_key", "kiosk_pin",
    "satellite_sync_minutes", "device_id",
    "secret_key", "auth_password", "totp_secret", "api_key", "auth_required",
    "rclone_remote", "rclone_schedule_hours",
    "tunnel_mode", "tunnel_token", "tunnel_url",
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
    "staple_items", "cook_ai_context",
    "perishable_days", "expiring_soon_days", "suggest_per_tier",
    "custom_storage_categories",
]

# Settings that hold credentials. These are redacted from backups unless the
# user explicitly opts in, and never rendered back into the setup page.
SECRET_SETTING_KEYS = [
    "gemini_api_key", "openai_api_key", "anthropic_api_key",
    "grocy_api_key", "mealie_api_key",
    "themealdb_api_key", "spoonacular_api_key",
    "auth_password", "totp_secret", "api_key", "secret_key", "kiosk_pin",
]

_DEFAULT_GROCY_URL = "http://grocy:80"


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

    # How barcodes are scanned: "usb" = USB/BT HID keyboard-wedge, "camera" =
    # Pi camera / scan engine, "" = not set (user picks on Add Food page).
    scanner_type: str = ""

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

    def grocy_link_url(self) -> str:
        """URL for browser-facing Grocy links (public address if set, else base)."""
        return (self.grocy_public_url or self.grocy_base_url).rstrip("/")

    # Mealie recipe manager (optional): enables the Recipes, Meal Plan and
    # Shopping List pages. base_url is for API calls (LAN/docker address);
    # public_url is only used for browser links and falls back to base_url.
    mealie_base_url: str = ""
    mealie_api_key: str = ""
    mealie_public_url: str = ""

    def mealie_configured(self) -> bool:
        return bool(self.mealie_base_url and self.mealie_api_key)

    def mealie_link_url(self) -> str:
        """URL for browser-facing links (public address if set, else base)."""
        return (self.mealie_public_url or self.mealie_base_url).rstrip("/")

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
    perishable_days: int = 14
    expiring_soon_days: int = 5
    suggest_per_tier: int = 8

    # Navigation: comma-separated tab keys. nav_order sets display order
    # (unlisted tabs follow in default order); nav_hidden hides tabs.
    nav_order: str = ""
    nav_hidden: str = ""

    # UI colour theme. One of the keys in THEMES (dark | light | bootswatch).
    ui_theme: str = _DEFAULT_THEME

    # UI scale. One of the keys in UI_SCALES; applied as a document zoom on the
    # kiosk display only, so the interface fits a small or large hardware panel
    # without changing what other browsers see.
    ui_scale: str = _DEFAULT_UI_SCALE

    # Rotation (degrees) of the attached hardware display. One of
    # DISPLAY_ROTATIONS; applied to the kiosk display only.
    display_rotation: int = _DEFAULT_DISPLAY_ROTATION

    # Hardware declared in the wizard (Pi modes only). has_streamdeck enables
    # the controller setup hints; streamdeck_key_count is 6, 15, or 32.
    # display_touch flags a touch-compatible kiosk screen for future UI hints.
    has_streamdeck: bool = False
    streamdeck_key_count: int = 0
    display_touch: bool = False

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
    # Satellite only: an optional numeric PIN that gates the kiosk UI. A
    # satellite turns the UI password off by default (the main server owns
    # access control), so this is a lightweight, touchscreen-friendly lock for
    # the local screen. Empty means no PIN gate.
    kiosk_pin: str = ""

    def pin_lock_active(self) -> bool:
        """True when the numeric kiosk PIN should gate the UI (satellite only)."""
        return self.is_satellite() and bool(self.kiosk_pin)

    # Satellite only: how often to re-pull backend config from the main server,
    # in minutes. 0 disables the periodic refresh (boot + manual sync only).
    satellite_sync_minutes: int = 15

    # Stable per-device identifier. Auto-generated on first run and persisted so
    # a satellite presents the same identity across reboots and IP changes, and
    # so the main server can track it as one device in its remotes list.
    device_id: str = ""

    def is_remote_mode(self) -> bool:
        return self.deployment_mode == "pi_remote"

    # Clearer name for the same thing: pi_remote == a satellite of a main server.
    def is_satellite(self) -> bool:
        return self.deployment_mode == "pi_remote"

    def manages_local_stack(self) -> bool:
        """True when this device runs/controls its own Grocy/Mealie Docker
        stack (server, pi_hosted). A satellite points at a remote stack."""
        return not self.is_satellite()

    def features(self) -> "dict[str, bool]":
        """Which capability groups are active for this deployment_mode.

        Templates and routers use these flags to show or hide sections.
        The method does NOT import at module level (hardware detection reads
        /proc/device-tree, which is unavailable in tests and CI).
        """
        from .hardware import is_raspberry_pi  # deferred to avoid import-time side-effects
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
    rclone_remote: str = ""          # e.g. "s3:mybucket/foodassistant"
    rclone_schedule_hours: int = 0   # 0 = disabled; 24 = daily

    # Remote access tunnel. tunnel_mode: "" | "cloudflare" | "subscription"
    tunnel_mode: str = ""
    tunnel_token: str = ""
    tunnel_url: str = ""

    def provider_key(self, provider: str) -> str:
        """API key for a cloud provider name; '' for local/unknown providers."""
        return {
            "gemini": self.gemini_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
        }.get(provider, "ollama-no-key-needed" if provider == "ollama" else "")

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
        """Merge data into settings.json and apply values to the live object."""
        sf = Path(self.data_dir) / "settings.json"
        sf.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if sf.exists():
            try:
                existing = json.loads(sf.read_text())
            except Exception:
                pass
        # Reject an unknown theme rather than persisting a broken value.
        if data.get("ui_theme") is not None and data["ui_theme"] not in THEMES:
            data["ui_theme"] = _DEFAULT_THEME
        existing.update({k: v for k, v in data.items() if k in _SAVEABLE and v is not None})
        sf.write_text(json.dumps(existing, indent=2))
        sf.chmod(0o600)  # settings.json holds API keys: owner-only
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
    except Exception:
        pass

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
