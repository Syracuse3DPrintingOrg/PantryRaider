"""The share-time recipe audit: the brand and nominative-marker truth table,
publisher-copy markers, the softer note-only signals, the wiring into both
share paths, and the stored audit result."""
import json

from app.database import SessionLocal
from app.models import CommunityRecipe
from app.recipe_audit import (BRAND_NAMES, audit_recipe, block_message,
                              guidance)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


VALID = {
    "title": "Weeknight Chili",
    "description": "A quick pot of chili.",
    "ingredients": ["1 lb beans", "1 onion"],
    "steps": ["Chop the onion.", "Simmer everything for an hour."],
    "attribution": "From my grandmother's recipe box.",
}


def _submit(client, token, **overrides):
    body = {**VALID, **overrides}
    return client.post("/v1/recipes", json=body, headers=_auth(token))


def _codes(audit):
    return [f["code"] for f in audit["findings"]]


# --- The truth table --------------------------------------------------------

def test_clean_recipe_is_ok():
    audit = audit_recipe(title="Weeknight Chili",
                         description="A quick pot of chili.",
                         steps=["Chop the onion.", "Simmer for an hour."])
    assert audit == {"level": "ok", "findings": []}


def test_brand_alone_in_title_advises():
    audit = audit_recipe(title="Oreo Cheesecake")
    assert audit["level"] == "advise"
    assert _codes(audit) == ["brand_identity"]
    assert audit["findings"][0]["brand"] == "Oreo"
    # The guidance steers toward the compliant naming forms.
    assert "Copycat Oreo" in audit["findings"][0]["message"]


def test_copycat_brand_is_ok():
    assert audit_recipe(title="Copycat Oreo Cheesecake")["level"] == "ok"
    # "copycat" anywhere in the title counts, either side of the brand.
    assert audit_recipe(title="Oreo Copycat Cheesecake")["level"] == "ok"


def test_brand_style_is_ok():
    assert audit_recipe(title="Oreo-style Cheesecake")["level"] == "ok"
    assert audit_recipe(title="oreo style cheesecake")["level"] == "ok"
    # "style" must hang off the brand, not float elsewhere in the title.
    assert audit_recipe(title="Country style Oreo bites")["level"] == "advise"


def test_inspired_by_is_ok():
    assert audit_recipe(title="Inspired by Olive Garden Breadsticks")["level"] == "ok"
    assert audit_recipe(title="Olive Garden Inspired Breadsticks")["level"] == "ok"


def test_case_insensitive_and_plural():
    assert audit_recipe(title="oreo truffles")["level"] == "advise"
    assert audit_recipe(title="Homemade Oreos")["level"] == "advise"
    assert audit_recipe(title="OREO PIE")["level"] == "advise"


def test_word_boundary_no_match_inside_words():
    assert audit_recipe(title="Boreo Cookies")["level"] == "ok"
    assert audit_recipe(title="Ritzy Dinner Rolls")["level"] == "ok"
    assert audit_recipe(title="Cookies and Cream Milkshake")["level"] == "ok"


def test_multi_word_and_apostrophe_brands():
    audit = audit_recipe(title="Olive Garden Alfredo")
    assert audit["level"] == "advise"
    assert audit["findings"][0]["brand"] == "Olive Garden"
    # Apostrophes are optional both ways.
    assert audit_recipe(title="Wendy's Chili")["level"] == "advise"
    assert audit_recipe(title="Wendys Chili")["level"] == "advise"
    assert audit_recipe(title="McDonald's-style Fries")["level"] == "ok"


def test_brand_in_steps_is_nominative_and_not_flagged():
    audit = audit_recipe(title="Chocolate Hazelnut Toast",
                         steps=["Spread Nutella over the warm toast."])
    assert audit == {"level": "ok", "findings": []}


def test_publisher_copy_marker_blocks():
    audit = audit_recipe(title="Weeknight Chili",
                         steps=["Simmer for an hour.",
                                "Reprinted with permission of the publisher."])
    assert audit["level"] == "block"
    assert "publisher_copy" in _codes(audit)
    assert "own words" in block_message(audit)


