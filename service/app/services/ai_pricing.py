"""Approximate AI token cost estimates (Pantry Raider).

The token tracker (services/usage.py) records combined input+output totals, so
the Settings page can only show an approximate spend. This module holds a small
static price table for the models the setup wizard offers per provider
(config.AI_MODELS) and turns a token count into a dollar estimate.

Prices are USD per MILLION tokens, hardcoded from each provider's published
list prices as of October 2026:
  - Anthropic: claude.com/pricing (Haiku 5.5 $0.10/$0.50 for prompts up to
    100K tokens, Sonnet 5.5 $2/$10, Opus 5.5 $4/$20)
  - Google: ai.google.dev/gemini-api/docs/pricing
  - OpenAI: developers.openai.com/api/docs/pricing
Retired models stay in the table so usage recorded before an install moved
off them still gets an estimate.
Providers change list prices without notice, so the UI labels every figure as
an estimate. Ollama models run locally and cost nothing per token.

Because the tracker does not split input from output tokens, the estimate uses
a blended per-token rate assuming the app's typical mix (photo and label
analysis is input-heavy with short JSON replies): 75% input, 25% output.
Unknown models get no estimate at all rather than a guess.

Everything here is pure and deterministic so it unit-tests without a network.
"""
from __future__ import annotations

# Assumed share of tracked tokens that were input tokens. Vision calls send a
# large image plus prompt and get back a short JSON object, so input dominates.
INPUT_SHARE = 0.75

# model id -> (USD per 1M input tokens, USD per 1M output tokens).
# Keep in sync with config.AI_MODELS when the curated model lists change.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    # Google Gemini
    "gemini-3.8-flash":      (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-4-argon":        (2.00, 10.00),
    "gemini-2.5-flash":      (0.30, 2.50),
    "gemini-2.5-pro":        (1.25, 10.00),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.0-flash":      (0.10, 0.40),
    # OpenAI
    "gpt-6-luna":            (0.10, 0.50),
    "gpt-6.1-sol":           (2.00, 10.00),
    "gpt-6-astra":           (10.00, 50.00),
    "gpt-4o-mini":           (0.15, 0.60),
    "gpt-4o":                (2.50, 10.00),
    "gpt-4.1-mini":          (0.40, 1.60),
    "gpt-4.1":               (2.00, 8.00),
    # Anthropic
    "claude-haiku-5-5":      (0.10, 0.50),
    "claude-sonnet-5-5":     (2.00, 10.00),
    "claude-opus-5-5":       (4.00, 20.00),
    "claude-haiku-4-5":      (1.00, 5.00),
    "claude-sonnet-4-6":     (3.00, 15.00),
    "claude-opus-4-8":       (5.00, 25.00),
    # Ollama runs locally: no per-token cost.
    "gemma4:e4b":            (0.0, 0.0),
    "gemma4:e2b":            (0.0, 0.0),
    "qwen3.5:4b":            (0.0, 0.0),
    "qwen3.5:9b":            (0.0, 0.0),
    "minicpm-v4.6":          (0.0, 0.0),
    "llava:7b":              (0.0, 0.0),
    "llava:13b":             (0.0, 0.0),
    "llama3.2-vision:11b":   (0.0, 0.0),
    "moondream":             (0.0, 0.0),
}


def model_prices(model: str) -> tuple[float, float] | None:
    """(input, output) USD per 1M tokens for ``model``, or None if unknown.

    Matches the exact id first, then a dated-variant prefix so ids like
    ``claude-haiku-4-5-20251001`` resolve to their base entry.
    """
    if not model:
        return None
    m = model.strip()
    if m in MODEL_PRICES:
        return MODEL_PRICES[m]
    # Dated / suffixed variants: longest matching base id wins.
    best = None
    for base, prices in MODEL_PRICES.items():
        if m.startswith(base + "-") and (best is None or len(base) > len(best[0])):
            best = (base, prices)
    return best[1] if best else None


def blended_rate(model: str) -> float | None:
    """Blended USD per 1M tokens for ``model`` (input/output mixed), or None."""
    prices = model_prices(model)
    if prices is None:
        return None
    inp, out = prices
    return inp * INPUT_SHARE + out * (1.0 - INPUT_SHARE)


def estimate_cost(tokens: int, model: str) -> float | None:
    """Approximate USD cost of ``tokens`` combined tokens on ``model``.

    Returns None when the model is unknown (no guess), 0.0 for local models.
    """
    rate = blended_rate(model)
    if rate is None:
        return None
    return max(0, int(tokens)) * rate / 1_000_000.0
