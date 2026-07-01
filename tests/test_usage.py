"""AI token usage tracking + budget (FoodAssistant): the response token
extractor, recording/accumulation, monthly rollup, and the budget check."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402
from app.services import usage  # noqa: E402


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "ai_token_budget", 0, raising=False)
    yield


class _Now:
    def __init__(self, y, m):
        self.year, self.month = y, m


def test_tokens_from_gemini_usage_metadata():
    resp = SimpleNamespace(usage_metadata=SimpleNamespace(total_token_count=1234))
    assert usage.tokens_from_response(resp) == 1234


def test_tokens_from_gemini_prompt_plus_candidates():
    resp = SimpleNamespace(usage_metadata=SimpleNamespace(
        total_token_count=0, prompt_token_count=10, candidates_token_count=5))
    assert usage.tokens_from_response(resp) == 15


def test_tokens_from_openai_total_and_anthropic_split():
    assert usage.tokens_from_response({"usage": {"total_tokens": 42}}) == 42
    assert usage.tokens_from_response(
        {"usage": {"input_tokens": 30, "output_tokens": 12}}) == 42


def test_tokens_from_ollama_eval_counts():
    assert usage.tokens_from_response({"prompt_eval_count": 100, "eval_count": 40}) == 140


def test_tokens_from_unknown_is_zero():
    assert usage.tokens_from_response({"nope": 1}) == 0
    assert usage.tokens_from_response(None) == 0


def test_record_accumulates_total_month_and_provider():
    now = _Now(2026, 7)
    usage.record("gemini", 100, now=now)
    usage.record("gemini", 50, now=now)
    usage.record("openai", 25, now=now)
    u = usage.get_usage(now=now)
    assert u["total"] == 175
    assert u["month"] == 175
    assert u["by_provider"] == {"gemini": 150, "openai": 25}


def test_month_rolls_over():
    usage.record("gemini", 100, now=_Now(2026, 7))
    usage.record("gemini", 30, now=_Now(2026, 8))
    assert usage.get_usage(now=_Now(2026, 8))["month"] == 30   # only August
    assert usage.get_usage(now=_Now(2026, 8))["total"] == 130  # all time


def test_budget_and_over_budget(monkeypatch):
    now = _Now(2026, 7)
    monkeypatch.setattr(settings, "ai_token_budget", 200, raising=False)
    usage.record("gemini", 150, now=now)
    assert usage.over_budget(now=now) is False
    usage.record("gemini", 60, now=now)   # 210 >= 200
    u = usage.get_usage(now=now)
    assert u["over_budget"] is True and u["remaining"] == 0
    assert usage.over_budget(now=now) is True


def test_reset_clears_usage():
    usage.record("gemini", 100, now=_Now(2026, 7))
    usage.reset()
    assert usage.get_usage(now=_Now(2026, 7))["total"] == 0


def test_record_response_records_and_returns():
    now = _Now(2026, 7)
    n = usage.record_response("openai", {"usage": {"total_tokens": 77}}, now=now)
    assert n == 77
    assert usage.get_usage(now=now)["total"] == 77
