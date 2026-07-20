"""Recipe share links: creating, the public page, download, save, revoke,
inbox, reporting, and the never-reveal-account-existence guarantee."""

import json

from app.config import settings
from app.database import SessionLocal
from app.models import SharedRecipe
from app.routers import shares as shares_module
from app.routers.shares import (download_filename, image_url_ok,
                                og_description, share_email_bodies,
                                share_field_error, share_summary)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


VALID = {
    "title": "Weeknight Chili",
    "description": "A quick pot of chili.",
    "ingredients": ["1 lb beans", "1 onion", "2 tbsp chili powder"],
    "steps": ["Chop the onion.", "Simmer everything for an hour."],
    "attribution": "Dan's recipe box",
}


def _create(client, token, **overrides):
    body = {**VALID, **overrides}
    return client.post("/v1/recipes/shares", json=body, headers=_auth(token))


def _signup_second(client, email="pat@example.com"):
    resp = client.post("/v1/accounts/signup",
                       json={"email": email, "password": "hunter2222"})
    assert resp.status_code == 200
    return resp.json()["session_token"]


# --- Pure helpers -------------------------------------------------------------

def test_image_url_ok():
    assert image_url_ok("")
    assert image_url_ok("https://example.com/pic.jpg")
    assert image_url_ok("http://example.com/pic.jpg")
    assert not image_url_ok("javascript:alert(1)")
    assert not image_url_ok("data:image/png;base64,xxxx")
    assert not image_url_ok("/relative/path.jpg")


def test_share_field_error_caps():
    ok = share_field_error("T", "d", ["a"], ["b"], "credit", "")
    assert ok == ""
    assert "100" in share_field_error("T", "d", ["a"] * 101, ["b"],
                                      "credit", "")
    assert "100" in share_field_error("T", "d", ["a"], ["b"] * 101,
                                      "credit", "")
    assert "500" in share_field_error("T", "d", ["x" * 501], ["b"],
                                      "credit", "")
    assert "4000" in share_field_error("T", "d" * 4001, ["a"], ["b"],
                                       "credit", "")
    assert "http" in share_field_error("T", "d", ["a"], ["b"], "credit",
                                       "javascript:alert(1)")
    assert "credit" in share_field_error("T", "d", ["a"], ["b"], " ", "")


def test_share_summary_truncates():
    assert share_summary(["a", "b"]) == "- a\n- b"
    long = share_summary([str(i) for i in range(9)])
    assert "...and more" in long and "- 5" not in long


def test_share_email_bodies_escape_html():
    subject, text, html_body = share_email_bodies(
        title="Chili & Beans", url="https://x/r/t",
        message='<script>alert("hi")</script>',
        ingredients=["<b>beans</b>"])
    assert "Chili & Beans" in subject
    assert "<script>" in text  # plain text carries the raw note
    assert "<script>" not in html_body
    assert "&lt;script&gt;" in html_body
    assert "&lt;b&gt;beans&lt;/b&gt;" in html_body


def test_download_filename():
    assert download_filename("Weeknight Chili!") == "weeknight-chili.json"
    assert download_filename("") == "recipe.json"
    assert download_filename('a"b\r\nc') == "a-b-c.json"


def test_og_description_truncates():
    assert og_description("short", []) == "short"
    assert og_description("", ["a", "b"]) == "a, b"
    long = og_description("word " * 100, [])
    assert len(long) <= 160 and long.endswith("...")


# --- Creating shares ----------------------------------------------------------

def test_create_requires_login(client):
    assert client.post("/v1/recipes/shares", json=VALID).status_code == 401


def test_create_with_session_token(client, session_token):
    resp = _create(client, session_token)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["emailed"] is False
    assert body["url"].endswith(f"/r/{body['token']}")


def test_create_with_instance_token(client, instance_token):
    assert _create(client, instance_token).status_code == 200


