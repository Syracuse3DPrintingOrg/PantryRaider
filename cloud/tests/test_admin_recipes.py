"""The community-recipe moderation panel: gate, listing and filters, report
reasons, status transitions and their effect on the public listing, the
require-approval moderation mode, and the admin overview summary."""
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import CommunityRecipe
from app.routers.admin import (apply_recipe_filter, normalize_recipe_filter,
                               recipe_admin_row)

ADMIN = {"email": "dan@example.com", "password": "hunter2222",
         "confirm_password": "hunter2222"}

VALID = {
    "title": "Weeknight Chili",
    "ingredients": ["1 lb beans", "1 onion"],
    "steps": ["Chop the onion.", "Simmer for an hour."],
    "attribution": "From my grandmother's recipe box.",
}


@pytest.fixture
def admin_client(monkeypatch):
    """A browser signed in as the allowlisted admin."""
    monkeypatch.setattr(settings, "admin_emails", "dan@example.com")
    c = TestClient(app)
    assert c.post("/signup", data=ADMIN, follow_redirects=False).status_code == 303
    return c


def _member_token(client, email="mia@example.com"):
    """A signed-up member's bearer token, for the recipe write path."""
    resp = client.post("/v1/accounts/signup",
                       json={"email": email, "password": "hunter2222"})
    return resp.json()["session_token"]


def _submit(client, token, **overrides):
    body = {**VALID, **overrides}
    return client.post("/v1/recipes", json=body,
                       headers={"Authorization": f"Bearer {token}"})


def _set_status(recipe_id, status):
    db = SessionLocal()
    try:
        db.get(CommunityRecipe, recipe_id).status = status
        db.commit()
    finally:
        db.close()


# --- Pure helpers -----------------------------------------------------------

def test_normalize_recipe_filter():
    assert normalize_recipe_filter("pending") == "pending"
    assert normalize_recipe_filter("reported") == "reported"
    assert normalize_recipe_filter("bogus") == "all"
    assert normalize_recipe_filter("") == "all"


def test_recipe_admin_row_exposes_moderation_fields():
    row = recipe_admin_row(
        CommunityRecipe(id=3, title="T", attribution="a",
                        submitter_account_id=9, rating_count=2, rating_sum=9,
                        report_count=4, status="hidden", created_at="2026-01-01",
                        updated_at="2026-01-02"),
        "mia@example.com", ["spam", "", "wrong"])
    assert row["submitter"] == "mia@example.com"
    assert row["report_count"] == 4
    assert row["status"] == "hidden"
    assert row["average_rating"] == 4.5
    # Blank reasons are dropped.
    assert row["reasons"] == ["spam", "wrong"]


def test_recipe_admin_row_falls_back_to_account_id():
    row = recipe_admin_row(
        CommunityRecipe(id=1, title="T", attribution="", submitter_account_id=7,
                        rating_count=0, rating_sum=0, report_count=0,
                        status="approved", created_at="", updated_at=""),
        "")
    assert row["submitter"] == "#7"


def test_apply_recipe_filter_reported_and_status(client):
    token = _member_token(client)
    a = _submit(client, token, title="A").json()["id"]
    b = _submit(client, token, title="B").json()["id"]
    _set_status(b, "hidden")
    db = SessionLocal()
    try:
        db.get(CommunityRecipe, a).report_count = 3
        db.commit()
        reported = apply_recipe_filter(db.query(CommunityRecipe), "reported").all()
        assert [r.id for r in reported] == [a]
        hidden = apply_recipe_filter(db.query(CommunityRecipe), "hidden").all()
        assert [r.id for r in hidden] == [b]
        everything = apply_recipe_filter(db.query(CommunityRecipe), "all").all()
        assert {r.id for r in everything} == {a, b}
    finally:
        db.close()


# --- Gate --------------------------------------------------------------------

