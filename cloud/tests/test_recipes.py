"""Forager community recipes: sharing, browsing, download, ratings, reports,
and the spam-protection layers on the write path."""


from app import ratelimit
from app.database import SessionLocal
from app.models import CommunityRecipe, RecipeRating, RecipeReport
from app.routers.recipes import (attribution_ok, average_rating, clamp_stars,
                                  normalize_lines, recipe_card)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


VALID = {
    "title": "Weeknight Chili",
    "description": "A quick pot of chili.",
    "ingredients": ["1 lb beans", "1 onion", "2 tbsp chili powder"],
    "steps": ["Chop the onion.", "Simmer everything for an hour."],
    "attribution": "From my grandmother's recipe box.",
}


def _submit(client, token, **overrides):
    body = {**VALID, **overrides}
    return client.post("/v1/recipes", json=body, headers=_auth(token))


# --- Pure helpers -----------------------------------------------------------

def test_clamp_stars():
    assert clamp_stars(3) == 3
    assert clamp_stars(0) == 1
    assert clamp_stars(9) == 5
    assert clamp_stars("bad") == 1


def test_average_rating():
    assert average_rating(0, 0) == 0.0
    assert average_rating(2, 9) == 4.5
    assert average_rating(3, 10) == 3.3


def test_normalize_lines_accepts_list_and_text():
    assert normalize_lines(["a", " ", "b "]) == ["a", "b"]
    assert normalize_lines("a\n\nb\n") == ["a", "b"]
    assert normalize_lines(None) == []


def test_attribution_ok():
    assert attribution_ok("Aunt May")
    assert not attribution_ok("   ")
    assert not attribution_ok("")


def test_recipe_card_hides_report_count():
    card = recipe_card(CommunityRecipe(
        id=1, title="T", description="d", image_url="", attribution="a",
        rating_count=0, rating_sum=0, report_count=7))
    assert "report_count" not in card


# --- Auth and submission ----------------------------------------------------

def test_submit_requires_login(client):
    assert client.post("/v1/recipes", json=VALID).status_code == 401


def test_submit_requires_attribution(client, session_token):
    resp = _submit(client, session_token, attribution="   ")
    assert resp.status_code == 400
    assert "credit" in resp.json()["detail"].lower()
    # Nothing was stored on the rejected submission.
    db = SessionLocal()
    try:
        assert db.query(CommunityRecipe).count() == 0
    finally:
        db.close()


def test_submit_stores_and_returns_id(client, session_token):
    resp = _submit(client, session_token)
    assert resp.status_code == 200
    rid = resp.json()["id"]
    assert isinstance(rid, int)
    db = SessionLocal()
    try:
        row = db.get(CommunityRecipe, rid)
        assert row.title == "Weeknight Chili"
        assert row.status == "approved"
        assert row.attribution == "From my grandmother's recipe box."
    finally:
        db.close()


def test_app_instance_token_can_submit(client, instance_token):
    """The app's linked credential is a valid sign-in for sharing."""
    assert _submit(client, instance_token).status_code == 200


def test_admin_block_refuses_app_submit(client, instance_token):
    """An admin's after-abuse block covers the app's JSON submit path too,
    not just the portal upload form."""
    from app.models import Account
    db = SessionLocal()
    try:
        account = db.query(Account).first()
        account.recipe_upload_blocked = 1
        db.commit()
    finally:
        db.close()
    resp = _submit(client, instance_token)
    assert resp.status_code == 403
    assert "turned off" in resp.json()["detail"]


def test_free_tier_can_submit_and_download(client, session_token):
    """No paid plan, no active trial: sharing and downloading still work."""
    from tests.conftest import expire_trial
    expire_trial()
    rid = _submit(client, session_token).json()["id"]
    got = client.get(f"/v1/recipes/{rid}", headers=_auth(session_token))
    assert got.status_code == 200
    assert got.json()["title"] == "Weeknight Chili"


# --- Listing and search -----------------------------------------------------

