"""The redesigned account settings and admin: the sidebar + landing shell,
each panel's data, and that the reorganized layout keeps every existing
action wired to its endpoint."""
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import SessionLocal
from app.main import app

SIGNUP = {"email": "dan@example.com", "password": "hunter2222",
          "confirm_password": "hunter2222"}
LOGIN = {"email": "dan@example.com", "password": "hunter2222"}

ADMIN = {"email": "dan@example.com", "password": "hunter2222",
         "confirm_password": "hunter2222"}


# --- User settings: landing + sidebar ---------------------------------------

def test_settings_landing_shows_grouped_cards_and_sidebar(client):
    client.post("/signup", data=SIGNUP)
    page = client.get("/account").text
    # The landing renders the grouped category cards.
    assert 'class="ov-card"' in page
    for group in ("Your account", "Your kitchens", "Community"):
        assert group in page
    for card in ("Profile", "Security", "Plan and billing", "Kitchens",
                 "Share recipes"):
        assert card in page
    # The sidebar links to each panel (its show-pane control per section).
    for pane in ("profile", "security", "billing", "kitchens", "community"):
        assert f"showPane('{pane}')" in page
    assert 'id="pane-overview"' in page


def test_settings_requires_login(client):
    resp = client.get("/account", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# --- User settings: each panel renders its data -----------------------------

def test_profile_panel_shows_email_and_plan(client):
    client.post("/signup", data=SIGNUP)
    page = client.get("/account").text
    assert 'id="pane-profile"' in page
    assert "dan@example.com" in page
    assert "Free trial" in page  # plan label
    # Sign-out stays wired to the logout endpoint.
    assert 'action="/logout"' in page


def test_security_panel_wires_password_and_two_factor(client):
    client.post("/signup", data=SIGNUP)
    page = client.get("/account").text
    assert 'id="pane-security"' in page
    assert 'action="/account/password"' in page
    # 2FA is off for a fresh account, so the turn-on link shows.
    assert 'href="/account/2fa/setup"' in page


def test_billing_panel_shows_plan_and_billing_state(client, monkeypatch):
    client.post("/signup", data=SIGNUP)
    monkeypatch.setattr(settings, "stripe_checkout_url", "")
    page = client.get("/account").text
    assert 'id="pane-billing"' in page
    assert "scanning allowance" in page
    assert "Billing is not live yet" in page
    monkeypatch.setattr(settings, "stripe_checkout_url",
                        "https://buy.stripe.com/test_123")
    paid = client.get("/account").text
    assert "https://buy.stripe.com/test_123" in paid


def test_kitchens_panel_shows_web_address(client):
    session = client.post("/v1/accounts/signup",
                          json={"email": "dan@example.com",
                                "password": "hunter2222"}).json()["session_token"]
    code = client.post("/v1/pairing/code",
                       headers={"Authorization": f"Bearer {session}"})
    client.post("/v1/pairing/redeem",
                json={"code": code.json()["code"], "name": "Kitchen Pi"})
    db = SessionLocal()
    try:
        from app.models import Instance
        inst = db.query(Instance).filter_by(name="Kitchen Pi").first()
        inst.public_url = "https://kitchen-pi.forager.pantryraider.app"
        inst.app_version = "0.15.8"
        db.commit()
    finally:
        db.close()
    client.post("/login", data=LOGIN)
    page = client.get("/account").text
    assert 'id="pane-kitchens"' in page
    assert "Kitchen Pi" in page
    assert "https://kitchen-pi.forager.pantryraider.app" in page
    assert "0.15.8" in page  # version surfaced
    assert "Copy" in page
    # The remove action still points at the kitchen-remove endpoint.
    assert "/remove" in page


def test_community_panel_shows_upload_and_submissions(client):
    token = client.post("/v1/accounts/signup",
                        json={"email": "dan@example.com",
                              "password": "hunter2222"}).json()["session_token"]
    client.post("/v1/recipes",
                json={"title": "Weeknight Chili",
                      "ingredients": ["1 lb beans", "1 onion"],
                      "steps": ["Chop the onion.", "Simmer for an hour."],
                      "attribution": "From my grandmother's recipe box."},
                headers={"Authorization": f"Bearer {token}"})
    client.post("/login", data=LOGIN)
    page = client.get("/account").text
    assert 'id="pane-community"' in page
    assert 'href="/recipes/upload"' in page
    # The account's own submission shows with its status.
    assert "Weeknight Chili" in page


# --- Admin: landing + sidebar + gate ----------------------------------------

@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", "dan@example.com")
    c = TestClient(app)
    assert c.post("/signup", data=ADMIN,
                  follow_redirects=False).status_code == 303
    return c


def test_admin_landing_and_sidebar_render_for_admin(admin_client):
    page = admin_client.get("/admin")
    assert page.status_code == 200
    assert 'class="settings-nav"' in page.text
    # Sidebar links to each admin section.
    assert 'href="/admin/recipes"' in page.text
    assert 'href="/admin/stats"' in page.text
    # Landing card grid.
    assert 'class="ov-card"' in page.text
    assert "Recipe moderation" in page.text
    assert "Trials and stats" in page.text


def test_admin_shell_is_hidden_from_non_admin(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", "someone-else@example.com")
    assert client.get("/admin").status_code == 404
    client.post("/signup", data=ADMIN)  # signed in, not on the list
    assert client.get("/admin").status_code == 404
    assert client.get("/admin/stats").status_code == 404


# --- Admin: existing actions stay wired -------------------------------------

def _member_token(client, email="mia@example.com"):
    return client.post("/v1/accounts/signup",
                       json={"email": email, "password": "hunter2222"}
                       ).json()["session_token"]


def test_admin_moderation_actions_still_post(admin_client, client, monkeypatch):
    monkeypatch.setattr(settings, "recipe_require_approval", True)
    token = _member_token(client)
    rid = client.post("/v1/recipes",
                      json={"title": "Fresh Dish",
                            "ingredients": ["1 egg"], "steps": ["Cook it."],
                            "attribution": "Mine."},
                      headers={"Authorization": f"Bearer {token}"}).json()["id"]
    # The moderation page shows the pending recipe with its approve control.
    page = admin_client.get("/admin/recipes?status=pending").text
    assert "Fresh Dish" in page
    assert f'/admin/recipes/{rid}/moderate' in page

    approve = admin_client.post(f"/admin/recipes/{rid}/moderate",
                                data={"action": "approve"},
                                follow_redirects=False)
    assert approve.status_code == 303
    assert client.get(f"/v1/recipes/{rid}").status_code == 200

    reject = admin_client.post(f"/admin/recipes/{rid}/moderate",
                               data={"action": "reject"},
                               follow_redirects=False)
    assert reject.status_code == 303
    assert client.get(f"/v1/recipes/{rid}").status_code == 404


def test_admin_recipe_upload_toggle_still_posts(admin_client, client):
    client.post("/signup", data={"email": "eve@example.com",
                                 "password": "eviltwin99",
                                 "confirm_password": "eviltwin99"})
    from app.models import Account
    db = SessionLocal()
    account_id = db.query(Account).filter_by(email="eve@example.com").first().id
    db.close()

    detail = admin_client.get(f"/admin/accounts/{account_id}").text
    assert f'/admin/accounts/{account_id}/recipe-upload' in detail

    admin_client.post(f"/admin/accounts/{account_id}/recipe-upload",
                      data={"authorize": "1"})
    db = SessionLocal()
    try:
        assert db.get(Account, account_id).recipe_upload_authorized == 1
    finally:
        db.close()

    admin_client.post(f"/admin/accounts/{account_id}/recipe-upload",
                      data={"authorize": "0"})
    db = SessionLocal()
    try:
        assert db.get(Account, account_id).recipe_upload_authorized == 0
    finally:
        db.close()
