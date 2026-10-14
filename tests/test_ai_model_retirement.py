"""No shipped default may point at an AI model its provider is about to retire.

The table of shutdown dates lives in config._RETIRED_AI_MODELS. A saved
setting that names a retired model is moved on automatically, but a default
that names one would hand every new install a model that is about to stop
working. These tests start failing HORIZON_DAYS before a shutdown date, so
the defaults move while there is still time to ship a release.

When one fails: move the named default to a current model (the table's
replacement is a good choice) and ship it. When a provider announces a new
shutdown, add a row to _RETIRED_AI_MODELS so this check covers it.
"""
import datetime as dt
import inspect
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "service"))

from app import config  # noqa: E402
from app.config import AI_MODELS, Settings, model_retirement  # noqa: E402

HORIZON_DAYS = 45

_MODEL_RE = re.compile(r"\b((?:claude|gemini|gpt)-[0-9a-z][0-9a-z.\-]*[0-9a-z])\b")

# Files that hand a model id to a new install, on top of the Python defaults.
_CONFIG_FILES = [
    ".env.example",
    "image/config.env",
    "docker-compose.yml",
    "docker-compose.prod.yml",
    "docker-compose.forager.yml",
    "unraid/docker-compose.yml",
    "unraid/pantryraider.xml",
    "cloud/docker-compose.yml",
    "service/app/routers/setup/connections.py",
    "cloud/app/config.py",
]


def _shipped_defaults() -> list[tuple[str, str]]:
    """Every (where, model id) a fresh install or Forager can start on."""
    found: list[tuple[str, str]] = []
    for name in config._AI_MODEL_FIELDS:
        default = Settings.model_fields[name].default
        if isinstance(default, str) and default:
            found.append((f"Settings.{name}", default))
    for provider, choices in AI_MODELS.items():
        if provider == "ollama":
            continue  # a pulled local model never stops working
        for choice in choices:
            found.append((f"AI_MODELS[{provider}]", choice["id"]))
    from app.providers.anthropic import AnthropicProvider
    from app.providers.gemini import GeminiProvider
    from app.providers.openai import OpenAIProvider
    for cls in (AnthropicProvider, GeminiProvider, OpenAIProvider):
        default = inspect.signature(cls.__init__).parameters["model"].default
        found.append((f"{cls.__name__} default", default))
    for rel in _CONFIG_FILES:
        path = ROOT / rel
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for model in _MODEL_RE.findall(line):
                found.append((f"{rel}:{lineno}", model))
    for _prefix, repl, _shutdown in config._RETIRED_AI_MODELS:
        found.append(("a replacement in _RETIRED_AI_MODELS", repl))
    return found


def test_the_scan_finds_the_defaults():
    found = {model for _, model in _shipped_defaults()}
    assert Settings.model_fields["gemini_model"].default in found
    assert "gemini-3.8-flash" in found  # the Forager default, from cloud/app/config.py


def test_no_shipped_default_is_near_its_shutdown():
    cutoff = dt.date.today() + dt.timedelta(days=HORIZON_DAYS)
    stale = []
    for where, model in _shipped_defaults():
        rule = model_retirement(model)
        if rule and dt.date.fromisoformat(rule[1]) <= cutoff:
            stale.append(f"{where}: {model} shuts down {rule[1]}, use {rule[0]}")
    assert not stale, "Defaults on retiring models:\n" + "\n".join(stale)


def test_every_replacement_is_a_listed_choice():
    listed = {c["id"] for choices in AI_MODELS.values() for c in choices}
    missing = sorted({repl for _, repl, _ in config._RETIRED_AI_MODELS} - listed)
    assert not missing, f"Replacements missing from AI_MODELS: {missing}"


def test_model_retirement_matches_snapshots_and_prefixed_ids():
    assert model_retirement("gemini-2.0-flash-001") == ("gemini-3.8-flash", "2026-06-01")
    assert model_retirement("models/gemini-2.0-flash") == ("gemini-3.8-flash", "2026-06-01")
    assert model_retirement("gemini-2.0-flash-lite")[0] == "gemini-3.5-flash-lite"
    assert model_retirement("claude-opus-5-5") is None
    assert model_retirement("") is None
    assert model_retirement(None) is None


@pytest.fixture
def retired_test_model(monkeypatch):
    monkeypatch.setattr(config, "_RETIRED_AI_MODELS", config._RETIRED_AI_MODELS + (
        ("claude-test-old", "claude-haiku-5-5", "2000-01-01"),
        ("claude-test-later", "claude-haiku-5-5", "2999-01-01"),
    ))


def test_a_running_provider_moves_off_a_model_on_its_shutdown_day(retired_test_model):
    from app.providers.anthropic import AnthropicProvider
    from app.providers.base import LiveModelMixin
    from app.providers.gemini import GeminiProvider
    from app.providers.openai import OpenAIProvider
    for cls in (AnthropicProvider, GeminiProvider, OpenAIProvider):
        assert issubclass(cls, LiveModelMixin)

    # Built without __init__ so no SDK client is made (other tests stub them).
    p = AnthropicProvider.__new__(AnthropicProvider)
    p.model = "claude-test-old"
    assert p.model == "claude-haiku-5-5"
    p.model = "claude-test-later"
    assert p.model == "claude-test-later"
    g = GeminiProvider.__new__(GeminiProvider)
    g.model = "gemini-2.0-flash"
    assert g.model == "gemini-3.8-flash"
    # Choosing a new model on a live provider still takes effect.
    p.model = "claude-opus-5-5"
    assert p.model == "claude-opus-5-5"


def test_the_connection_test_refuses_a_retired_model(retired_test_model):
    import asyncio
    from app.routers.setup.connections import test_provider
    from app.routers.setup.page import TestProviderPayload
    out = asyncio.run(test_provider(TestProviderPayload(
        provider="anthropic", api_key="k", model="claude-test-old")))
    assert out["ok"] is False
    assert "retired" in out["error"] and "claude-haiku-5-5" in out["error"]


def test_the_connection_test_warns_about_a_coming_shutdown(retired_test_model, monkeypatch):
    import asyncio
    from app.routers.setup import connections
    from app.routers.setup.page import TestProviderPayload

    async def connected(payload):
        return {"ok": True, "message": f"Connected: model {payload.model} available."}

    monkeypatch.setattr(connections, "_test_provider", connected)
    out = asyncio.run(connections.test_provider(TestProviderPayload(
        provider="anthropic", api_key="k", model="claude-test-later")))
    assert out["ok"] is True
    assert "January 1, 2999" in out["message"]
    assert "claude-haiku-5-5" in out["message"]
