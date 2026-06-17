import json
import secrets as _secrets
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Single source of truth for the app version (shown in the UI, used by the
# update checker, and reported by FastAPI). Bump on each tagged release.
APP_VERSION = "1.1.0"

# GitHub repo used by the in-app update checker.
GITHUB_REPO = "Syracuse3DPrinting/FoodAssistant"

_SAVEABLE = [
    "vision_provider", "gemini_api_key", "gemini_model",
    "ollama_base_url", "ollama_model",
    "openai_api_key", "openai_model",
    "anthropic_api_key", "anthropic_model",
    "barcode_enrichment", "barcode_llm_fallback", "enrich_provider", "enrich_model",
    "grocy_base_url", "grocy_api_key",
    "mealie_base_url", "mealie_api_key", "mealie_public_url",
    "recipe_source", "themealdb_api_key", "spoonacular_api_key",
    "staple_items", "cook_ai_context", "perishable_days", "expiring_soon_days", "suggest_per_tier",
    "nav_order", "nav_hidden", "custom_storage_categories",
    "secret_key", "auth_password", "totp_secret", "api_key", "auth_required",
    "rclone_remote", "rclone_schedule_hours",
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
    gemini_model: str = "gemini-1.5-flash"

    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "llava:7b"

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"

    # Barcode-scan enrichment: "llm" cleans up name/category/storage/shelf-life
    # via the LLM; "off" uses Open Food Facts heuristics only.
    barcode_enrichment: str = "llm"
    # When a barcode is not found in Open Food Facts, try the LLM to identify
    # the product by barcode/UPC number. Results are low-confidence guesses
    # for rare or regional products. Default off — enable when enrichment is on.
    barcode_llm_fallback: bool = False
    # Which provider enriches scans: gemini | ollama | openai | anthropic, or
    # "" to follow vision_provider. Set to "ollama" (or VISION_PROVIDER=ollama)
    # for a fully local pipeline.
    enrich_provider: str = ""
    # Model override for enrichment; "" uses the chosen provider's model above.
    enrich_model: str = ""

    grocy_base_url: str = _DEFAULT_GROCY_URL
    grocy_api_key: str = ""

    # Mealie recipe manager (optional) — enables the Recipes, Meal Plan and
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

    # User-defined storage categories beyond the four built-ins. Each is a
    # dict {key,label,icon,color,bg,location,match}. See storage_categories.py.
    custom_storage_categories: list = []

    data_dir: str = "/app/data"
    secret_key: str = ""

    # Secure by default: a standalone install must set a password before it is
    # usable (enforced via is_configured). Set AUTH_REQUIRED=false when an
    # outer layer already handles auth — e.g. the HA add-on (Ingress) or a
    # zero-trust proxy like Pangolin — to avoid a redundant second login.
    auth_required: bool = True
    auth_password: str = ""
    totp_secret: str = ""   # base32 secret; empty = TOTP disabled
    api_key: str = ""
    rclone_remote: str = ""          # e.g. "s3:mybucket/foodassistant"
    rclone_schedule_hours: int = 0   # 0 = disabled; 24 = daily

    def provider_key(self, provider: str) -> str:
        """API key for a cloud provider name; '' for local/unknown providers."""
        return {
            "gemini": self.gemini_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
        }.get(provider, "ollama-no-key-needed" if provider == "ollama" else "")

    def is_configured(self) -> bool:
        """True when the minimum required settings have been supplied."""
        if not self.grocy_api_key:
            return False
        if not self.grocy_base_url or self.grocy_base_url == _DEFAULT_GROCY_URL:
            return False
        if not self.provider_key(self.vision_provider):
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
        existing.update({k: v for k, v in data.items() if k in _SAVEABLE and v is not None})
        sf.write_text(json.dumps(existing, indent=2))
        sf.chmod(0o600)  # settings.json holds API keys — owner-only
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