def test_list_returns_only_public_and_matches_search(client, session_token):
    _submit(client, session_token, title="Weeknight Chili")
    _submit(client, session_token, title="Lemon Cake",
            ingredients=["flour", "lemon"], steps=["Bake it."])
    # Hide one directly to prove non-public recipes stay out of the browser.
    db = SessionLocal()
    try:
        hidden = _submit(client, session_token, title="Hidden One").json()
        row = db.get(CommunityRecipe, hidden["id"])
        row.status = "hidden"
        db.commit()
    finally:
        db.close()

    everything = client.get("/v1/recipes").json()
    titles = {c["title"] for c in everything["recipes"]}
    assert "Hidden One" not in titles
    assert {"Weeknight Chili", "Lemon Cake"} <= titles

    by_title = client.get("/v1/recipes", params={"q": "chili"}).json()
    assert [c["title"] for c in by_title["recipes"]] == ["Weeknight Chili"]

    by_ingredient = client.get("/v1/recipes", params={"q": "lemon"}).json()
    assert [c["title"] for c in by_ingredient["recipes"]] == ["Lemon Cake"]


def test_list_paginates(client, session_token):
    for i in range(3):
        _submit(client, session_token, title=f"Recipe {i}")
    page1 = client.get("/v1/recipes", params={"per_page": 2, "page": 1}).json()
    assert page1["total"] == 3
    assert len(page1["recipes"]) == 2
    page2 = client.get("/v1/recipes", params={"per_page": 2, "page": 2}).json()
    assert len(page2["recipes"]) == 1


# --- Download visibility -----------------------------------------------------

def test_download_returns_full_recipe(client, session_token):
    rid = _submit(client, session_token).json()["id"]
    full = client.get(f"/v1/recipes/{rid}").json()
    assert full["ingredients"] == VALID["ingredients"]
    assert full["steps"] == VALID["steps"]


def test_owner_can_download_own_pending_but_others_cannot(client, session_token):
    rid = _submit(client, session_token).json()["id"]
    db = SessionLocal()
    try:
        db.get(CommunityRecipe, rid).status = "pending"
        db.commit()
    finally:
        db.close()
    # A stranger (no auth) gets a not-found.
    assert client.get(f"/v1/recipes/{rid}").status_code == 404
    # A different account also gets a not-found.
    other = client.post("/v1/accounts/signup",
                        json={"email": "eve@example.com", "password": "hunter2222"})
    other_token = other.json()["session_token"]
    assert client.get(f"/v1/recipes/{rid}",
                      headers=_auth(other_token)).status_code == 404
    # The owner still can.
    owner = client.get(f"/v1/recipes/{rid}", headers=_auth(session_token))
    assert owner.status_code == 200


# --- Ratings ----------------------------------------------------------------

def test_rating_upserts_and_average_updates(client, session_token):
    rid = _submit(client, session_token).json()["id"]
    first = client.post(f"/v1/recipes/{rid}/rating", json={"stars": 4},
                        headers=_auth(session_token))
    assert first.status_code == 200
    assert first.json()["average_rating"] == 4.0
    assert first.json()["rating_count"] == 1

    # A second rating by the SAME account replaces the first, not adds to it.
    second = client.post(f"/v1/recipes/{rid}/rating", json={"stars": 2},
                         headers=_auth(session_token))
    assert second.json()["rating_count"] == 1
    assert second.json()["average_rating"] == 2.0

    db = SessionLocal()
    try:
        assert db.query(RecipeRating).count() == 1
        row = db.get(CommunityRecipe, rid)
        assert row.rating_count == 1 and row.rating_sum == 2
    finally:
        db.close()


def test_rating_clamps_out_of_range(client, session_token):
    rid = _submit(client, session_token).json()["id"]
    resp = client.post(f"/v1/recipes/{rid}/rating", json={"stars": 99},
                       headers=_auth(session_token))
    assert resp.json()["your_rating"] == 5


def test_two_accounts_rating_both_count(client, session_token):
    rid = _submit(client, session_token).json()["id"]
    client.post(f"/v1/recipes/{rid}/rating", json={"stars": 5},
                headers=_auth(session_token))
    other = client.post("/v1/accounts/signup",
                        json={"email": "bob@example.com", "password": "hunter2222"})
    client.post(f"/v1/recipes/{rid}/rating", json={"stars": 3},
                headers=_auth(other.json()["session_token"]))
    listing = client.get("/v1/recipes", params={"q": "chili"}).json()
    card = listing["recipes"][0]
    assert card["rating_count"] == 2
    assert card["average_rating"] == 4.0


def test_rating_requires_login(client, session_token):
    rid = _submit(client, session_token).json()["id"]
    assert client.post(f"/v1/recipes/{rid}/rating",
                       json={"stars": 5}).status_code == 401


# --- Reports ----------------------------------------------------------------

