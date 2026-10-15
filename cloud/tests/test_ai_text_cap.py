"""The AI proxy caps the text one task may carry, and sizes the quota it holds
before forwarding from the request itself.

Without the cap a trial or Basic account could push about a megabyte of
"enrichment" text through every call while the ledger held a flat 1000 tokens
for it, so a burst of such calls could spend well past the quota before the
real counts landed. The app never sends more than a couple of kilobytes.
"""
import io

from app import usage
from app.config import settings
from app.database import SessionLocal
from app.forwarder import StubForwarder
from app.models import UsageLedger
from app.routers import ai as ai_router
from tests.conftest import activate_entitlement

# The most text one task may carry. The app sends a couple of kilobytes.
MAX_TEXT_CHARS = 8000


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class _WatchingForwarder:
    """The stub forwarder, plus a note of each call and of the reservation
    sitting in the ledger while that call is in flight."""

    def __init__(self):
        self.calls = []
        self.reserved = []

    async def forward(self, kind, image_data, mime_type, text):
        self.calls.append(kind)
        db = SessionLocal()
        try:
            self.reserved.append([row.tokens for row in db.query(UsageLedger)
                                  .filter_by(kind=usage.RESERVE_KIND)])
        finally:
            db.close()
        return await StubForwarder().forward(kind, image_data, mime_type, text)


def _watch(monkeypatch):
    fwd = _WatchingForwarder()
    monkeypatch.setattr(ai_router, "get_forwarder", lambda: fwd)
    return fwd


def _enrich(client, token, text):
    return client.post("/v1/ai/analyze", data={"kind": "enrich", "text": text},
                       headers=_auth(token))


def _food(client, token, text=""):
    return client.post(
        "/v1/ai/analyze", data={"kind": "food", "text": text},
        files={"image": ("p.jpg", io.BytesIO(b"fake-jpeg"), "image/jpeg")},
        headers=_auth(token))


def _ledger_rows():
    db = SessionLocal()
    try:
        return db.query(UsageLedger).count()
    finally:
        db.close()


def test_the_cap_is_eight_thousand_characters():
    assert ai_router.MAX_TEXT_CHARS == MAX_TEXT_CHARS


def test_over_long_text_is_refused_before_anything_is_held_or_sent(
        client, instance_token, monkeypatch):
    activate_entitlement()
    fwd = _watch(monkeypatch)
    gate_calls = []
    real_gate = usage.gate_and_reserve
    monkeypatch.setattr(usage, "gate_and_reserve",
                        lambda *a, **kw: gate_calls.append(a) or real_gate(*a, **kw))

    resp = _enrich(client, instance_token, "x" * (MAX_TEXT_CHARS + 1))

    assert resp.status_code == 413
    assert "8,000 characters" in resp.json()["detail"]
    assert fwd.calls == []          # nothing reached the provider
    assert gate_calls == []         # no reservation was even attempted
    assert _ledger_rows() == 0


def test_image_tasks_get_the_same_cap(client, instance_token, monkeypatch):
    activate_entitlement()
    fwd = _watch(monkeypatch)
    resp = _food(client, instance_token, text="x" * (MAX_TEXT_CHARS + 1))
    assert resp.status_code == 413
    assert fwd.calls == []
    assert _ledger_rows() == 0


def test_text_right_at_the_cap_is_accepted(client, instance_token,
                                           monkeypatch):
    activate_entitlement()
    fwd = _watch(monkeypatch)
    resp = _enrich(client, instance_token, "x" * MAX_TEXT_CHARS)
    assert resp.status_code == 200
    assert fwd.calls == ["enrich"]


def test_a_normal_enrich_still_succeeds(client, instance_token):
    activate_entitlement()
    info = ('{"product_name": "Kewpie Mayonnaise", "brands": "Kewpie", '
            '"categories_tags": ["en:condiments", "en:mayonnaises"]}')
    resp = _enrich(client, instance_token, info)
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["kind"] == "enrich"
    assert body["tokens"] == StubForwarder.STUB_TOKENS
    # The reservation was settled to the real count: one row, no leftover hold.
    db = SessionLocal()
    try:
        rows = db.query(UsageLedger).all()
        assert [(r.kind, r.tokens) for r in rows] == [
            ("enrich", StubForwarder.STUB_TOKENS)]
    finally:
        db.close()


def test_the_reservation_is_sized_from_the_request(client, instance_token,
                                                   monkeypatch):
    activate_entitlement()
    fwd = _watch(monkeypatch)
    floor = settings.proxy_reservation_tokens

    assert _enrich(client, instance_token, "x" * 300).status_code == 200
    assert _enrich(client, instance_token, "x" * 7500).status_code == 200
    assert _food(client, instance_token).status_code == 200
    assert _food(client, instance_token, text="x" * 3000).status_code == 200

    assert fwd.reserved == [
        [floor],        # a short text holds the configured floor
        [2500],         # about a token per three characters
        [1500],         # a photo holds its own share
        [2500],         # photo plus text
    ]


def test_reservation_estimate_is_never_below_the_floor(monkeypatch):
    reservation_estimate = ai_router.reservation_estimate
    monkeypatch.setattr(settings, "proxy_reservation_tokens", 1000)
    assert reservation_estimate("", has_image=False) == 1000
    assert reservation_estimate("x" * 2999, has_image=False) == 1000
    assert reservation_estimate("x" * 6000, has_image=False) == 2000
    assert reservation_estimate("", has_image=True) == 1500
    assert reservation_estimate("x" * 300, has_image=True) == 1600
    monkeypatch.setattr(settings, "proxy_reservation_tokens", 5000)
    assert reservation_estimate("x" * 6000, has_image=True) == 5000
