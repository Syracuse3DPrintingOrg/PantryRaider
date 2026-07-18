"""Forager-hosted recipe photos and the readable per-recipe URL (afx1, peg2).

Covers the image sniffing/validation helpers, the community and private-share
image upload/serve endpoints (store the bytes, refuse a non-image or oversized
upload, serve the sniffed type, enforce ownership), and the by-token resolution
plus slug/token on the cards. The image store is pointed at a temp dir so
nothing touches the real data volume.
"""
from __future__ import annotations

import pytest

from app import recipe_images
from app.config import settings
from app.database import SessionLocal
from app.models import CommunityRecipe, SharedRecipe


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# Minimal valid magic-byte payloads (>= 12 bytes, which the sniff needs). They
# are not decodable images, but the endpoint validates by signature, not by
# decoding, so these exercise the real accept path.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
GIF = b"GIF89a" + b"\x00" * 24
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 12
SVG = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
NOT_IMAGE = b"this is definitely not an image file at all"


@pytest.fixture(autouse=True)
def image_dir(tmp_path, monkeypatch):
    """Store uploaded photos in a temp dir, not the real data volume."""
    monkeypatch.setattr(settings, "recipe_image_dir", str(tmp_path / "img"))
    yield


VALID = {
    "title": "Weeknight Chili",
    "description": "A quick pot of chili.",
    "ingredients": ["1 lb beans", "1 onion"],
    "steps": ["Chop the onion.", "Simmer for an hour."],
    "attribution": "From my recipe box.",
}


# --- Pure sniff helpers ------------------------------------------------------

def test_sniff_accepts_the_whitelist_and_refuses_everything_else():
    assert recipe_images.sniff_image_ext(PNG) == "png"
    assert recipe_images.sniff_image_ext(JPEG) == "jpg"
    assert recipe_images.sniff_image_ext(GIF) == "gif"
    assert recipe_images.sniff_image_ext(WEBP) == "webp"
    # SVG is XML (script-capable) and everything unknown is refused.
    assert recipe_images.sniff_image_ext(SVG) is None
    assert recipe_images.sniff_image_ext(NOT_IMAGE) is None
    assert recipe_images.sniff_image_ext(b"tiny") is None


def test_find_image_refuses_traversal_names():
    assert recipe_images.find_image("../etc/passwd") is None
    assert recipe_images.find_image("a/b") is None


# --- Community image upload / serve ------------------------------------------

def test_community_upload_stores_serves_and_sets_url(client, instance_token):
    rid = client.post("/v1/recipes", json=VALID,
                      headers=_auth(instance_token)).json()["id"]

    up = client.post(f"/v1/recipes/{rid}/image",
                     files={"file": ("photo.png", PNG, "image/png")},
                     headers=_auth(instance_token))
    assert up.status_code == 200
    image_url = up.json()["image_url"]
    # A Forager-hosted absolute URL, so the website and the share page can load
    # it directly and it passes the share image-URL guard (a hostname, not IP).
    assert image_url.endswith(f"/v1/recipes/{rid}/image")
    assert image_url.startswith("http")

    # The row now points at the hosted copy...
    db = SessionLocal()
    try:
        assert db.get(CommunityRecipe, rid).image_url == image_url
    finally:
        db.close()

    # ...and the bytes come back with the sniffed type.
    got = client.get(f"/v1/recipes/{rid}/image")
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("image/png")
    assert got.content == PNG


def test_community_image_rejects_non_image_and_oversized(client, instance_token,
                                                         monkeypatch):
    rid = client.post("/v1/recipes", json=VALID,
                      headers=_auth(instance_token)).json()["id"]

    bad = client.post(f"/v1/recipes/{rid}/image",
                      files={"file": ("x.png", NOT_IMAGE, "image/png")},
                      headers=_auth(instance_token))
    assert bad.status_code == 400  # a lie about the type does not get it stored

    svg = client.post(f"/v1/recipes/{rid}/image",
                      files={"file": ("x.svg", SVG, "image/svg+xml")},
                      headers=_auth(instance_token))
    assert svg.status_code == 400  # SVG refused outright

    monkeypatch.setattr(settings, "recipe_image_max_bytes", 8)
    big = client.post(f"/v1/recipes/{rid}/image",
                      files={"file": ("x.png", PNG, "image/png")},
                      headers=_auth(instance_token))
    assert big.status_code == 413

    # Nothing was stored by any of the rejected uploads.
    assert client.get(f"/v1/recipes/{rid}/image").status_code == 404


def test_community_image_upload_is_owner_only(client, instance_token):
    rid = client.post("/v1/recipes", json=VALID,
                      headers=_auth(instance_token)).json()["id"]
    other = client.post("/v1/accounts/signup",
                        json={"email": "mallory@example.com",
                              "password": "hunter2222"})
    other_token = other.json()["session_token"]
    resp = client.post(f"/v1/recipes/{rid}/image",
                       files={"file": ("photo.png", PNG, "image/png")},
                       headers=_auth(other_token))
    assert resp.status_code == 403
    assert client.get(f"/v1/recipes/{rid}/image").status_code == 404


