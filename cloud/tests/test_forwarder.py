"""GeminiForwarder against a mocked httpx transport: success with real
token counts, upstream error mapping, timeouts, and no key leakage."""
import asyncio
import io
import json

import httpx
import pytest

from app.forwarder import ForwarderError, GeminiForwarder
from tests.conftest import activate_entitlement

API_KEY = "test-gemini-key-XYZ"


def _gemini_response(text="looks like apples", prompt_tokens=321,
                     candidate_tokens=45):
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": candidate_tokens,
            "totalTokenCount": prompt_tokens + candidate_tokens,
        },
    }


def _forwarder(handler):
    return GeminiForwarder(api_key=API_KEY, timeout=5.0,
                           transport=httpx.MockTransport(handler))


def test_success_returns_text_and_real_tokens():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_gemini_response())

    result = asyncio.run(_forwarder(handler).forward(
        "food", b"fake-jpeg-bytes", "image/jpeg", ""))
    assert result.result["text"] == "looks like apples"
    assert result.tokens == 321 + 45
    # The request carried the image inline and the key only in the header.
    assert "generativelanguage.googleapis.com" in seen["url"]
    assert "gemini-2.5-flash" in seen["url"]
    assert API_KEY not in seen["url"]
    assert seen["headers"]["x-goog-api-key"] == API_KEY
    parts = seen["body"]["contents"][0]["parts"]
    assert parts[0]["text"]  # the task prompt
    assert parts[1]["inline_data"]["mime_type"] == "image/jpeg"


def test_enrich_sends_text_only():
    def handler(request):
        body = json.loads(request.content)
        parts = body["contents"][0]["parts"]
        assert len(parts) == 1
        assert "barcode product data" in parts[0]["text"]
        return httpx.Response(200, json=_gemini_response(text="{}"))

    result = asyncio.run(_forwarder(handler).forward(
        "enrich", None, "", "barcode product data"))
    assert result.result["text"] == "{}"


def test_upstream_429_maps_to_429():
    def handler(request):
        return httpx.Response(429, json={"error": {"message": "quota"}})

    with pytest.raises(ForwarderError) as exc:
        asyncio.run(_forwarder(handler).forward("enrich", None, "", "x"))
    assert exc.value.status == 429
    assert exc.value.detail["error"] == "upstream_rate_limited"
    assert API_KEY not in json.dumps(exc.value.detail)


def test_upstream_500_maps_to_502():
    def handler(request):
        return httpx.Response(500, text=f"boom {API_KEY} echoed")

    with pytest.raises(ForwarderError) as exc:
        asyncio.run(_forwarder(handler).forward("enrich", None, "", "x"))
    assert exc.value.status == 502
    assert exc.value.detail["error"] == "upstream_error"
    assert exc.value.detail["upstream_status"] == 500
    # Even an upstream body that echoes the key never reaches the client.
    assert API_KEY not in json.dumps(exc.value.detail)


def test_timeout_maps_to_504():
    def handler(request):
        raise httpx.ReadTimeout("too slow")

    with pytest.raises(ForwarderError) as exc:
        asyncio.run(_forwarder(handler).forward("food", b"img", "image/jpeg", ""))
    assert exc.value.status == 504
    assert exc.value.detail["error"] == "upstream_timeout"
    assert API_KEY not in json.dumps(exc.value.detail)


def test_missing_key_is_503():
    fwd = GeminiForwarder(api_key="")
    with pytest.raises(ForwarderError) as exc:
        asyncio.run(fwd.forward("enrich", None, "", "x"))
    assert exc.value.status == 503


def test_proxy_records_gemini_usage(client, instance_token, monkeypatch):
    """End to end through /v1/ai/analyze: the ledger gets the REAL token
    count from usageMetadata, not an estimate."""
    from app import usage
    from app.database import SessionLocal
    from app.models import Account, UsageLedger
    from app.routers import ai as ai_router

    activate_entitlement()

    def handler(request):
        return httpx.Response(200, json=_gemini_response(
            prompt_tokens=1234, candidate_tokens=56))

    monkeypatch.setattr(ai_router, "get_forwarder",
                        lambda: _forwarder(handler))
    resp = client.post(
        "/v1/ai/analyze", data={"kind": "food"},
        files={"image": ("p.jpg", io.BytesIO(b"fake"), "image/jpeg")},
        headers={"Authorization": f"Bearer {instance_token}"})
    assert resp.status_code == 200
    assert resp.json()["tokens"] == 1290
    db = SessionLocal()
    try:
        row = db.query(UsageLedger).one()
        assert row.tokens == 1290
        account_id = db.query(Account).first().id
        assert usage.month_total(db, account_id, usage.month_key()) == 1290
    finally:
        db.close()


def test_proxy_maps_forwarder_errors(client, instance_token, monkeypatch):
    from app.routers import ai as ai_router

    def handler(request):
        return httpx.Response(429)

    monkeypatch.setattr(ai_router, "get_forwarder",
                        lambda: _forwarder(handler))
    resp = client.post(
        "/v1/ai/analyze", data={"kind": "enrich", "text": "x"},
        headers={"Authorization": f"Bearer {instance_token}"})
    assert resp.status_code == 429
    body = json.dumps(resp.json())
    assert "upstream_rate_limited" in body
    assert API_KEY not in body
