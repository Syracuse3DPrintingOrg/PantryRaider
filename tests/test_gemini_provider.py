"""GeminiProvider on the google-genai SDK: the call shape, the JSON reply
parsing, the usage accounting, and the cached health check. The SDK client is
swapped for a fake, so no network or API key is needed."""
import sys
import types

import pytest

try:
    import google.genai  # noqa: F401
except ImportError:
    _google = sys.modules.setdefault("google", types.ModuleType("google"))
    _genai = types.ModuleType("google.genai")
    _genai_types = types.ModuleType("google.genai.types")
    _genai.Client = lambda *args, **kwargs: types.SimpleNamespace()
    _genai_types.GenerateContentConfig = lambda **kwargs: types.SimpleNamespace(**kwargs)
    _genai.types = _genai_types
    _google.genai = _genai
    sys.modules["google.genai"] = _genai
    sys.modules["google.genai.types"] = _genai_types

from app.providers import gemini  # noqa: E402
from app.providers.gemini import GeminiProvider  # noqa: E402


class _FakeModels:
    def __init__(self, reply="{}", fail_get=False):
        self.reply = reply
        self.fail_get = fail_get
        self.calls = []
        self.gets = 0

    async def generate_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return types.SimpleNamespace(
            text=self.reply,
            usage_metadata=types.SimpleNamespace(total_token_count=42))

    async def get(self, *, model):
        self.gets += 1
        if self.fail_get:
            raise RuntimeError("bad key")
        return types.SimpleNamespace(name=f"models/{model}")


def _provider(models, **kwargs):
    p = GeminiProvider("primary-key", model="gemini-test", **kwargs)
    p.client = types.SimpleNamespace(aio=types.SimpleNamespace(models=models))
    return p


@pytest.mark.anyio
async def test_generate_uses_model_and_json_config(monkeypatch):
    recorded = []
    from app.services import usage
    monkeypatch.setattr(usage, "record_response",
                        lambda name, resp: recorded.append((name, resp)))
    models = _FakeModels('```json\n{"name": "Kewpie Mayonnaise"}\n```')
    p = _provider(models)

    data = await p.enrich_product({"product_name": "kewpie"})

    assert data == {"name": "Kewpie Mayonnaise"}
    call = models.calls[0]
    assert call["model"] == "gemini-test"
    assert call["config"].response_mime_type == "application/json"
    assert isinstance(call["contents"], list) and "kewpie" in call["contents"][0]
    assert recorded and recorded[0][0] == "gemini"


@pytest.mark.anyio
async def test_image_calls_send_an_inline_part(monkeypatch):
    monkeypatch.setattr(gemini, "_image_part",
                        lambda data, mime: ("part", data, mime))
    models = _FakeModels('{"name": "roma tomatoes", "confidence": 0.9}')
    p = _provider(models)

    result = await p.analyze_food(b"jpeg-bytes", "image/jpeg")

    assert result.items[0].name == "roma tomatoes"
    assert models.calls[0]["contents"][1] == ("part", b"jpeg-bytes", "image/jpeg")


@pytest.mark.anyio
async def test_health_check_is_cached_and_reports_failure():
    ok = _provider(_FakeModels())
    assert await ok.health_check() is True
    assert await ok.health_check() is True
    assert ok.client.aio.models.gets == 1

    bad = _provider(_FakeModels(fail_get=True))
    assert await bad.health_check() is False


def test_extra_keys_drop_blanks_and_the_primary():
    p = _provider(_FakeModels(), extra_keys=["", "primary-key", "spare"])
    assert p.extra_keys == ["spare"]
