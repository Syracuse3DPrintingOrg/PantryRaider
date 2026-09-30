"""Community recipes are stored as plain text.

A shared recipe is public the moment it is shared (moderation is after the
fact by default), and every linked install and the website show it, so markup
in any field is removed on the way in rather than trusted to each place that
renders it. Both ways in are covered: the app's (and website's) JSON submit
and the portal upload's confirm step.
"""
import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Account, CommunityRecipe
from app.routers import recipes

PAYLOAD = "<img src=x onerror=alert(1)>"

# What a browser reads as the start of a tag: "<" and then a letter, "/", "!"
# or "?". A "<" followed by anything else is text.
TAG_OPENER = re.compile(r"<[A-Za-z/!?]")

# Tags built so that taking out one "<", or one complete tag, leaves a new one
# behind. An installed app that shows a field without escaping it would run
# each of these if a "<img" survived.
NESTED = [
    "<<img src=x onerror=alert(1)//",
    "Chili <<<svg/onload=alert(1)//",
    "&lt;&lt;img src=x onerror=alert(1)//",
    "<</b><<img src=x onerror=alert(1)//",
    "<!<b>--><<img src=x onerror=alert(1)//",
    "<!-- open <<img src=x onerror=alert(1)//",
]

MARKED_UP = {
    "title": f"Weeknight Chili {PAYLOAD}",
    "description": f"A quick pot of chili. {PAYLOAD}",
    "ingredients": ["1 lb beans", f"1 onion {PAYLOAD}"],
    "steps": [f"Chop the onion. {PAYLOAD}", "Simmer everything for an hour."],
    "attribution": f"From my grandmother's recipe box. {PAYLOAD}",
}


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _stored(recipe_id):
    """Every text field of the stored row, as a dict."""
    db = SessionLocal()
    try:
        row = db.get(CommunityRecipe, recipe_id)
        return {"title": row.title, "description": row.description,
                "attribution": row.attribution, "slug": row.slug,
                "ingredients": json.loads(row.ingredients),
                "steps": json.loads(row.steps)}
    finally:
        db.close()


def _texts(fields):
    """The string values of a stored row or an API reply, lists flattened."""
    out = []
    for key in ("title", "description", "attribution", "ingredients", "steps"):
        value = fields.get(key)
        if isinstance(value, list):
            out.extend(value)
        elif value is not None:
            out.append(value)
    return out


def _assert_clean(fields):
    for value in _texts(fields):
        assert "<" not in value, value


# --- Through the endpoints ----------------------------------------------------

@pytest.mark.parametrize("who", ["instance_token", "session_token"])
def test_markup_is_stored_and_listed_as_plain_text(client, who, request):
    token = request.getfixturevalue(who)
    resp = client.post("/v1/recipes", json=MARKED_UP, headers=_auth(token))
    assert resp.status_code == 200
    rid = resp.json()["id"]

    stored = _stored(rid)
    _assert_clean(stored)
    assert stored == {
        "title": "Weeknight Chili",
        "description": "A quick pot of chili.",
        "attribution": "From my grandmother's recipe box.",
        "slug": "weeknight-chili",
        "ingredients": ["1 lb beans", "1 onion"],
        "steps": ["Chop the onion.", "Simmer everything for an hour."],
    }

    card = next(r for r in client.get("/v1/recipes").json()["recipes"]
                if r["id"] == rid)
    _assert_clean(card)
    full = client.get(f"/v1/recipes/{rid}").json()
    _assert_clean(full)
    assert full["ingredients"] == ["1 lb beans", "1 onion"]


def test_escaped_markup_is_caught_too(client, instance_token):
    body = {**MARKED_UP,
            "title": "Chili &lt;script&gt;alert(1)&lt;/script&gt;",
            "steps": ["Stir &lt;img src=x onerror=alert(1)&gt; well."]}
    rid = client.post("/v1/recipes", json=body,
                      headers=_auth(instance_token)).json()["id"]
    stored = _stored(rid)
    _assert_clean(stored)
    assert stored["title"] == "Chili alert(1)"
    assert stored["steps"] == ["Stir well."]


def test_a_field_of_nothing_but_markup_counts_as_missing(client,
                                                        instance_token):
    resp = client.post("/v1/recipes", json={**MARKED_UP, "title": PAYLOAD},
                       headers=_auth(instance_token))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Please give your recipe a title."

    resp = client.post("/v1/recipes",
                       json={**MARKED_UP, "attribution": "<b></b>"},
                       headers=_auth(instance_token))
    assert resp.status_code == 400
    assert "credit" in resp.json()["detail"].lower()

    resp = client.post("/v1/recipes",
                       json={**MARKED_UP, "steps": [PAYLOAD, "<br>"]},
                       headers=_auth(instance_token))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Please add at least one step."

    db = SessionLocal()
    try:
        assert db.query(CommunityRecipe).count() == 0
    finally:
        db.close()


def test_the_share_audit_reads_the_stored_text(client, instance_token):
    # An entity or a tag must not hide a publisher-copy line from the audit
    # when the stored text would carry it plainly.
    body = {**MARKED_UP,
            "steps": ["Mix well.", "Reprinted&nbsp;with <b>permission</b>."]}
    resp = client.post("/v1/recipes", json=body, headers=_auth(instance_token))
    assert resp.status_code == 400
    assert "reprinted with permission" in resp.json()["detail"]


