"""Retired AI model ids move to their replacements (FoodAssistant-dug16).

Providers shut old model ids down (gemini-2.0-flash on 2026-06-01, Claude
Haiku 4.5 from 2026-10-15, the gpt-4o and gpt-4.1 snapshots on 2027-01-14),
and an install whose settings.json still names one would fail every AI call.
These tests pin the remap rules, the date gating, the "only the model keys
change" guarantee, the curated lists, and the provider request shapes the new
models need.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

# The vendor SDKs are not installed in the pure-logic test env (the app
# imports them lazily); stub the bare modules as test_recipe_prompt.py does.
for _name in ("google", "google.generativeai"):
    sys.modules.setdefault(_name, types.ModuleType(_name))
sys.modules["google"].generativeai = sys.modules["google.generativeai"]
if "anthropic" not in sys.modules:
    _anthropic = types.ModuleType("anthropic")
    _anthropic.AsyncAnthropic = object
    sys.modules["anthropic"] = _anthropic

from app import config  # noqa: E402
from app.config import (AI_MODELS, migrate_retired_ai_models,  # noqa: E402
                        previous_default_ai_models,
                        replacement_for_retired_model, settings)


# -- The remap rules ---------------------------------------------------------

@pytest.mark.parametrize("old,new", [
    ("gemini-2.0-flash", "gemini-3.8-flash"),
    ("gemini-2.0-flash-001", "gemini-3.8-flash"),
    ("gemini-2.0-flash-lite", "gemini-3.5-flash-lite"),
    ("gemini-2.0-flash-lite-001", "gemini-3.5-flash-lite"),
    ("gemini-1.5-flash", "gemini-3.8-flash"),
    ("claude-haiku-4-5", "claude-haiku-5-5"),
    ("claude-haiku-4-5-20251001", "claude-haiku-5-5"),
    ("claude-3-5-sonnet-20241022", "claude-sonnet-5-5"),
    ("claude-3-haiku-20240307", "claude-haiku-5-5"),
    ("gpt-4o-mini", "gpt-6-luna"),
    ("gpt-4o-2024-08-06", "gpt-6-luna"),
    ("gpt-4.1", "gpt-6-luna"),
    ("gpt-4.1-mini-2025-04-14", "gpt-6-luna"),
])
def test_retired_ids_map_after_shutdown(old, new):
    assert replacement_for_retired_model(old, "2027-02-01") == new


def test_a_model_is_left_alone_until_its_shutdown_date():
    # Claude Haiku 4.5 keeps working until Anthropic retires it.
    assert replacement_for_retired_model("claude-haiku-4-5-20251001", "2026-10-14") is None
    assert replacement_for_retired_model("claude-haiku-4-5-20251001", "2026-10-15") == "claude-haiku-5-5"
    # The OpenAI snapshots keep working until 2027-01-14.
    assert replacement_for_retired_model("gpt-4o-mini", "2027-01-13") is None
    assert replacement_for_retired_model("gpt-4o-mini", "2027-01-14") == "gpt-6-luna"


@pytest.mark.parametrize("model", [
    "gemini-2.5-flash", "gemini-3.8-flash", "claude-opus-4-8", "claude-sonnet-4-6",
    "claude-haiku-5-5", "gpt-6-luna", "gpt-4.1x", "", None, 42,
    # Ollama models are never remapped, even old ones.
    "llava:7b", "llava:13b", "moondream", "llama3.2-vision:11b",
])
def test_current_local_and_odd_ids_are_untouched(model):
    assert replacement_for_retired_model(model, "2030-01-01") is None


def test_migration_returns_only_changed_model_keys():
    data = {
        "gemini_model": "gemini-2.0-flash",
        "anthropic_model": "claude-opus-4-8",
        "ollama_model": "llava:7b",
        "enrich_model": "claude-haiku-4-5",
        "mealie_base_url": "http://m:9285",
    }
    before = dict(data)
    changes = migrate_retired_ai_models(data, "2026-11-01")
    assert changes == {"gemini_model": "gemini-3.8-flash",
                       "enrich_model": "claude-haiku-5-5"}
    assert data == before  # pure: the input is not modified


def test_apply_never_makes_a_retired_id_live(monkeypatch):
    # A satellite pull or a restore can hand apply() an old id.
    monkeypatch.setattr(settings, "gemini_model", "gemini-3.8-flash", raising=False)
    monkeypatch.setattr(settings, "ui_theme", settings.ui_theme, raising=False)
    settings.apply({"gemini_model": "gemini-2.0-flash"})
    assert settings.gemini_model == "gemini-3.8-flash"


def test_save_rewrites_a_retired_id_and_keeps_everything_else(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "gemini_model", settings.gemini_model, raising=False)
    monkeypatch.setattr(settings, "ui_theme", settings.ui_theme, raising=False)
    sf = tmp_path / "settings.json"
    sf.write_text(json.dumps({"gemini_model": "gemini-2.0-flash",
                              "mealie_base_url": "http://m:9285",
                              "ollama_model": "llava:7b"}))
    settings.save({"ui_theme": "forest"})
    on_disk = json.loads(sf.read_text())
    assert on_disk["gemini_model"] == "gemini-3.8-flash"
    assert on_disk["ollama_model"] == "llava:7b"
    assert on_disk["mealie_base_url"] == "http://m:9285"
    assert on_disk["ui_theme"] == "forest"


# -- Keeping an existing install on the model it was running ------------------

def test_ollama_install_without_a_saved_model_keeps_llava():
    saved = {"vision_provider": "ollama", "grocy_base_url": "http://g"}
    assert previous_default_ai_models(saved) == {"ollama_model": "llava:7b"}


def test_saved_model_is_never_pinned_over():
    saved = {"vision_provider": "ollama", "ollama_model": "qwen3.5:4b"}
    assert previous_default_ai_models(saved) == {}


def test_cloud_provider_is_pinned_only_with_a_key():
    assert previous_default_ai_models({"vision_provider": "gemini"}) == {}
    assert previous_default_ai_models(
        {"vision_provider": "gemini", "gemini_api_key": "k"}) == {"gemini_model": "gemini-2.5-flash"}
    # Enrichment on a second provider counts as using it.
    assert previous_default_ai_models(
        {"vision_provider": "gemini", "gemini_model": "gemini-2.5-pro",
         "enrich_provider": "openai", "openai_api_key": "k"}) == {"openai_model": "gpt-4o-mini"}


def test_fresh_install_is_not_pinned():
    assert previous_default_ai_models({}) == {}


def _boot(tmp_path, saved: dict, env: dict | None = None) -> dict:
    """Import the app config in a fresh process against a data dir holding
    ``saved`` and report the live model fields."""
    (tmp_path / "settings.json").write_text(json.dumps(saved))
    code = (
        "import sys, json; sys.path.insert(0, %r); from app.config import settings;"
        "print(json.dumps({k: getattr(settings, k) for k in "
        "('gemini_model','openai_model','anthropic_model','ollama_model','enrich_model')}))"
    ) % str(SERVICE)
    run_env = {k: v for k, v in os.environ.items()
               if not k.endswith("_MODEL") and k not in ("VISION_PROVIDER", "DATA_DIR")}
    run_env["DATA_DIR"] = str(tmp_path)
    run_env.update(env or {})
    out = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path), env=run_env,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_startup_migrates_saved_ids_and_writes_them_back(tmp_path):
    live = _boot(tmp_path, {"vision_provider": "ollama",
                            "gemini_model": "gemini-2.0-flash",
                            "mealie_base_url": "http://m:9285"})
    assert live["gemini_model"] == "gemini-3.8-flash"
    assert live["ollama_model"] == "llava:7b"  # pinned, not moved to the new default
    on_disk = json.loads((tmp_path / "settings.json").read_text())
    assert on_disk["gemini_model"] == "gemini-3.8-flash"
    assert on_disk["ollama_model"] == "llava:7b"
    assert on_disk["mealie_base_url"] == "http://m:9285"


def test_startup_corrects_a_retired_env_var_in_memory_only(tmp_path):
    live = _boot(tmp_path, {"mealie_base_url": "http://m:9285"},
                 env={"GEMINI_MODEL": "gemini-1.5-flash"})
    assert live["gemini_model"] == "gemini-3.8-flash"
    on_disk = json.loads((tmp_path / "settings.json").read_text())
    assert "gemini_model" not in on_disk


# -- The curated lists ---------------------------------------------------------

def test_no_curated_model_is_already_retired():
    for provider, models in AI_MODELS.items():
        for m in models:
            assert replacement_for_retired_model(m["id"]) is None, (
                f"{provider}:{m['id']} is retired but still offered")


def test_first_curated_model_matches_the_field_default():
    """A new install gets the model the dropdown recommends."""
    for provider in ("gemini", "openai", "ollama"):
        assert AI_MODELS[provider][0]["id"] == config.Settings.model_fields[f"{provider}_model"].default


def test_defaults_are_current_models():
    fields = config.Settings.model_fields
    assert fields["gemini_model"].default == "gemini-3.8-flash"
    assert fields["openai_model"].default == "gpt-6-luna"
    assert fields["ollama_model"].default == "gemma4:e4b"
    ids = {m["id"] for m in AI_MODELS["anthropic"]}
    assert "claude-haiku-4-5-20251001" not in ids
    assert {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"} <= ids
    assert "claude-fable-5-1" not in ids  # never an offered default


def test_retiring_models_say_so():
    for m in AI_MODELS["openai"]:
        if m["id"].startswith("gpt-4"):
            assert "Retiring" in m["note"]


# -- Provider request shapes ---------------------------------------------------

def test_claude_5_models_get_low_effort_and_room_to_think():
    from app.providers.anthropic import request_kwargs
    kw = request_kwargs("claude-haiku-5-5", 256)
    assert kw["max_tokens"] > 256
    assert kw["extra_body"] == {"output_config": {"effort": "low"}}
    # Older models are sent exactly what they were before.
    assert request_kwargs("claude-opus-4-8", 256) == {"max_tokens": 256}


def test_claude_reply_skips_thinking_blocks():
    from app.providers.anthropic import response_text
    blk = lambda t, **kw: types.SimpleNamespace(type=t, **kw)  # noqa: E731
    reply = types.SimpleNamespace(content=[blk("thinking", thinking=""),
                                           blk("text", text='{"a": 1}')],
                                  stop_reason="end_turn")
    assert response_text(reply) == '{"a": 1}'
    empty = types.SimpleNamespace(content=[blk("thinking", thinking="")],
                                  stop_reason="max_tokens")
    with pytest.raises(RuntimeError, match="max_tokens"):
        response_text(empty)


def test_openai_reasoning_models_use_completion_tokens():
    from app.providers.openai import token_fields
    luna = token_fields("gpt-6-luna", 256)
    assert "max_tokens" not in luna
    assert luna["max_completion_tokens"] > 256 and luna["reasoning_effort"] == "low"
    # gpt-4o accepts max_completion_tokens but not a reasoning effort.
    assert token_fields("gpt-4o-mini", 256) == {"max_completion_tokens": 256}
