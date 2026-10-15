"""The enrich_batch kind: a receipt's shelf-life estimates in one request.

The proxy allows 30 requests a minute per install, and the app used to ask
for one estimate per receipt line, so on a long receipt the lines past about
the 28th got none. A batch carries up to MAX_BATCH_ITEMS lines in one request
that is gated, rate limited and metered like any other call. Gemini is an
httpx.MockTransport here; nothing leaves the process.
"""
import ast
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app import forwarder, usage
from app.database import SessionLocal
from app.forwarder import GeminiForwarder, StubForwarder
from app.models import UsageLedger
from app.routers import ai as ai_router
from tests.conftest import activate_entitlement


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _lines(count):
    return json.dumps([{"index": n, "name": f"Item {n}", "category": "Dairy"}
                       for n in range(1, count + 1)])


def _batch(client, token, text):
    return client.post("/v1/ai/analyze",
                       data={"kind": "enrich_batch", "text": text},
                       headers=_auth(token))


class _WatchingForwarder:
    """The stub forwarder, plus a note of each call it gets."""

    def __init__(self):
        self.calls = []

    async def forward(self, kind, image_data, mime_type, text):
        self.calls.append((kind, text))
        return await StubForwarder().forward(kind, image_data, mime_type, text)


@pytest.fixture
def watched(monkeypatch):
    fwd = _WatchingForwarder()
    monkeypatch.setattr(ai_router, "get_forwarder", lambda: fwd)
    return fwd


def _ledger():
    db = SessionLocal()
    try:
        return [(row.kind, row.tokens) for row in db.query(UsageLedger)]
    finally:
        db.close()


# --- The gates ------------------------------------------------------------------

def test_a_batch_is_one_metered_call(client, instance_token, watched):
    activate_entitlement()
    text = _lines(30)
    resp = _batch(client, instance_token, text)
    assert resp.status_code == 200
    assert resp.json()["tokens"] == StubForwarder.STUB_TOKENS
    assert resp.json()["quota"]["used"] == StubForwarder.STUB_TOKENS
    # The whole list reached the forwarder in one call, as sent.
    assert watched.calls == [("enrich_batch", text)]
    # Charged to the month like any other task, under its own kind.
    assert _ledger() == [("enrich_batch", StubForwarder.STUB_TOKENS)]


def test_a_full_batch_counts_once_against_the_rate_limit(client, instance_token,
                                                         monkeypatch, watched):
    activate_entitlement()
    from app.config import settings
    monkeypatch.setattr(settings, "proxy_rate_per_minute", 2)
    assert _batch(client, instance_token, _lines(30)).status_code == 200
    assert _batch(client, instance_token, _lines(30)).status_code == 200
    assert _batch(client, instance_token, _lines(1)).status_code == 429
    assert len(watched.calls) == 2


def test_too_many_items_is_refused_before_anything_is_held_or_sent(
        client, instance_token, watched):
    activate_entitlement()
    assert ai_router.MAX_BATCH_ITEMS == 30
    resp = _batch(client, instance_token, _lines(31))
    assert resp.status_code == 413
    assert "30 per request" in resp.json()["detail"]
    assert watched.calls == []
    assert _ledger() == []


@pytest.mark.parametrize("text", [
    "", "not json", "{}", "[]", "[1, 2]", '[{"index": 1}, "two"]',
    '{"items": [{"index": 1}]}', "[" * 5000,
])
def test_a_batch_that_is_not_a_list_of_items_is_400(client, instance_token,
                                                     watched, text):
    activate_entitlement()
    resp = _batch(client, instance_token, text)
    assert resp.status_code == 400
    assert resp.json()["detail"] == ai_router.BATCH_UNREADABLE_MESSAGE
    assert watched.calls == []
    assert _ledger() == []


def test_a_batch_still_gets_the_text_cap(client, instance_token, watched):
    activate_entitlement()
    text = json.dumps([{"index": 1, "name": "x" * ai_router.MAX_TEXT_CHARS}])
    resp = _batch(client, instance_token, text)
    assert resp.status_code == 413
    assert watched.calls == []


def test_a_batch_over_quota_is_402(client, instance_token, watched):
    account_id = activate_entitlement()
    from app.config import PLAN_QUOTAS
    db = SessionLocal()
    try:
        usage.record(db, account_id, 1, PLAN_QUOTAS["premium"], "food",
                     usage.month_key(), "2026-01-01T00:00:00+00:00")
    finally:
        db.close()
    resp = _batch(client, instance_token, _lines(3))
    assert resp.status_code == 402
    assert resp.json()["detail"]["error"] == "quota_exceeded"
    assert watched.calls == []