def test_community_image_serve_hidden_recipe_is_not_public(client,
                                                           instance_token):
    rid = client.post("/v1/recipes", json=VALID,
                      headers=_auth(instance_token)).json()["id"]
    client.post(f"/v1/recipes/{rid}/image",
                files={"file": ("photo.png", PNG, "image/png")},
                headers=_auth(instance_token))
    # Pull the recipe from public view: its photo follows the same rule.
    db = SessionLocal()
    try:
        db.get(CommunityRecipe, rid).status = "hidden"
        db.commit()
    finally:
        db.close()
    assert client.get(f"/v1/recipes/{rid}/image").status_code == 404


# --- Readable per-recipe URL: slug + token, by-token resolution --------------

def test_submit_returns_slug_token_and_canonical_url(client, instance_token):
    body = client.post("/v1/recipes", json=VALID,
                       headers=_auth(instance_token)).json()
    assert body["slug"] == "weeknight-chili"
    assert body["share_token"] and len(body["share_token"]) == 10
    assert body["url"].endswith(f"/r/{body['slug']}-{body['share_token']}")


def test_cards_and_full_carry_slug_and_token(client, instance_token):
    client.post("/v1/recipes", json=VALID, headers=_auth(instance_token))
    listing = client.get("/v1/recipes").json()["recipes"]
    assert listing and listing[0]["slug"] == "weeknight-chili"
    assert listing[0]["share_token"]


def test_by_token_resolves_same_as_by_id(client, instance_token):
    body = client.post("/v1/recipes", json=VALID,
                       headers=_auth(instance_token)).json()
    token = body["share_token"]
    by_token = client.get(f"/v1/recipes/by-token/{token}")
    assert by_token.status_code == 200
    assert by_token.json()["title"] == "Weeknight Chili"
    assert by_token.json()["ingredients"] == VALID["ingredients"]
    # An unknown token is a plain 404, and the old numeric id still resolves.
    assert client.get("/v1/recipes/by-token/nope1234ab").status_code == 404
    assert client.get(f"/v1/recipes/{body['id']}").status_code == 200


def test_by_token_hidden_recipe_stays_submitter_only(client, instance_token):
    body = client.post("/v1/recipes", json=VALID,
                       headers=_auth(instance_token)).json()
    token = body["share_token"]
    db = SessionLocal()
    try:
        db.get(CommunityRecipe, body["id"]).status = "pending"
        db.commit()
    finally:
        db.close()
    # A stranger cannot reach it by token (same rule as by id)...
    assert client.get(f"/v1/recipes/by-token/{token}").status_code == 404
    # ...but the submitter still can.
    ok = client.get(f"/v1/recipes/by-token/{token}", headers=_auth(instance_token))
    assert ok.status_code == 200


# --- Private share image upload / serve --------------------------------------

def _make_share(client, token):
    resp = client.post("/v1/recipes/shares", json={
        "title": "Sam's Chili", "ingredients": ["1 lb beans"],
        "steps": ["Simmer."], "attribution": "Sam"}, headers=_auth(token))
    assert resp.status_code == 200
    return resp.json()["token"]


def test_share_image_upload_stores_serves_and_sets_url(client, session_token):
    stoken = _make_share(client, session_token)
    up = client.post(f"/v1/recipes/shares/{stoken}/image",
                     files={"file": ("photo.jpg", JPEG, "image/jpeg")},
                     headers=_auth(session_token))
    assert up.status_code == 200
    assert up.json()["image_url"].endswith(
        f"/v1/recipes/shares/{stoken}/image")

    db = SessionLocal()
    try:
        assert db.query(SharedRecipe).filter_by(token=stoken).first().image_url \
            == up.json()["image_url"]
    finally:
        db.close()

    got = client.get(f"/v1/recipes/shares/{stoken}/image")
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("image/jpeg")
    assert got.content == JPEG


def test_share_image_rejects_non_image_and_serves_404_when_absent(client,
                                                                  session_token):
    stoken = _make_share(client, session_token)
    bad = client.post(f"/v1/recipes/shares/{stoken}/image",
                      files={"file": ("x.png", NOT_IMAGE, "image/png")},
                      headers=_auth(session_token))
    assert bad.status_code == 400
    assert client.get(f"/v1/recipes/shares/{stoken}/image").status_code == 404


def test_share_image_upload_is_owner_only(client, session_token):
    stoken = _make_share(client, session_token)
    other = client.post("/v1/accounts/signup",
                        json={"email": "mallory@example.com",
                              "password": "hunter2222"})
    resp = client.post(f"/v1/recipes/shares/{stoken}/image",
                       files={"file": ("photo.png", PNG, "image/png")},
                       headers=_auth(other.json()["session_token"]))
    assert resp.status_code == 404  # not yours: indistinguishable from unknown
