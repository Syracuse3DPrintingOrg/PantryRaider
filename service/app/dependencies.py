from functools import lru_cache
from .config import settings
from .providers.base import VisionProvider


@lru_cache(maxsize=1)
def get_vision_provider() -> VisionProvider:
    if settings.vision_provider == "ollama":
        from .providers.ollama import OllamaProvider
        return OllamaProvider(settings.ollama_base_url, settings.ollama_model)

    from .providers.gemini import GeminiProvider
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return GeminiProvider(settings.gemini_api_key, settings.gemini_model)