def test_report_records_and_bumps_count(client, session_token):
    rid = _submit(client, session_token).json()["id"]
    resp = client.post(f"/v1/recipes/{rid}/report", json={"reason": "spam"},
                       headers=_auth(session_token))
    assert resp.status_code == 200
    db = SessionLocal()
    try:
        assert db.query(RecipeReport).count() == 1
        assert db.get(CommunityRecipe, rid).report_count == 1
    finally:
        db.close()


def test_report_auto_hides_at_threshold(client, session_token, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "recipe_report_hide_threshold", 2)
    rid = _submit(client, session_token).json()["id"]
    client.post(f"/v1/recipes/{rid}/report", json={"reason": "a"},
                headers=_auth(session_token))
    # Still public after one flag.
    assert client.get(f"/v1/recipes/{rid}").status_code == 200
    other = client.post("/v1/accounts/signup",
                        json={"email": "cara@example.com", "password": "hunter2222"})
    client.post(f"/v1/recipes/{rid}/report", json={"reason": "b"},
                headers=_auth(other.json()["session_token"]))
    # Second flag reaches the threshold and pulls it from the browser.
    assert client.get(f"/v1/recipes/{rid}").status_code == 404


def test_report_twice_by_same_account_is_a_noop(client, session_token):
    rid = _submit(client, session_token).json()["id"]
    first = client.post(f"/v1/recipes/{rid}/report", json={"reason": "spam"},
                        headers=_auth(session_token))
    assert first.status_code == 200
    # A repeat flag from the same member does not error and does not stack.
    second = client.post(f"/v1/recipes/{rid}/report", json={"reason": "again"},
                         headers=_auth(session_token))
    assert second.status_code == 200
    db = SessionLocal()
    try:
        assert db.query(RecipeReport).filter_by(recipe_id=rid).count() == 1
        assert db.get(CommunityRecipe, rid).report_count == 1
    finally:
        db.close()


def test_report_hide_counts_distinct_reporters_not_calls(client, session_token,
                                                         monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "recipe_report_hide_threshold", 2)
    monkeypatch.setattr(settings, "recipe_report_rate_per_minute", 0)
    rid = _submit(client, session_token).json()["id"]
    # One member reporting many times must never reach a two-reporter threshold.
    for _ in range(5):
        client.post(f"/v1/recipes/{rid}/report", json={"reason": "x"},
                    headers=_auth(session_token))
    assert client.get(f"/v1/recipes/{rid}").status_code == 200
    db = SessionLocal()
    try:
        assert db.get(CommunityRecipe, rid).report_count == 1
    finally:
        db.close()
    # A second, distinct member tips it over.
    other = client.post("/v1/accounts/signup",
                        json={"email": "cara@example.com", "password": "hunter2222"})
    client.post(f"/v1/recipes/{rid}/report", json={"reason": "y"},
                headers=_auth(other.json()["session_token"]))
    assert client.get(f"/v1/recipes/{rid}").status_code == 404


def test_report_rate_limited(client, session_token, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "recipe_report_rate_per_minute", 2)
    # Keep the threshold out of the way so only the rate limit is under test.
    monkeypatch.setattr(settings, "recipe_report_hide_threshold", 0)
    ratelimit.reset()
    # Distinct recipes so the per-member dedupe never turns a call into a no-op.
    r1 = _submit(client, session_token, title="One").json()["id"]
    r2 = _submit(client, session_token, title="Two").json()["id"]
    r3 = _submit(client, session_token, title="Three").json()["id"]
    assert client.post(f"/v1/recipes/{r1}/report", json={"reason": "a"},
                       headers=_auth(session_token)).status_code == 200
    assert client.post(f"/v1/recipes/{r2}/report", json={"reason": "b"},
                       headers=_auth(session_token)).status_code == 200
    flood = client.post(f"/v1/recipes/{r3}/report", json={"reason": "c"},
                        headers=_auth(session_token))
    assert flood.status_code == 429


# --- Spam protection --------------------------------------------------------

def test_honeypot_rejects_and_stores_nothing(client, session_token):
    resp = _submit(client, session_token, website="http://spam.example")
    assert resp.status_code == 400
    db = SessionLocal()
    try:
        assert db.query(CommunityRecipe).count() == 0
    finally:
        db.close()


def test_submit_rate_limited(client, session_token, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "recipe_submit_rate_per_minute", 2)
    ratelimit.reset()
    assert _submit(client, session_token, title="One").status_code == 200
    assert _submit(client, session_token, title="Two").status_code == 200
    third = _submit(client, session_token, title="Three")
    assert third.status_code == 429
    db = SessionLocal()
    try:
        assert db.query(CommunityRecipe).count() == 2
    finally:
        db.close()
