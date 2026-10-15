"""Approximate AI cost estimates (Pantry Raider): the static price table,
model lookup with dated-variant fallback, and the blended cost math."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import AI_MODELS  # noqa: E402
from app.services import ai_pricing  # noqa: E402


def test_every_curated_model_has_a_price():
    """Each model the setup wizard offers must resolve to a price entry, so
    the Settings estimate never silently disappears for a stock choice."""
    for provider, models in AI_MODELS.items():
        for m in models:
            assert ai_pricing.model_prices(m["id"]) is not None, (
                f"{provider}:{m['id']} missing from ai_pricing.MODEL_PRICES"
            )


def test_known_cloud_models_have_positive_rates():
    for mid in ("gemini-2.5-flash", "gpt-4o-mini", "claude-opus-4-8"):
        inp, out = ai_pricing.model_prices(mid)
        assert inp > 0 and out > 0
        assert out >= inp  # output list prices are never cheaper than input


def test_ollama_models_are_free():
    assert ai_pricing.estimate_cost(5_000_000, "llava:7b") == 0.0
    assert ai_pricing.blended_rate("moondream") == 0.0


def test_dated_variant_resolves_to_base_model():
    # The Anthropic dropdown offers a dated snapshot id.
    assert ai_pricing.model_prices("claude-haiku-4-5-20251001") == \
        ai_pricing.MODEL_PRICES["claude-haiku-4-5"]


def test_unknown_model_gets_no_guess():
    assert ai_pricing.model_prices("some-custom-model") is None
    assert ai_pricing.blended_rate("") is None
    assert ai_pricing.estimate_cost(123456, "gpt-99-ultra") is None


def test_blended_rate_between_input_and_output_prices():
    inp, out = ai_pricing.MODEL_PRICES["claude-sonnet-4-6"]
    rate = ai_pricing.blended_rate("claude-sonnet-4-6")
    assert inp <= rate <= out
    assert rate == pytest.approx(
        inp * ai_pricing.INPUT_SHARE + out * (1 - ai_pricing.INPUT_SHARE)
    )


def test_estimate_cost_math():
    # 1M blended tokens on gemini-2.5-flash: 0.75*0.30 + 0.25*2.50 = 0.85
    assert ai_pricing.estimate_cost(1_000_000, "gemini-2.5-flash") == \
        pytest.approx(0.85)
    assert ai_pricing.estimate_cost(0, "gemini-2.5-flash") == 0.0
    # Negative counts never produce a negative charge.
    assert ai_pricing.estimate_cost(-5, "gemini-2.5-flash") == 0.0