def test_an_unknown_kind_keeps_the_answer_the_app_reads(client, instance_token,
                                                        watched):
    # An app newer than this Forager sends a kind it does not know yet, and
    # reads this exact answer as "fall back to the kinds it has". Before
    # enrich_batch existed, every Forager answered it this way.
    activate_entitlement()
    resp = client.post("/v1/ai/analyze", data={"kind": "poetry", "text": "[]"},
                       headers=_auth(instance_token))
    assert resp.status_code == 400
    assert resp.json() == {"detail": "Unknown task kind: poetry"}
    assert watched.calls == []


# --- The forwarder ----------------------------------------------------------------

def _gemini_reply(text, prompt_tokens=600, candidate_tokens=400):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": prompt_tokens,
                              "candidatesTokenCount": candidate_tokens}}


def _forward(reply, text, model="gemini-3.8-flash"):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_gemini_reply(reply))

    fwd = GeminiForwarder(api_key="test-key", model=model, timeout=5.0,
                          transport=httpx.MockTransport(handler))
    out = asyncio.run(fwd.forward("enrich_batch", None, "", text))
    return out, seen["body"]


def test_the_items_go_inside_the_batch_prompt():
    text = _lines(3)
    _, body = _forward("{}", text)
    parts = body["contents"][0]["parts"]
    assert len(parts) == 1  # text only
    prompt = parts[0]["text"]
    assert prompt == forwarder._ENRICH_BATCH_PROMPT.format(items=text)
    assert prompt.index(text) < prompt.index("Return a JSON object")
    for field in ("items", "index", "storage_type", "shelf_life_days"):
        assert f'"{field}"' in prompt
    assert "{{" not in prompt and "{items}" not in prompt


def test_the_batch_asks_for_json_under_its_own_cap():
    _, body = _forward("{}", _lines(1))
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["maxOutputTokens"] == 4096
    assert config["thinkingConfig"] == {"thinkingLevel": "low"}


def test_the_cap_leaves_room_for_a_full_batch():
    # A full batch answers in about 30 tokens an item. Even with the 2.5
    # thinking budget spent, what is left is several times that.
    cap, budget = forwarder._OUTPUT_LIMITS["enrich_batch"]
    assert cap - budget >= 3 * 30 * ai_router.MAX_BATCH_ITEMS
    config = forwarder._generation_config("enrich_batch", "gemini-2.5-flash")
    assert config["thinkingConfig"] == {"thinkingBudget": budget}


def test_the_batch_reply_comes_back_as_text_and_fields():
    reply = json.dumps({"items": [
        {"index": 1, "storage_type": "refrigerated", "shelf_life_days": 10},
        {"index": 2, "storage_type": "dry", "shelf_life_days": 365},
    ]})
    out, _ = _forward(reply, _lines(2))
    assert out.result["text"] == reply
    assert out.result["items"][1]["shelf_life_days"] == 365
    assert out.tokens == 1000


# --- What the app sends ---------------------------------------------------------

# The app's Forager client, read as source (never imported: the cloud is a
# separate app).
APP_CLOUD = (Path(__file__).resolve().parents[2]
             / "service" / "app" / "providers" / "cloud.py")


def _app_constants() -> dict:
    if not APP_CLOUD.exists():
        pytest.skip("the app's source is not beside this checkout")
    found = {}
    for node in ast.parse(APP_CLOUD.read_text(encoding="utf-8")).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)):
            found[node.targets[0].id] = node.value.value
    return found


def test_the_apps_batches_fit_what_the_proxy_takes():
    app = _app_constants()
    assert app["_BATCH_KIND"] == "enrich_batch"
    assert app["_BATCH_KIND"] in ai_router._KINDS
    assert 0 < app["BATCH_MAX_ITEMS"] <= ai_router.MAX_BATCH_ITEMS
    assert 0 < app["_BATCH_MAX_CHARS"] < ai_router.MAX_TEXT_CHARS


def test_the_app_still_recognizes_an_unknown_kind(client, instance_token):
    # How the app tells "this Forager has no such kind" from a real failure.
    prefix = _app_constants()["_UNKNOWN_KIND"]
    resp = client.post("/v1/ai/analyze", data={"kind": "poetry"},
                       headers=_auth(instance_token))
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith(prefix)


def test_the_proxy_meters_a_real_batch(client, instance_token, monkeypatch):
    activate_entitlement()
    reply = json.dumps({"items": [{"index": n, "storage_type": "dry",
                                   "shelf_life_days": 30} for n in range(1, 31)]})

    def handler(request):
        return httpx.Response(200, json=_gemini_reply(reply, 900, 950))

    monkeypatch.setattr(ai_router, "get_forwarder", lambda: GeminiForwarder(
        api_key="test-key", timeout=5.0, transport=httpx.MockTransport(handler)))
    resp = _batch(client, instance_token, _lines(30))
    assert resp.status_code == 200
    body = resp.json()
    assert json.loads(body["result"]["text"]) == json.loads(reply)
    assert body["tokens"] == 1850
    assert _ledger() == [("enrich_batch", 1850)]