def test_create_validates_caps(client, session_token):
    assert _create(client, session_token,
                   ingredients=["a"] * 101).status_code == 400
    assert _create(client, session_token,
                   image_url="javascript:alert(1)").status_code == 400
    assert _create(client, session_token, attribution="  ").status_code == 400
    # Nothing was stored on the rejected submissions.
    db = SessionLocal()
    try:
        assert db.query(SharedRecipe).count() == 0
    finally:
        db.close()


def test_create_rate_limited(client, session_token, monkeypatch):
    monkeypatch.setattr(settings, "share_create_rate_per_minute", 2)
    assert _create(client, session_token).status_code == 200
    assert _create(client, session_token).status_code == 200
    assert _create(client, session_token).status_code == 429


# --- Email path and recipient resolution --------------------------------------

def _capture_email(monkeypatch, result=True):
    sent = []

    def fake_send(to, subject, text, html_body=None):
        sent.append({"to": to, "subject": subject, "text": text,
                     "html": html_body})
        return result

    monkeypatch.setattr(shares_module, "send_email", fake_send)
    return sent


def test_email_to_sends_and_reports(client, session_token, monkeypatch):
    sent = _capture_email(monkeypatch)
    resp = _create(client, session_token, email_to="friend@example.com",
                   message="Try this one!")
    assert resp.status_code == 200
    assert resp.json()["emailed"] is True
    assert len(sent) == 1
    assert sent[0]["to"] == "friend@example.com"
    assert "shared a recipe with you: Weeknight Chili" in sent[0]["subject"]
    assert "Try this one!" in sent[0]["text"]
    assert resp.json()["url"] in sent[0]["text"]
    assert "1 lb beans" in sent[0]["text"]  # the short summary rides along


def test_email_failure_does_not_fail_share(client, session_token, monkeypatch):
    _capture_email(monkeypatch, result=False)
    resp = _create(client, session_token, email_to="friend@example.com")
    assert resp.status_code == 200
    assert resp.json()["emailed"] is False
    # The share itself exists regardless.
    assert client.get(f"/r/{resp.json()['token']}").status_code == 200


def test_email_send_rate_limited_per_hour(client, session_token, monkeypatch):
    sent = _capture_email(monkeypatch)
    monkeypatch.setattr(settings, "share_email_per_hour", 1)
    monkeypatch.setattr(settings, "share_create_rate_per_minute", 0)
    first = _create(client, session_token, email_to="a@example.com")
    second = _create(client, session_token, email_to="b@example.com")
    # Both shares are created; only the first drew from the email budget.
    assert first.json()["emailed"] is True
    assert second.json()["emailed"] is False
    assert len(sent) == 1


def test_recipient_resolution_never_leaks(client, session_token, monkeypatch):
    """A recipient with an account and one without get byte-identical
    response shapes and values (apart from the fresh token and url)."""
    sent = _capture_email(monkeypatch)
    _signup_second(client, "member@example.com")
    with_account = _create(client, session_token,
                           recipient="Member@Example.com")
    without_account = _create(client, session_token,
                              recipient="stranger@example.com")
    a, b = with_account.json(), without_account.json()
    assert with_account.status_code == without_account.status_code == 200
    assert set(a) == set(b)
    assert a["ok"] == b["ok"] and a["emailed"] == b["emailed"]
    # Behind the identical responses: the member got an inbox entry, the
    # stranger got the link by email.
    assert [m["to"] for m in sent] == ["stranger@example.com"]
    db = SessionLocal()
    try:
        rows = db.query(SharedRecipe).order_by(SharedRecipe.id).all()
        assert rows[0].recipient_account_id is not None
        assert rows[1].recipient_account_id is None
    finally:
        db.close()


# --- The public page -----------------------------------------------------------

def test_share_page_renders_escaped(client, session_token):
    resp = _create(client, session_token,
                   title='<script>alert("xss")</script>',
                   ingredients=['<img src=x onerror=alert(1)>'],
                   steps=["Stir & serve."],
                   attribution="<b>Me</b>")
    token = resp.json()["token"]
    page = client.get(f"/r/{token}")
    assert page.status_code == 200
    body = page.text
    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body
    assert "<img src=x" not in body
    assert "&lt;b&gt;Me&lt;/b&gt;" in body
    # The page carries social preview tags and the product footer link.
    assert 'property="og:title"' in body
    assert "https://pantryraider.app" in body


