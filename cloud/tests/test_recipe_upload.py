"""The portal "share a recipe" upload path: the can_upload gate, the three
input methods formatted through a mocked AI, the draft-then-confirm flow, spam
and rate limits, and the admin authorization toggle.

Everything AI, PDF, and HTTP is mocked or built in memory; no network."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import ratelimit
from app.config import settings
from app.database import SessionLocal
from app.forwarder import ForwardResult
from app.main import app
from app.models import Account, CommunityRecipe
from app.recipe_format import (clean_text, extract_pdf_text,
                               format_recipe_draft, parse_recipe_draft)
from app.routers import recipe_upload
from app.routers.recipes import can_upload


NOW = datetime(2026, 7, 6, tzinfo=timezone.utc)


# --- can_upload gate (pure) -------------------------------------------------

def _acct(**kw):
    kw.setdefault("disabled", 0)
    kw.setdefault("recipe_upload_authorized", 0)
    kw.setdefault("recipe_upload_blocked", 0)
    return SimpleNamespace(**kw)


def _inst(last_seen):
    return SimpleNamespace(last_seen_at=last_seen)


SEEN = (NOW - timedelta(days=3)).isoformat(timespec="seconds")


def test_can_upload_logged_out():
    assert can_upload(None, []) is False


def test_can_upload_automatic_with_checked_in_kitchen():
    assert can_upload(_acct(), [_inst(SEEN)]) is True


def test_can_upload_kitchen_seen_long_ago_still_counts():
    # A linked kitchen that has checked in vouches for its owner; there is
    # no recency window to fall out of.
    old = (NOW - timedelta(days=400)).isoformat(timespec="seconds")
    assert can_upload(_acct(), [_inst(old)]) is True


def test_can_upload_no_kitchens():
    assert can_upload(_acct(), []) is False


def test_can_upload_manual_authorization():
    # No kitchen at all, but the hand-set flag lets it through.
    assert can_upload(_acct(recipe_upload_authorized=1), []) is True


def test_can_upload_disabled_account():
    assert can_upload(_acct(disabled=1, recipe_upload_authorized=1),
                      [_inst(SEEN)]) is False


def test_can_upload_ignores_never_seen_kitchen():
    assert can_upload(_acct(), [_inst("")]) is False


def test_can_upload_admin_block_wins():
    # The after-abuse switch refuses uploads even with a checked-in kitchen
    # and even alongside the manual allow flag.
    assert can_upload(_acct(recipe_upload_blocked=1), [_inst(SEEN)]) is False
    assert can_upload(_acct(recipe_upload_blocked=1,
                            recipe_upload_authorized=1), [_inst(SEEN)]) is False


# --- Pure format helpers ----------------------------------------------------

def test_clean_text_collapses_blank_runs():
    assert clean_text("a\n\n\n\nb   \n") == "a\n\nb"


def test_parse_recipe_draft_strips_fence():
    raw = "```json\n" + json.dumps({
        "title": "Chili", "ingredients": ["beans", " "], "steps": ["cook"]}) + "\n```"
    draft = parse_recipe_draft(raw)
    assert draft == {"title": "Chili", "ingredients": ["beans"], "steps": ["cook"]}


def test_parse_recipe_draft_bad_json():
    assert parse_recipe_draft("not json") == {
        "title": "", "ingredients": [], "steps": []}


def test_extract_pdf_text_from_text_pdf():
    pdf = _make_text_pdf("Weeknight Chili")
    assert "Weeknight Chili" in extract_pdf_text(pdf)


def test_extract_pdf_text_scanned_returns_empty():
    # Not a real PDF (stands in for a scan with no text layer): fails soft.
    assert extract_pdf_text(b"%PDF-1.4 not really a pdf") == ""


class FakeForwarder:
    """A stand-in AI provider that records its call and returns a chosen draft."""

    def __init__(self, draft):
        self._draft = draft
        self.calls = []

    async def forward(self, kind, image_data, mime_type, text):
        self.calls.append({"kind": kind, "image": image_data,
                           "mime": mime_type, "text": text})
        return ForwardResult(result={"text": json.dumps(self._draft)}, tokens=42)


def test_format_recipe_draft_mocked_provider():
    fake = FakeForwarder({"title": "Soup", "ingredients": ["water"],
                          "steps": ["boil"]})
    draft = asyncio.run(format_recipe_draft(fake, text="some recipe text"))
    assert draft == {"title": "Soup", "ingredients": ["water"], "steps": ["boil"]}
    assert fake.calls[0]["kind"] == "recipe"
    assert fake.calls[0]["text"] == "some recipe text"


# --- Endpoint helpers -------------------------------------------------------

CREDS = {"email": "cook@example.com", "password": "hunter2222",
         "confirm_password": "hunter2222"}


@pytest.fixture
def portal_client():
    """A browser signed in through the portal (carries the session cookie)."""
    c = TestClient(app)
    assert c.post("/signup", data=CREDS, follow_redirects=False).status_code == 303
    return c


def _account(email="cook@example.com"):
    db = SessionLocal()
    try:
        return db.query(Account).filter_by(email=email).first()
    finally:
        db.close()


def _authorize(email="cook@example.com"):
    """Hand-authorize the account so the gate opens without a kitchen."""
    db = SessionLocal()
    try:
        acct = db.query(Account).filter_by(email=email).first()
        acct.recipe_upload_authorized = 1
        db.commit()
        return acct.id
    finally:
        db.close()


def _recipe_count():
    db = SessionLocal()
    try:
        return db.query(CommunityRecipe).count()
    finally:
        db.close()


def _fake_ai(monkeypatch, draft):
    fake = FakeForwarder(draft)
    monkeypatch.setattr(recipe_upload, "get_forwarder", lambda: fake)
    return fake


MANUAL = {"method": "manual", "title": "Grandma's Chili",
          "ingredients": "1 lb beans\n1 onion", "steps": "Chop.\nSimmer.",
          "attribution": "From my grandmother."}


# --- Endpoint: auth and gate ------------------------------------------------

def test_upload_page_requires_login():
    c = TestClient(app)
    resp = c.get("/recipes/upload", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_upload_blocked_without_kitchen(portal_client, monkeypatch):
    _fake_ai(monkeypatch, {"title": "x", "ingredients": ["a"], "steps": ["b"]})
    # No kitchen, not authorized: the page shows the gate and a POST is refused.
    page = portal_client.get("/recipes/upload")
    assert "linked Pantry Raider" in page.text
    resp = portal_client.post("/recipes/upload", data=MANUAL)
    assert resp.status_code == 403
    assert _recipe_count() == 0


def test_upload_requires_attribution(portal_client, monkeypatch):
    _authorize()
    _fake_ai(monkeypatch, {"title": "x", "ingredients": ["a"], "steps": ["b"]})
    body = {**MANUAL, "attribution": "   "}
    resp = portal_client.post("/recipes/upload", data=body)
    assert resp.status_code == 400
    assert "credit line" in resp.text
    assert _recipe_count() == 0


# --- Endpoint: manual entry -> draft, not saved -----------------------------

def test_manual_entry_returns_draft_not_saved(portal_client, monkeypatch):
    _authorize()
    fake = _fake_ai(monkeypatch, {"title": "AI Chili",
                                  "ingredients": ["1 lb beans"],
                                  "steps": ["Simmer."]})
    resp = portal_client.post("/recipes/upload", data=MANUAL)
    assert resp.status_code == 200
    assert "AI Chili" in resp.text            # the review draft is shown
    assert "Share this recipe" in resp.text   # confirm button
    assert fake.calls[0]["kind"] == "recipe"
    assert fake.calls[0]["image"] is None     # text path, not vision
    assert _recipe_count() == 0               # nothing saved until confirm


def test_confirm_saves_community_recipe(portal_client):
    account_id = _authorize()
    confirm = {"title": "AI Chili", "ingredients": "1 lb beans\n1 onion",
               "steps": "Chop.\nSimmer.", "attribution": "From my grandmother."}
    resp = portal_client.post("/recipes/upload/confirm", data=confirm,
                              follow_redirects=False)
    assert resp.status_code == 303
    db = SessionLocal()
    try:
        row = db.query(CommunityRecipe).one()
        assert row.submitter_account_id == account_id
        assert row.title == "AI Chili"
        assert row.status == "approved"
        assert json.loads(row.ingredients) == ["1 lb beans", "1 onion"]
        assert row.attribution == "From my grandmother."
    finally:
        db.close()


def test_confirm_respects_require_approval(portal_client, monkeypatch):
    _authorize()
    monkeypatch.setattr(settings, "recipe_require_approval", True)
    confirm = {"title": "Pending Chili", "ingredients": "beans",
               "steps": "cook", "attribution": "Me"}
    portal_client.post("/recipes/upload/confirm", data=confirm,
                       follow_redirects=False)
    db = SessionLocal()
    try:
        assert db.query(CommunityRecipe).one().status == "pending"
    finally:
        db.close()


def test_confirm_requires_attribution(portal_client):
    _authorize()
    confirm = {"title": "Chili", "ingredients": "beans", "steps": "cook",
               "attribution": "  "}
    resp = portal_client.post("/recipes/upload/confirm", data=confirm)
    assert resp.status_code == 400
    assert _recipe_count() == 0


# --- Endpoint: PDF ----------------------------------------------------------

def test_pdf_upload_returns_draft(portal_client, monkeypatch):
    _authorize()
    fake = _fake_ai(monkeypatch, {"title": "PDF Chili",
                                  "ingredients": ["beans"], "steps": ["cook"]})
    monkeypatch.setattr(recipe_upload, "extract_pdf_text",
                        lambda data: "Chili\nbeans\ncook")
    resp = portal_client.post(
        "/recipes/upload",
        data={"method": "pdf", "attribution": "A cookbook"},
        files={"pdf": ("chili.pdf", b"%PDF-1.4 fake", "application/pdf")})
    assert resp.status_code == 200
    assert "PDF Chili" in resp.text
    assert fake.calls[0]["text"] == "Chili\nbeans\ncook"
    assert _recipe_count() == 0


def test_scanned_pdf_shows_friendly_message(portal_client, monkeypatch):
    _authorize()
    _fake_ai(monkeypatch, {"title": "x", "ingredients": ["a"], "steps": ["b"]})
    monkeypatch.setattr(recipe_upload, "extract_pdf_text", lambda data: "")
    resp = portal_client.post(
        "/recipes/upload",
        data={"method": "pdf", "attribution": "A cookbook"},
        files={"pdf": ("scan.pdf", b"%PDF-1.4 scan", "application/pdf")})
    assert resp.status_code == 400
    assert "readable text" in resp.text
    assert "photo" in resp.text
    assert _recipe_count() == 0


# --- Endpoint: photo (vision) -----------------------------------------------

def test_photo_upload_uses_vision(portal_client, monkeypatch):
    _authorize()
    fake = _fake_ai(monkeypatch, {"title": "Photo Chili",
                                  "ingredients": ["beans"], "steps": ["cook"]})
    resp = portal_client.post(
        "/recipes/upload",
        data={"method": "photo", "attribution": "A recipe card"},
        files={"photo": ("card.jpg", b"\xff\xd8\xff\xe0jpegbytes", "image/jpeg")})
    assert resp.status_code == 200
    assert "Photo Chili" in resp.text
    # The image went to the provider (vision path), not text.
    assert fake.calls[0]["image"] == b"\xff\xd8\xff\xe0jpegbytes"
    assert fake.calls[0]["mime"] == "image/jpeg"
    assert _recipe_count() == 0


def test_photo_rejects_bad_type(portal_client, monkeypatch):
    _authorize()
    _fake_ai(monkeypatch, {"title": "x", "ingredients": ["a"], "steps": ["b"]})
    resp = portal_client.post(
        "/recipes/upload",
        data={"method": "photo", "attribution": "A recipe card"},
        files={"photo": ("notes.txt", b"hello", "text/plain")})
    assert resp.status_code == 400
    assert "photo" in resp.text.lower()


# --- Spam and rate limits ---------------------------------------------------

def test_honeypot_rejects_and_stores_nothing(portal_client, monkeypatch):
    _authorize()
    _fake_ai(monkeypatch, {"title": "x", "ingredients": ["a"], "steps": ["b"]})
    resp = portal_client.post("/recipes/upload",
                              data={**MANUAL, "website": "http://spam.example"})
    assert resp.status_code == 400
    assert _recipe_count() == 0


def test_upload_rate_limited(portal_client, monkeypatch):
    _authorize()
    monkeypatch.setattr(settings, "recipe_upload_rate_per_minute", 2)
    ratelimit.reset()
    _fake_ai(monkeypatch, {"title": "Chili", "ingredients": ["beans"],
                           "steps": ["cook"]})
    assert portal_client.post("/recipes/upload", data=MANUAL).status_code == 200
    assert portal_client.post("/recipes/upload", data=MANUAL).status_code == 200
    third = portal_client.post("/recipes/upload", data=MANUAL)
    assert third.status_code == 429


# --- Admin authorization toggle ---------------------------------------------

def test_admin_can_authorize_recipe_upload(monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", "admin@example.com")
    admin = TestClient(app)
    admin.post("/signup", data={"email": "admin@example.com",
                                "password": "hunter2222",
                                "confirm_password": "hunter2222"},
               follow_redirects=False)
    # A separate member with no kitchen.
    member = TestClient(app)
    member.post("/signup", data=CREDS, follow_redirects=False)
    acct = _account()
    assert acct.recipe_upload_authorized == 0

    resp = admin.post(f"/admin/accounts/{acct.id}/recipe-upload",
                      data={"mode": "allow"}, follow_redirects=False)
    assert resp.status_code == 303
    assert _account().recipe_upload_authorized == 1

    # And the member can now reach the upload form instead of the gate.
    page = member.get("/recipes/upload")
    assert "How would you like to add it?" in page.text

    # Back to automatic turns it off again (no kitchen, so gated).
    admin.post(f"/admin/accounts/{acct.id}/recipe-upload",
               data={"mode": "auto"}, follow_redirects=False)
    assert _account().recipe_upload_authorized == 0
    assert _account().recipe_upload_blocked == 0


def test_kitchen_that_checked_in_unlocks_upload_automatically(monkeypatch):
    """A linked kitchen that has checked in opens the upload form with no
    admin action at all: the automatic rule Dan expects on a fresh install."""
    member = TestClient(app)
    member.post("/signup", data=CREDS, follow_redirects=False)
    acct_id = _account().id
    db = SessionLocal()
    try:
        from app.models import Instance
        db.add(Instance(token_hash="x" * 64, account_id=acct_id,
                        name="Kitchen Pi", last_seen_at="2026-07-01T00:00:00+00:00",
                        created_at="2026-06-01T00:00:00+00:00"))
        db.commit()
    finally:
        db.close()
    page = member.get("/recipes/upload")
    assert "How would you like to add it?" in page.text


def test_admin_block_beats_a_checked_in_kitchen(monkeypatch):
    """Blocking from the admin panel wins even while the account has a
    kitchen checking in, and posting straight at the form is refused too."""
    monkeypatch.setattr(settings, "admin_emails", "admin@example.com")
    admin = TestClient(app)
    admin.post("/signup", data={"email": "admin@example.com",
                                "password": "hunter2222",
                                "confirm_password": "hunter2222"},
               follow_redirects=False)
    member = TestClient(app)
    member.post("/signup", data=CREDS, follow_redirects=False)
    acct_id = _account().id
    db = SessionLocal()
    try:
        from app.models import Instance
        db.add(Instance(token_hash="y" * 64, account_id=acct_id,
                        name="Kitchen Pi", last_seen_at="2026-07-01T00:00:00+00:00",
                        created_at="2026-06-01T00:00:00+00:00"))
        db.commit()
    finally:
        db.close()

    admin.post(f"/admin/accounts/{acct_id}/recipe-upload",
               data={"mode": "block"}, follow_redirects=False)
    assert _account().recipe_upload_blocked == 1

    page = member.get("/recipes/upload")
    assert "linked Pantry Raider" in page.text  # the gate, not the form
    assert member.post("/recipes/upload", data=MANUAL).status_code == 403

    # Back to automatic restores the kitchen rule.
    admin.post(f"/admin/accounts/{acct_id}/recipe-upload",
               data={"mode": "auto"}, follow_redirects=False)
    assert _account().recipe_upload_blocked == 0
    assert "How would you like to add it?" in member.get("/recipes/upload").text


# --- A minimal text PDF for the extraction test -----------------------------

def _make_text_pdf(text: str) -> bytes:
    """A tiny but valid single-page PDF containing one line of text, with a
    correct xref table so pypdf reads it without reconstruction."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
    ]
    stream = ("BT /F1 24 Tf 72 700 Td (" + text + ") Tj ET").encode("latin-1")
    objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += (b"trailer\n<< /Size " + str(len(objs) + 1).encode()
            + b" /Root 1 0 R >>\nstartxref\n" + str(xref_pos).encode()
            + b"\n%%EOF")
    return bytes(out)
