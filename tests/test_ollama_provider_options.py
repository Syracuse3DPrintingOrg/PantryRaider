"""What the Ollama provider sends, checked on the wire.

Ollama keeps the END of a prompt that outgrows its context window, and its
default window is small. Recipe import put the instructions first and up to
18,000 characters of page text after them (a PDF had no cap at all) with no
num_ctx, so a long page pushed the instructions out and the model answered
without them. Ollama 0.32.10 also stopped applying a repeat penalty by
default, which lets a small model loop until it runs out of room. A model
that was never pulled answered with a generic analysis failure, and on a Pi
appliance the default address (the compose name "ollama") does not resolve
because the app runs with host networking.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

# ollama.py takes its prompts from gemini.py, which imports the Gemini SDK at
# module load; the SDK is not installed in the pure-logic test environment.
# Stub it the same way tests/test_receipt_purchase.py does.
if "google.generativeai" not in sys.modules:
    _google = sys.modules.setdefault("google", types.ModuleType("google"))
    _genai = types.ModuleType("google.generativeai")
    _genai.configure = lambda **kwargs: None
    _genai.GenerativeModel = lambda *args, **kwargs: types.SimpleNamespace()
    _google.generativeai = _genai
    sys.modules["google.generativeai"] = _genai

from app.config import settings  # noqa: E402
from app.providers.ollama import OllamaProvider  # noqa: E402


class _Capture:
    """A MockTransport handler that keeps every request body."""

    def __init__(self, reply: str = '{"name": "Soup"}', status: int = 200, body=None):
        self.bodies: list[dict] = []
        self.reply = reply
        self.status = status
        self.body = body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        if self.body is not None:
            return httpx.Response(self.status, json=self.body)
        return httpx.Response(self.status, json={
            "response": self.reply, "prompt_eval_count": 40, "eval_count": 60})


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    return tmp_path


def _provider(capture: _Capture, model: str = "llava:7b") -> OllamaProvider:
    return OllamaProvider("http://ollama.test:11434", model,
                          transport=httpx.MockTransport(capture))


# --- recipe import from page text ------------------------------------------------

def test_page_text_comes_first_and_the_instructions_last():
    cap = _Capture()
    page = "Grandma's lentil soup. 2 cups lentils. Simmer for 40 minutes."
    asyncio.run(_provider(cap).extract_recipe(page_text=page))
    prompt = cap.bodies[0]["prompt"]
    assert prompt.startswith("--- PAGE TEXT ---")
    assert prompt.index(page) < prompt.index("Return a JSON object")
    # The instructions end the prompt, so a trimmed window keeps them.
    assert prompt.rstrip().endswith("No markdown, no explanation.")
    assert "webpage text above" in prompt


def test_page_text_is_capped_at_12000_characters():
    cap = _Capture()
    asyncio.run(_provider(cap).extract_recipe(page_text="x" * 30000))
    prompt = cap.bodies[0]["prompt"]
    page = prompt.split("--- PAGE TEXT ---\n", 1)[1].split("\n--- END OF PAGE TEXT ---", 1)[0]
    assert page == "x" * 12000


def test_recipe_text_import_sends_context_and_penalty():
    cap = _Capture()
    asyncio.run(_provider(cap).extract_recipe(page_text="soup"))
    assert cap.bodies[0]["options"] == {
        "num_ctx": 8192, "num_predict": 2048, "repeat_penalty": 1.1}


def test_recipe_photo_import_caps_the_reply():
    cap = _Capture()
    asyncio.run(_provider(cap).extract_recipe(image_data=b"img", mime_type="image/jpeg"))
    body = cap.bodies[0]
    assert body["images"]
    assert body["options"] == {"num_predict": 2048, "repeat_penalty": 1.1}


# --- every other call -------------------------------------------------------------

def test_text_generation_gets_the_larger_context():
    cap = _Capture()
    asyncio.run(_provider(cap).generate_recipe("lentil soup"))
    assert cap.bodies[0]["options"] == {
        "num_ctx": 8192, "num_predict": 4096, "repeat_penalty": 1.1}


def test_photo_analysis_caps_the_reply_and_sets_the_penalty():
    cap = _Capture()
    asyncio.run(_provider(cap).analyze_food(b"img", "image/jpeg"))
    assert cap.bodies[0]["options"] == {"num_predict": 2048, "repeat_penalty": 1.1}


def test_receipts_get_the_same_budget_as_every_other_provider():
    """A receipt lists every line it holds, so its reply is long. Capping it at
    the photo budget (2048) cut a receipt of more than about two dozen lines
    off mid-JSON, and a reply that stops there cannot be parsed at all, so the
    whole scan failed. OpenAI and Anthropic give receipts 8192; Ollama matches
    them, and a food photo keeps the smaller cap."""
    cap = _Capture()
    asyncio.run(_provider(cap).analyze_receipt(b"img", "image/jpeg"))
    assert cap.bodies[0]["options"] == {"num_predict": 8192, "repeat_penalty": 1.1}

    cap = _Capture()
    asyncio.run(_provider(cap).extract_receipt_prices(b"img", "image/jpeg"))
    assert cap.bodies[0]["options"] == {"num_predict": 8192, "repeat_penalty": 1.1}


def test_enrich_and_identify_cap_the_reply_and_set_the_penalty():
    cap = _Capture()
    asyncio.run(_provider(cap).enrich_product({"product_name": "zero sugar"}))
    asyncio.run(_provider(cap).identify_barcode("0049000000443"))
    for body in cap.bodies:
        assert body["options"] == {"num_predict": 1024, "repeat_penalty": 1.1}


def test_enrich_tokens_count_toward_usage(_data_dir):
    cap = _Capture()
    asyncio.run(_provider(cap).enrich_product({"product_name": "zero sugar"}))
    usage = json.loads((_data_dir / "ai_usage.json").read_text())
    assert usage["by_provider"]["ollama"] == 100


# --- a model that was never pulled ---------------------------------------------------

def test_missing_model_says_how_to_pull_it():
    cap = _Capture(status=404, body={"error": 'model "llava:7b" not found, try pulling it first'})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_provider(cap).analyze_food(b"img", "image/jpeg"))
    assert exc.value.status_code == 503
    assert exc.value.detail == (
        "Ollama is running, but the model llava:7b is not installed. Pull it "
        "with: docker exec foodassistant-ollama ollama pull llava:7b")


def test_missing_model_on_the_text_paths_too():
    cap = _Capture(status=404, body={"error": "model not found"})
    for call in (lambda p: p.enrich_product({"product_name": "x"}),
                 lambda p: p.extract_recipe(page_text="soup"),
                 lambda p: p.generate_recipe("soup")):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(call(_provider(cap, model="llama3.2-vision")))
        assert exc.value.status_code == 503
        assert "ollama pull llama3.2-vision" in exc.value.detail


def test_other_errors_still_raise_as_errors():
    cap = _Capture(status=500, body={"error": "out of memory"})
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_provider(cap).analyze_food(b"img", "image/jpeg"))


# --- the address on a Pi appliance -------------------------------------------------

def test_pi_hosted_default_address_means_localhost(monkeypatch):
    from app.dependencies import _build_provider, ollama_base_url
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted")
    for url in ("http://ollama:11434", "http://ollama:11434/", ""):
        monkeypatch.setattr(settings, "ollama_base_url", url)
        assert ollama_base_url() == "http://localhost:11434", url
    assert _build_provider("ollama").base_url == "http://localhost:11434"


def test_an_address_the_user_entered_is_kept(monkeypatch):
    from app.dependencies import ollama_base_url
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted")
    monkeypatch.setattr(settings, "ollama_base_url", "http://gpu-box.local:11434")
    assert ollama_base_url() == "http://gpu-box.local:11434"


def test_server_keeps_the_compose_address(monkeypatch):
    from app.dependencies import _build_provider, ollama_base_url
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "ollama_base_url", "http://ollama:11434")
    assert ollama_base_url() == "http://ollama:11434"
    monkeypatch.setattr(settings, "ollama_base_url", "")
    assert _build_provider("ollama").base_url == "http://ollama:11434"
