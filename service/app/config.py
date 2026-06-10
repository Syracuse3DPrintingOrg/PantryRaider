import json
import secrets as _secrets
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

_SAVEABLE = [
    "vision_provider", "gemini_api_key", "gemini_model",
    "ollama_base_url", "ollama_model",
    "openai_api_key", "openai_model",
    "anthropic_api_key", "anthropic_model",
    "barcode_enrichment", "enrich_provider", "enrich_model",
    "grocy_base_url", "grocy_api_key",
    "mealie_base_url", "mealie_api_key", "mealie_public_url",
    "secret_key", "auth_password", "api_key",
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

    data_dir: str = "/app/data"
    secret_key: str = ""

    auth_password: str = ""
    api_key: str = ""

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

# Auto-generate SECRET_KEY on first run so it stays stable across restarts
if not settings.secret_key:
    object.__setattr__(settings, "secret_key", _secrets.token_hex(32))
    settings.save({"secret_key": settings.secret_key})
