from functools import lru_cache
from .config import settings
from .providers.base import VisionProvider


@lru_cache(maxsize=1)
def get_vision_provider() -> VisionProvider:
    if not settings.ai_configured():
        from .providers.noop import NoOpProvider
        return NoOpProvider()
    return _build_provider(settings.vision_provider)


@lru_cache(maxsize=1)
def get_enrich_provider() -> VisionProvider:
    """Provider for text-only barcode enrichment.

    Follows vision_provider unless enrich_provider is set; enrich_model
    overrides the provider's default model for enrichment only.
    """
    name = settings.enrich_provider or settings.vision_provider
    if not settings.provider_key(name):
        from .providers.noop import NoOpProvider
        return NoOpProvider()
    if name == settings.vision_provider and not settings.enrich_model:
        return get_vision_provider()
    return _build_provider(name, model=settings.enrich_model or None)


def reset_providers() -> None:
    """Drop cached provider instances so settings changes apply immediately."""
    get_vision_provider.cache_clear()
    get_enrich_provider.cache_clear()


def _build_provider(name: str, model: str | None = None) -> VisionProvider:
    if name == "cloud":
        # Pantry Raider Cloud: the managed AI proxy. No model choice here;
        # the cloud picks the upstream model. Requires a paired instance
        # token (Settings, AI, Pantry Raider Cloud).
        from .providers.cloud import CloudProvider
        if not settings.cloud_instance_token:
            raise RuntimeError("This install is not linked to Pantry Raider Cloud")
        return CloudProvider(settings.cloud_base_url, settings.cloud_instance_token)

    if name == "ollama":
        from .providers.ollama import OllamaProvider
        return OllamaProvider(settings.ollama_base_url, model or settings.ollama_model)

    if name == "openai":
        from .providers.openai import OpenAIProvider
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        return OpenAIProvider(settings.openai_api_key, model or settings.openai_model,
                              extra_keys=settings._extra_keys("openai"))

    if name == "anthropic":
        from .providers.anthropic import AnthropicProvider
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        return AnthropicProvider(settings.anthropic_api_key, model or settings.anthropic_model,
                                 extra_keys=settings._extra_keys("anthropic"))

    from .providers.gemini import GeminiProvider
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return GeminiProvider(settings.gemini_api_key, model or settings.gemini_model,
                          extra_keys=settings._extra_keys("gemini"))
