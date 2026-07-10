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
    return SimpleNamespace(**kw)


def _inst(last_seen):
    return SimpleNamespace(last_seen_at=last_seen)


def test_can_upload_logged_out():
    assert can_upload(None, [], NOW) is False


def test_can_upload_active_kitchen_recent():
    recent = (NOW - timedelta(days=3)).isoformat(timespec="seconds")
    assert can_upload(_acct(), [_inst(recent)], NOW, active_days=30) is True


def test_can_upload_kitchen_too_old():
    stale = (NOW - timedelta(days=90)).isoformat(timespec="seconds")
    assert can_upload(_acct(), [_inst(stale)], NOW, active_days=30) is False


def test_can_upload_no_kitchens():
    assert can_upload(_acct(), [], NOW) is False


def test_can_upload_manual_authorization():
    # No kitchen at all, but the hand-set flag lets it through.
    assert can_upload(_acct(recipe_upload_authorized=1), [], NOW) is True


def test_can_upload_disabled_account():
    recent = (NOW - timedelta(days=1)).isoformat(timespec="seconds")
    assert can_upload(_acct(disabled=1, recipe_upload_authorized=1),
                      [_inst(recent)], NOW) is False


def test_can_upload_ignores_never_seen_kitchen():
    assert can_upload(_acct(), [_inst("")], NOW) is False


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
    assert "actively using Pantry Raider" in page.text
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
                      data={"authorize": "1"}, follow_redirects=False)
    assert resp.status_code == 303
    assert _account().recipe_upload_authorized == 1

    # And the member can now reach the upload form instead of the gate.
    page = member.get("/recipes/upload")
    assert "How would you like to add it?" in page.text

    # Revoking turns it back off.
    admin.post(f"/admin/accounts/{acct.id}/recipe-upload",
               data={"authorize": "0"}, follow_redirects=False)
    assert _account().recipe_upload_authorized == 0


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
