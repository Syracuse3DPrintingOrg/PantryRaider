import json
import secrets as _secrets
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Single source of truth for the app version (shown in the UI, used by the
# update checker, and reported by FastAPI). Bump on each tagged release.
APP_VERSION = "1.5.0"

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
    "deployment_mode", "remote_server_url",
    "secret_key", "auth_password", "totp_secret", "api_key", "auth_required",
    "rclone_remote", "rclone_schedule_hours",
    "tunnel_mode", "tunnel_token", "tunnel_url",
]

# Settings that hold credentials. These are redacted from backups unless the
# user explicitly opts in, and never rendered back into the setup page.
SECRET_SETTING_KEYS = [
    "gemini_api_key", "openai_api_key", "anthropic_api_key",
    "grocy_api_key", "mealie_api_key",
    "themealdb_api_key", "spoonacular_api_key",
    "auth_password", "totp_secret", "api_key", "secret_key",
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

    # Deployment mode chosen in the wizard (one of DEPLOYMENT_MODES). Empty
    # until the user picks one. In "pi_remote" mode this device is only a
    # control surface for a remote server (remote_server_url), so the local
    # Grocy/Mealie requirements in is_configured() do not apply.
    deployment_mode: str = ""
    # For pi_remote: the base URL of the FoodAssistant server this device
    # controls (e.g. http://192.168.1.50:9284). Unused in the other modes.
    remote_server_url: str = ""

    def is_remote_mode(self) -> bool:
        return self.deployment_mode == "pi_remote"

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
        # Pi Remote is a thin control surface: it has no local Grocy, so the
        # only requirement is a reachable remote server URL (plus the usual
        # password gate). The local-stack checks below do not apply.
        if self.is_remote_mode():
            if not self.remote_server_url:
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
            if _k in _SAVEABLE and _v and not getattr(settings, _k, ""):
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