def test_publisher_copy_marker_is_case_insensitive_and_field_wide():
    assert audit_recipe(title="Chili",
                        description="EXCERPTED FROM a cookbook")["level"] == "block"


def test_block_beats_advise():
    audit = audit_recipe(title="Oreo Pie",
                         steps=["reprinted with permission"])
    assert audit["level"] == "block"


def test_publisher_link_in_step_is_a_note_not_a_block():
    audit = audit_recipe(title="Weeknight Chili",
                         steps=["From https://www.foodnetwork.com/recipes/chili"])
    assert audit["level"] == "ok"
    assert _codes(audit) == ["publisher_link"]
    assert audit["findings"][0]["severity"] == "note"


def test_unusually_long_step_is_a_note_not_a_block():
    audit = audit_recipe(title="Weeknight Chili", steps=["stir the pot " * 80])
    assert audit["level"] == "ok"
    assert _codes(audit) == ["long_step"]


def test_guidance_returns_only_advise_messages():
    audit = audit_recipe(title="Oreo Pie",
                         steps=["From https://seriouseats.com/pie"])
    tips = guidance(audit)
    assert len(tips) == 1
    assert "Copycat Oreo" in tips[0]
    assert audit_recipe(title="Plain Pie")["findings"] == []
    assert guidance(audit_recipe(title="Plain Pie")) == []


def test_messages_carry_no_em_dashes():
    audit = audit_recipe(title="Oreo Big Mac Surprise",
                         description="excerpted from somewhere",
                         steps=["See foodnetwork.com.", "long words " * 80])
    assert "\u2014" not in json.dumps(audit)


def test_brand_list_is_word_boundary_safe_against_itself():
    # Every shipped brand matches its own canonical spelling.
    for brand in BRAND_NAMES:
        audit = audit_recipe(title=f"{brand} at home")
        assert audit["level"] == "advise", brand


# --- Wiring: the app's JSON share path --------------------------------------

def _member_token(client, email="mia@example.com"):
    resp = client.post("/v1/accounts/signup",
                       json={"email": email, "password": "hunter2222"})
    return resp.json()["session_token"]


def test_submit_with_brand_title_shares_and_returns_guidance(client):
    token = _member_token(client)
    resp = _submit(client, token, title="Oreo Cheesecake Bites")
    assert resp.status_code == 200
    body = resp.json()
    assert body["guidance"] and "Copycat Oreo" in body["guidance"][0]

    db = SessionLocal()
    try:
        recipe = db.get(CommunityRecipe, body["id"])
        assert recipe.status == "approved"  # advised, never censored
        assert recipe.audit_level == "advise"
        stored = json.loads(recipe.audit_findings)
        assert stored[0]["code"] == "brand_identity"
    finally:
        db.close()


def test_submit_clean_recipe_has_no_guidance_and_stores_ok(client):
    token = _member_token(client)
    resp = _submit(client, token)
    assert resp.status_code == 200
    assert "guidance" not in resp.json()

    db = SessionLocal()
    try:
        recipe = db.get(CommunityRecipe, resp.json()["id"])
        assert recipe.audit_level == "ok"
        assert json.loads(recipe.audit_findings) == []
    finally:
        db.close()


def test_submit_with_publisher_copy_marker_is_refused(client):
    token = _member_token(client)
    resp = _submit(client, token,
                   steps=["Simmer.", "Reprinted with permission from the book."])
    assert resp.status_code == 400
    assert "own words" in resp.json()["detail"]

    db = SessionLocal()
    try:
        assert db.query(CommunityRecipe).count() == 0
    finally:
        db.close()


def test_submit_compliant_copycat_title_passes_clean(client):
    token = _member_token(client)
    resp = _submit(client, token, title="Copycat Olive Garden Breadsticks")
    assert resp.status_code == 200
    assert "guidance" not in resp.json()