def test_portal_upload_confirm_stores_plain_text_too():
    browser = TestClient(app)
    assert browser.post("/signup", data={
        "email": "cook@example.com", "password": "hunter2222",
        "confirm_password": "hunter2222"},
        follow_redirects=False).status_code == 303
    db = SessionLocal()
    try:
        # Hand-authorized, so the upload gate opens without a linked kitchen.
        account = db.query(Account).filter_by(email="cook@example.com").one()
        account.recipe_upload_authorized = 1
        db.commit()
    finally:
        db.close()

    resp = browser.post("/recipes/upload/confirm", data={
        "title": f"Grandma's Chili {PAYLOAD}",
        "ingredients": f"1 lb beans\n1 onion {PAYLOAD}\n{PAYLOAD}",
        "steps": f"Chop. {PAYLOAD}\nSimmer.",
        "attribution": f"From my grandmother. {PAYLOAD}",
    }, follow_redirects=False)
    assert resp.status_code == 303

    db = SessionLocal()
    try:
        rid = db.query(CommunityRecipe).one().id
    finally:
        db.close()
    stored = _stored(rid)
    _assert_clean(stored)
    assert stored["title"] == "Grandma's Chili"
    assert stored["ingredients"] == ["1 lb beans", "1 onion"]
    assert stored["steps"] == ["Chop.", "Simmer."]
    assert stored["attribution"] == "From my grandmother."


# --- The cleaning itself ------------------------------------------------------

def test_plain_text_leaves_ordinary_text_alone():
    plain_text = recipes.plain_text
    assert plain_text("Bake until the center reads < 165F") == \
        "Bake until the center reads < 165F"
    assert plain_text("Made with <3 by Aunt May") == "Made with <3 by Aunt May"
    assert plain_text("Salt & pepper, 1 cup (8 oz)") == \
        "Salt & pepper, 1 cup (8 oz)"
    assert plain_text("  Tom's   chili\t\n") == "Tom's chili"


def test_plain_text_removes_tags_comments_and_unclosed_openers():
    plain_text = recipes.plain_text
    assert plain_text("Very <b>hot</b> sauce") == "Very hot sauce"
    assert plain_text("Chili<br/>for two") == "Chili for two"
    assert plain_text("Soup <!-- <script>x</script> --> tonight") == \
        "Soup tonight"
    # A tag that never closes (cut off by a length cap, or sent that way)
    # loses the "<" that would open it, so nothing can start a tag.
    left = plain_text("Chili <img src=x onerror=alert(1)//")
    assert "<" not in left
    assert plain_text("</script") == "/script"


def test_plain_text_caps_after_cleaning():
    title = "<b>" + "a" * 250 + "</b>"
    assert recipes.plain_text(title, 200) == "a" * 200


def test_plain_text_multiline_keeps_paragraphs():
    text = "First  paragraph.\r\n\n\n\nSecond <b>bold</b>\tparagraph.\n"
    assert recipes.plain_text(text, multiline=True) == \
        "First paragraph.\n\nSecond bold paragraph."


def test_plain_lines_drops_lines_that_were_only_markup():
    lines = ["1 onion", PAYLOAD, " <br> ", "2 <i>ripe</i> tomatoes"]
    assert recipes.plain_lines(lines) == ["1 onion", "2 ripe tomatoes"]


@pytest.mark.parametrize("value", NESTED)
def test_no_tag_is_left_behind_by_the_cleaning(value):
    for multiline in (False, True):
        left = recipes.plain_text(value, 200, multiline=multiline)
        assert not TAG_OPENER.search(left), left
    assert recipes.plain_text("<<img src=x onerror=alert(1)//") == \
        "img src=x onerror=alert(1)//"


def test_nested_markup_is_stored_without_a_tag(client, instance_token):
    body = {**MARKED_UP,
            "title": f"Chili {NESTED[0]}",
            "description": "\n".join(NESTED),
            "ingredients": ["1 onion", *NESTED],
            "steps": [f"Stir. {value}" for value in NESTED],
            "attribution": f"Grandma {NESTED[1]}"}
    resp = client.post("/v1/recipes", json=body, headers=_auth(instance_token))
    assert resp.status_code == 200
    rid = resp.json()["id"]
    card = next(r for r in client.get("/v1/recipes").json()["recipes"]
                if r["id"] == rid)
    full = client.get(f"/v1/recipes/{rid}").json()
    for fields in (_stored(rid), card, full):
        for value in _texts(fields):
            assert not TAG_OPENER.search(value), value


def test_cleaning_time_grows_in_step_with_the_field():
    # A field arrives straight from the request body, so hostile input must
    # not cost more than its size: a search that runs to the end of the text
    # from every opener that never closes takes seconds on 100 KB, and a
    # request of a few megabytes would hold the whole service for hours.
    size = 100_000
    fields = ["<!--" * (size // 4), "<a" * (size // 2), "<" * size,
              "<a" + "b" * size, "<!--<a<<&lt;" * (size // 12)]
    started = time.perf_counter()
    for value in fields:
        recipes.plain_text(value, recipes.TITLE_MAX)
        recipes.plain_text(value, multiline=True)
    assert time.perf_counter() - started < 2.0