def test_share_page_counts_views(client, session_token):
    token = _create(client, session_token).json()["token"]
    client.get(f"/r/{token}")
    client.get(f"/r/{token}")
    db = SessionLocal()
    try:
        row = db.query(SharedRecipe).filter_by(token=token).first()
        assert row.view_count == 2
    finally:
        db.close()


def test_unknown_token_is_404(client):
    assert client.get("/r/not-a-real-token").status_code == 404


def test_download_emits_schema_org_json_ld(client, session_token):
    token = _create(client, session_token).json()["token"]
    resp = client.get(f"/r/{token}/download")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert "weeknight-chili.json" in resp.headers["content-disposition"]
    doc = json.loads(resp.content)
    assert doc["@context"] == "https://schema.org"
    assert doc["@type"] == "Recipe"
    assert doc["name"] == "Weeknight Chili"
    assert doc["recipeIngredient"] == VALID["ingredients"]
    assert [s["text"] for s in doc["recipeInstructions"]] == VALID["steps"]
    assert doc["author"]["name"] == VALID["attribution"]


# --- Save to my account ---------------------------------------------------------

def test_save_requires_session(client, session_token):
    token = _create(client, session_token).json()["token"]
    resp = client.post(f"/r/{token}/save", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_save_copies_into_viewer_inbox(client, session_token):
    token = _create(client, session_token).json()["token"]
    viewer_token = _signup_second(client)
    client.cookies.set("forager_session", viewer_token)
    resp = client.post(f"/r/{token}/save", follow_redirects=False)
    assert resp.status_code == 303
    # Saving twice keeps one copy.
    client.post(f"/r/{token}/save", follow_redirects=False)
    client.cookies.clear()
    inbox = client.get("/v1/recipes/shares/inbox",
                       headers=_auth(viewer_token)).json()
    assert len(inbox) == 1
    assert inbox[0]["title"] == "Weeknight Chili"
    assert inbox[0]["ingredients"] == VALID["ingredients"]
    # The saved copy survives the owner revoking the original link.
    client.post(f"/v1/recipes/shares/{token}/revoke",
                headers=_auth(session_token))
    inbox = client.get("/v1/recipes/shares/inbox",
                       headers=_auth(viewer_token)).json()
    assert len(inbox) == 1


# --- Listing, inbox, revoke ------------------------------------------------------

def test_list_own_shares(client, session_token):
    token = _create(client, session_token).json()["token"]
    client.get(f"/r/{token}")
    rows = client.get("/v1/recipes/shares", headers=_auth(session_token)).json()
    assert len(rows) == 1
    assert rows[0]["token"] == token
    assert rows[0]["revoked"] is False
    assert rows[0]["view_count"] == 1
    assert rows[0]["recipient_set"] is False


def test_inbox_lists_direct_shares(client, session_token):
    viewer_token = _signup_second(client, "member@example.com")
    _create(client, session_token, recipient="member@example.com")
    inbox = client.get("/v1/recipes/shares/inbox",
                       headers=_auth(viewer_token)).json()
    assert len(inbox) == 1
    assert inbox[0]["title"] == "Weeknight Chili"
    # The sender's own inbox stays empty.
    assert client.get("/v1/recipes/shares/inbox",
                      headers=_auth(session_token)).json() == []


def test_revoke_is_owner_only(client, session_token):
    token = _create(client, session_token).json()["token"]
    other_token = _signup_second(client)
    denied = client.post(f"/v1/recipes/shares/{token}/revoke",
                         headers=_auth(other_token))
    assert denied.status_code == 404  # same answer as a token that never was
    assert client.get(f"/r/{token}").status_code == 200
    ok = client.post(f"/v1/recipes/shares/{token}/revoke",
                     headers=_auth(session_token))
    assert ok.status_code == 200
    assert client.get(f"/r/{token}").status_code == 404
    assert client.get(f"/r/{token}/download").status_code == 404


# --- Reporting -------------------------------------------------------------------

def test_report_auto_revokes_at_threshold(client, session_token, monkeypatch):
    monkeypatch.setattr(settings, "recipe_report_hide_threshold", 2)
    token = _create(client, session_token).json()["token"]
    # Two distinct anonymous reporters (per-address identity).
    r1 = client.post(f"/r/{token}/report", follow_redirects=False,
                     headers={"x-forwarded-for": "10.0.0.1"})
    assert r1.status_code == 303
    assert client.get(f"/r/{token}").status_code == 200
    # The same person again does not count twice.
    client.post(f"/r/{token}/report", follow_redirects=False,
                headers={"x-forwarded-for": "10.0.0.1"})
    assert client.get(f"/r/{token}").status_code == 200
    client.post(f"/r/{token}/report", follow_redirects=False,
                headers={"x-forwarded-for": "10.0.0.2"})
    assert client.get(f"/r/{token}").status_code == 404


# --- Security headers -------------------------------------------------------------

def test_security_headers_on_portal_and_share_pages(client, session_token):
    token = _create(client, session_token).json()["token"]
    for path in ("/", "/login", "/signup", f"/r/{token}"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["x-content-type-options"] == "nosniff", path
        assert resp.headers["x-frame-options"] == "DENY", path
        assert (resp.headers["referrer-policy"]
                == "strict-origin-when-cross-origin"), path
        csp = resp.headers["content-security-policy"]
        assert "default-src 'self'" in csp, path
        assert "challenges.cloudflare.com" in csp, path


def test_account_page_renders_under_csp(client, session_token):
    """The signed-in account page (the heaviest inline-script page) still
    renders with the headers on, and shows the new shared-links section."""
    client.cookies.set("forager_session", session_token)
    resp = client.get("/account")
    assert resp.status_code == 200
    assert "content-security-policy" in resp.headers
    assert "Shared with you" in resp.text
    assert "My shared links" in resp.text
    client.cookies.clear()


# --- SSRF hardening (project evaluation 2026-07-13) --------------------------

def test_image_url_refuses_internal_hosts():
    # Every viewer's browser fetches the share image, so an internal address is
    # only ever a gadget for probing the viewer's network.
    from app.routers.shares import image_url_ok
    assert image_url_ok("")  # no image is fine
    assert image_url_ok("https://img.example.com/pie.jpg")
    for bad in ("http://127.0.0.1/x.png", "http://10.0.0.5/x.png",
                "http://192.168.1.170:9284/recipes/images/7",
                "http://[::1]/x.png", "http://localhost/x.png",
                "http://printer.local/x.png", "http://db.internal/x.png",
                "javascript:alert(1)", "data:image/png;base64,AAAA"):
        assert not image_url_ok(bad), bad


def test_anonymous_reports_store_a_hashed_key_not_the_address(
        client, session_token, monkeypatch):
    """An anonymous reporter's row carries "ip:" plus a short peppered hash;
    the raw address appears nowhere, and the same address still dedupes."""
    from app.database import SessionLocal
    from app.models import SharedRecipeReport
    from app.security import hash_ip

    monkeypatch.setattr(settings, "report_ip_pepper", "test-pepper")
    token = _create(client, session_token).json()["token"]
    for _ in range(2):  # the same address reports twice: one row
        client.post(f"/r/{token}/report", follow_redirects=False,
                    headers={"x-forwarded-for": "203.0.113.9"})
    db = SessionLocal()
    try:
        rows = db.query(SharedRecipeReport).all()
        assert len(rows) == 1
        key = rows[0].reporter_key
        assert key == f"ip:{hash_ip('203.0.113.9', 'test-pepper')}"
        assert "203.0.113.9" not in key
        assert len(key) == len("ip:") + 16
    finally:
        db.close()
