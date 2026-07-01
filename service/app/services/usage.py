"""AI token usage tracking and budget (FoodAssistant).

Records the tokens each AI call consumes so the user can see what their API key
is spending and, optionally, cap it with a token budget. This is the local
foundation for a future cloud implementation that meters and limits tokens per
user; here it meters this instance's own usage.

Usage is persisted to ``data_dir/ai_usage.json`` as an all-time total plus a
per-calendar-month total and a per-provider breakdown, so a monthly budget can
reset on its own. All the accounting helpers are pure/deterministic (the current
month is passed in) so they unit-test without a clock or network.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

_LOCK = threading.Lock()


def _path() -> Path:
    from ..config import settings
    return Path(settings.data_dir) / "ai_usage.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text())
    except Exception:
        return {}


def _save(data: dict) -> None:
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(p)
    except Exception:
        pass


def _month_key(now=None) -> str:
    """Current 'YYYY-MM' key. ``now`` injectable for tests."""
    if now is None:
        from datetime import datetime
        now = datetime.now()
    return f"{now.year:04d}-{now.month:02d}"


def tokens_from_response(resp) -> int:
    """Best-effort total token count from any provider's response object.

    Handles the Gemini SDK (``usage_metadata.total_token_count``), Anthropic and
    OpenAI (``usage`` with input/output or total), and Ollama (eval counts),
    accepting either attribute or dict access. Returns 0 when unknown, so a
    provider that does not report usage simply records nothing."""
    if resp is None:
        return 0

    def g(obj, name):
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    # Gemini SDK: response.usage_metadata.total_token_count
    um = g(resp, "usage_metadata")
    if um is not None:
        tot = g(um, "total_token_count")
        if tot:
            return int(tot)
        pt, ct = g(um, "prompt_token_count") or 0, g(um, "candidates_token_count") or 0
        if pt or ct:
            return int(pt) + int(ct)

    # OpenAI / Anthropic: a "usage" block
    u = g(resp, "usage")
    if u is not None:
        tot = g(u, "total_tokens")
        if tot:
            return int(tot)
        it = g(u, "input_tokens") or g(u, "prompt_tokens") or 0
        ot = g(u, "output_tokens") or g(u, "completion_tokens") or 0
        if it or ot:
            return int(it) + int(ot)

    # Ollama: prompt_eval_count + eval_count on the top-level response
    pec, ec = g(resp, "prompt_eval_count") or 0, g(resp, "eval_count") or 0
    if pec or ec:
        return int(pec) + int(ec)
    return 0


def record(provider: str, tokens: int, now=None) -> None:
    """Add ``tokens`` to the running totals for ``provider``. No-op for 0."""
    if not tokens or tokens < 0:
        return
    mk = _month_key(now)
    with _LOCK:
        data = _load()
        data["total"] = int(data.get("total", 0)) + int(tokens)
        by = data.setdefault("by_provider", {})
        by[provider] = int(by.get(provider, 0)) + int(tokens)
        months = data.setdefault("months", {})
        months[mk] = int(months.get(mk, 0)) + int(tokens)
        _save(data)


def record_response(provider: str, resp, now=None) -> int:
    """Record the tokens a provider response reports; returns the count."""
    n = tokens_from_response(resp)
    if n:
        record(provider, n, now=now)
    return n


def get_usage(now=None) -> dict:
    """Snapshot for the UI: all-time total, this month, per-provider, and the
    budget with how much is left / whether it is exceeded."""
    from ..config import settings
    data = _load()
    mk = _month_key(now)
    month = int(data.get("months", {}).get(mk, 0))
    budget = int(getattr(settings, "ai_token_budget", 0) or 0)
    return {
        "total": int(data.get("total", 0)),
        "month": month,
        "month_key": mk,
        "by_provider": data.get("by_provider", {}),
        "budget": budget,
        "remaining": max(0, budget - month) if budget else None,
        "over_budget": bool(budget) and month >= budget,
    }


def over_budget(now=None) -> bool:
    """True when a token budget is set and this month's usage has reached it."""
    return get_usage(now)["over_budget"]


def reset() -> None:
    """Clear all recorded usage."""
    with _LOCK:
        _save({})