def test_moderation_page_is_404_for_anonymous_and_non_admin(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", "someone-else@example.com")
    assert client.get("/admin/recipes").status_code == 404
    client.post("/signup", data=ADMIN)  # signed in, not on the list
    assert client.get("/admin/recipes").status_code == 404
    assert client.post("/admin/recipes/1/moderate",
                       data={"action": "approve"}).status_code == 404
    assert client.post("/admin/recipes/1/delete").status_code == 404


def test_moderation_page_loads_for_admin(admin_client):
    page = admin_client.get("/admin/recipes")
    assert page.status_code == 200
    assert "Community recipes" in page.text


# --- Listing, filters, report reasons ---------------------------------------

def test_listing_filters_by_status_and_shows_report_reasons(admin_client, client):
    token = _member_token(client)
    approved = _submit(client, token, title="Approved Dish").json()["id"]
    pending = _submit(client, token, title="Pending Dish").json()["id"]
    _set_status(pending, "pending")

    # Flag the approved one so it turns up in the reported view with reasons.
    client.post(f"/v1/recipes/{approved}/report", json={"reason": "looks off"},
                headers={"Authorization": f"Bearer {token}"})

    approved_only = admin_client.get("/admin/recipes?status=approved").text
    assert "Approved Dish" in approved_only
    assert "Pending Dish" not in approved_only

    pending_only = admin_client.get("/admin/recipes?status=pending").text
    assert "Pending Dish" in pending_only
    assert "Approved Dish" not in pending_only

    reported = admin_client.get("/admin/recipes?status=reported").text
    assert "Approved Dish" in reported
    assert "looks off" in reported  # the flag reason is surfaced
    assert "mia@example.com" in reported  # submitter is shown to the moderator


# --- Transitions -------------------------------------------------------------

def test_hide_removes_from_public_listing(admin_client, client):
    token = _member_token(client)
    rid = _submit(client, token, title="Public Dish").json()["id"]
    assert "Public Dish" in {c["title"]
                             for c in client.get("/v1/recipes").json()["recipes"]}

    admin_client.post(f"/admin/recipes/{rid}/moderate", data={"action": "hide"})
    titles = {c["title"] for c in client.get("/v1/recipes").json()["recipes"]}
    assert "Public Dish" not in titles
    _set_status_check(rid, "hidden")

    # Restore brings it back to the browser.
    admin_client.post(f"/admin/recipes/{rid}/moderate", data={"action": "unhide"})
    titles = {c["title"] for c in client.get("/v1/recipes").json()["recipes"]}
    assert "Public Dish" in titles


def test_approve_and_reject_transitions(admin_client, client, monkeypatch):
    monkeypatch.setattr(settings, "recipe_require_approval", True)
    token = _member_token(client)
    rid = _submit(client, token, title="Fresh Dish").json()["id"]
    # Require-approval: it starts pending and is not public yet.
    assert client.get(f"/v1/recipes/{rid}").status_code == 404

    admin_client.post(f"/admin/recipes/{rid}/moderate", data={"action": "approve"})
    assert client.get(f"/v1/recipes/{rid}").status_code == 200
    _set_status_check(rid, "approved")

    admin_client.post(f"/admin/recipes/{rid}/moderate", data={"action": "reject"})
    assert client.get(f"/v1/recipes/{rid}").status_code == 404
    _set_status_check(rid, "rejected")


def test_unknown_action_is_rejected(admin_client, client):
    token = _member_token(client)
    rid = _submit(client, token).json()["id"]
    assert admin_client.post(f"/admin/recipes/{rid}/moderate",
                             data={"action": "explode"}).status_code == 400


def test_delete_removes_the_recipe(admin_client, client):
    token = _member_token(client)
    rid = _submit(client, token, title="Doomed Dish").json()["id"]
    client.post(f"/v1/recipes/{rid}/report", json={"reason": "bad"},
                headers={"Authorization": f"Bearer {token}"})

    resp = admin_client.post(f"/admin/recipes/{rid}/delete",
                             follow_redirects=False)
    assert resp.status_code == 303
    db = SessionLocal()
    try:
        from app.models import RecipeReport
        assert db.get(CommunityRecipe, rid) is None
        assert db.query(RecipeReport).filter_by(recipe_id=rid).count() == 0
    finally:
        db.close()
    assert client.get(f"/v1/recipes/{rid}").status_code == 404


# --- Moderation mode setting -------------------------------------------------

def test_require_approval_setting_controls_new_status(client, monkeypatch):
    token = _member_token(client)
    # Default (off): a new recipe is approved and immediately public.
    rid = _submit(client, token, title="Auto Dish").json()["id"]
    db = SessionLocal()
    try:
        assert db.get(CommunityRecipe, rid).status == "approved"
    finally:
        db.close()

    # Flipped on: the next submission lands pending.
    monkeypatch.setattr(settings, "recipe_require_approval", True)
    rid2 = _submit(client, token, title="Held Dish").json()["id"]
    db = SessionLocal()
    try:
        assert db.get(CommunityRecipe, rid2).status == "pending"
    finally:
        db.close()


# --- Admin overview summary --------------------------------------------------

def test_overview_shows_recipe_counts(admin_client, client):
    token = _member_token(client)
    approved = _submit(client, token, title="Vis Dish").json()["id"]
    pending = _submit(client, token, title="Wait Dish").json()["id"]
    _set_status(pending, "pending")
    client.post(f"/v1/recipes/{approved}/report", json={"reason": "hmm"},
                headers={"Authorization": f"Bearer {token}"})

    overview = admin_client.get("/admin").text
    assert "Community recipes" in overview
    assert "/admin/recipes?status=pending" in overview
    assert "/admin/recipes?status=reported" in overview


def _set_status_check(recipe_id, expected):
    db = SessionLocal()
    try:
        assert db.get(CommunityRecipe, recipe_id).status == expected
    finally:
        db.close()
