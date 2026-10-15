"""Screensaver photo sources (FoodAssistant-af1l).

Covers the pure photo_source logic (URL parsing, folder listing, the
traversal guard, Immich album parsing, source dispatch), the guarded local
serve route, the Immich proxy's refusal paths, the photo-list endpoint
shape, and the /save validation for the new settings.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402
from app.services import photo_source as ps  # noqa: E402


# ---------------------------------------------------------------- pure logic

def test_normalize_photo_source():
    assert ps.normalize_photo_source("folder") == "folder"
    assert ps.normalize_photo_source("immich") == "immich"
    assert ps.normalize_photo_source("urls") == "urls"
    assert ps.normalize_photo_source("built-in") == "built-in"
    assert ps.normalize_photo_source("google-photos") == "built-in"
    assert ps.normalize_photo_source(None) == "built-in"


def test_parse_photo_urls_lines_commas_and_filtering():
    text = ("https://a.example/one.jpg\n"
            "  https://a.example/two.png , http://b.example/three.webp\n"
            "ftp://nope.example/x.jpg\n"
            "javascript:alert(1)\n"
            "not a url\n"
            "https://a.example/one.jpg\n")   # duplicate collapses
    assert ps.parse_photo_urls(text) == [
        "https://a.example/one.jpg",
        "https://a.example/two.png",
        "http://b.example/three.webp",
    ]


def test_parse_photo_urls_empty_and_non_string():
    assert ps.parse_photo_urls("") == []
    assert ps.parse_photo_urls("   \n  ") == []
    assert ps.parse_photo_urls(None) == []
    assert ps.parse_photo_urls(123) == []


def test_is_image_name():
    assert ps.is_image_name("cat.jpg")
    assert ps.is_image_name("CAT.JPEG")
    assert ps.is_image_name("pano.webp")
    assert not ps.is_image_name("notes.txt")
    assert not ps.is_image_name(".hidden.jpg")
    assert not ps.is_image_name("noext")
    assert not ps.is_image_name("")


def test_list_folder_photos(tmp_path):
    (tmp_path / "b.png").write_bytes(b"x")
    (tmp_path / "a.JPG").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    (tmp_path / ".hidden.jpg").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    assert ps.list_folder_photos(tmp_path) == ["a.JPG", "b.png"]


def test_list_folder_photos_missing_folder(tmp_path):
    assert ps.list_folder_photos(tmp_path / "nope") == []


def test_safe_photo_path_accepts_plain_image(tmp_path):
    (tmp_path / "ok.jpg").write_bytes(b"x")
    got = ps.safe_photo_path(tmp_path, "ok.jpg")
    assert got is not None and got.name == "ok.jpg"


@pytest.mark.parametrize("name", [
    "", "../etc/passwd", "..\\etc\\passwd", "sub/ok.jpg", "sub\\ok.jpg",
    "..", "ok.txt", ".hidden.jpg", "missing.jpg", "ok.jpg/../../etc/passwd",
])
def test_safe_photo_path_rejects(tmp_path, name):
    (tmp_path / "ok.jpg").write_bytes(b"x")
    (tmp_path / "ok.txt").write_bytes(b"x")
    (tmp_path / ".hidden.jpg").write_bytes(b"x")
    assert ps.safe_photo_path(tmp_path, name) is None


def test_safe_photo_path_rejects_directory_named_like_image(tmp_path):
    (tmp_path / "dir.jpg").mkdir()
    assert ps.safe_photo_path(tmp_path, "dir.jpg") is None


def test_effective_photo_folder_default_and_override(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "photo_folder", "", raising=False)
    assert ps.effective_photo_folder(settings) == tmp_path / "photos"
    monkeypatch.setattr(settings, "photo_folder", "/mnt/share/pics", raising=False)
    assert ps.effective_photo_folder(settings) == Path("/mnt/share/pics")


def test_parse_immich_album():
    data = {"assets": [
        {"id": "11111111-2222-3333-4444-555555555555", "type": "IMAGE"},
        {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "type": "image"},
        {"id": "99999999-8888-7777-6666-555555555555", "type": "VIDEO"},
        {"id": "../../evil", "type": "IMAGE"},
        {"type": "IMAGE"},
        "junk",
    ]}
    assert ps.parse_immich_album(data) == [
        "11111111-2222-3333-4444-555555555555",
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    ]
    assert ps.parse_immich_album({}) == []
    assert ps.parse_immich_album(None) == []
    assert ps.parse_immich_album({"assets": "nope"}) == []


def test_immich_headers():
    assert ps.immich_headers("k123")["x-api-key"] == "k123"


def test_immich_album_asset_ids_refuses_incomplete_config():
    ps.invalidate_immich_cache()
    run = asyncio.run
    aid = "11111111-2222-3333-4444-555555555555"
    assert run(ps.immich_album_asset_ids("", "key", aid)) == []
    assert run(ps.immich_album_asset_ids("http://x", "", aid)) == []
    assert run(ps.immich_album_asset_ids("http://x", "key", "")) == []
    # An album id that is not UUID-shaped never reaches the network.
    assert run(ps.immich_album_asset_ids("http://x", "key", "../../oops")) == []


def test_src_builders_quote():
    assert ps.folder_photo_src("my cat.jpg") == \
        "ui/screensaver/photo/local?name=my%20cat.jpg"
    assert ps.immich_photo_src("abc-def") == \
        "ui/screensaver/photo/immich?id=abc-def"


def test_list_photos_dispatch(monkeypatch, tmp_path):
    run = asyncio.run
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)

    # folder: default data_dir/photos
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "a.jpg").write_bytes(b"x")
    monkeypatch.setattr(settings, "photo_source", "folder", raising=False)
    monkeypatch.setattr(settings, "photo_folder", "", raising=False)
    assert run(ps.list_photos(settings)) == ["ui/screensaver/photo/local?name=a.jpg"]

    # urls: parsed pass-through
    monkeypatch.setattr(settings, "photo_source", "urls", raising=False)
    monkeypatch.setattr(settings, "photo_urls",
                        "https://x.example/a.jpg\nhttps://x.example/b.jpg",
                        raising=False)
    assert run(ps.list_photos(settings)) == [
        "https://x.example/a.jpg", "https://x.example/b.jpg"]

    # immich: ids from the (mocked) album listing become proxy srcs
    async def fake_ids(base, key, album):
        assert (base, key, album) == ("http://im", "k", "album-1")
        return ["11111111-2222-3333-4444-555555555555"]
    monkeypatch.setattr(ps, "immich_album_asset_ids", fake_ids)
    monkeypatch.setattr(settings, "photo_source", "immich", raising=False)
    monkeypatch.setattr(settings, "immich_base_url", "http://im", raising=False)
    monkeypatch.setattr(settings, "immich_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "immich_album_id", "album-1", raising=False)
    assert run(ps.list_photos(settings)) == [
        "ui/screensaver/photo/immich?id=11111111-2222-3333-4444-555555555555"]

    # built-in and unknown: nothing here (the ui router owns the USB path)
    monkeypatch.setattr(settings, "photo_source", "built-in", raising=False)
    assert run(ps.list_photos(settings)) == []
    monkeypatch.setattr(settings, "photo_source", "google", raising=False)
    assert run(ps.list_photos(settings)) == []


# ------------------------------------------------------------------- routes

@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "photo_source", "built-in", raising=False)
    monkeypatch.setattr(settings, "photo_folder", "", raising=False)
    monkeypatch.setattr(settings, "photo_urls", "", raising=False)
    monkeypatch.setattr(settings, "immich_base_url", "", raising=False)
    monkeypatch.setattr(settings, "immich_api_key", "", raising=False)
    monkeypatch.setattr(settings, "immich_album_id", "", raising=False)
    ps.invalidate_immich_cache()
    # The setup-redirect middleware bounces every request until the app is
    # configured; these tests exercise routes, not the wizard.
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True)
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _configured():
    return patch.object(type(settings), "is_configured", lambda self: True)


def test_photos_endpoint_shape_built_in_off_pi(client):
    r = client.get("/ui/screensaver/photos")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["photos"] == [] and data["urls"] == []


def test_photos_endpoint_folder_source(client, monkeypatch, tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "kitchen.jpg").write_bytes(b"img")
    monkeypatch.setattr(settings, "photo_source", "folder", raising=False)
    r = client.get("/ui/screensaver/photos")
    assert r.json()["urls"] == ["ui/screensaver/photo/local?name=kitchen.jpg"]


def test_photos_endpoint_urls_source(client, monkeypatch):
    monkeypatch.setattr(settings, "photo_source", "urls", raising=False)
    monkeypatch.setattr(settings, "photo_urls",
                        "https://pics.example/a.jpg", raising=False)
    r = client.get("/ui/screensaver/photos")
    assert r.json()["urls"] == ["https://pics.example/a.jpg"]


def test_local_serve_route_and_traversal_guard(client, monkeypatch, tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "ok.jpg").write_bytes(b"imgbytes")
    (tmp_path / "secret.txt").write_text("secret")
    monkeypatch.setattr(settings, "photo_source", "folder", raising=False)

    r = client.get("/ui/screensaver/photo/local", params={"name": "ok.jpg"})
    assert r.status_code == 200 and r.content == b"imgbytes"

    for bad in ("../secret.txt", "..%2fsecret.txt", "secret.txt",
                "ok.txt", "", "/etc/passwd", "....//ok.jpg"):
        r = client.get("/ui/screensaver/photo/local", params={"name": bad})
        assert r.status_code == 404, bad


def test_local_serve_route_404_when_source_not_folder(client, monkeypatch, tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "ok.jpg").write_bytes(b"img")
    # photo_source stays built-in: the route must refuse even a valid name.
    r = client.get("/ui/screensaver/photo/local", params={"name": "ok.jpg"})
    assert r.status_code == 404


def test_immich_proxy_refuses_without_config_or_bad_id(client, monkeypatch):
    r = client.get("/ui/screensaver/photo/immich",
                   params={"id": "11111111-2222-3333-4444-555555555555"})
    assert r.status_code == 404   # source not immich / nothing configured
    monkeypatch.setattr(settings, "photo_source", "immich", raising=False)
    monkeypatch.setattr(settings, "immich_base_url", "http://im", raising=False)
    monkeypatch.setattr(settings, "immich_api_key", "k", raising=False)
    r = client.get("/ui/screensaver/photo/immich",
                   params={"id": "../../etc/passwd"})
    assert r.status_code == 404   # non-UUID id never reaches the network


def test_photos_test_endpoint_urls_and_folder(client, tmp_path):
    r = client.post("/ui/screensaver/photos/test", json={
        "photo_source": "urls",
        "photo_urls": "https://pics.example/a.jpg\nhttps://pics.example/b.jpg",
    })
    data = r.json()
    assert data["ok"] is True and data["count"] == 2
    assert data["sample"] == "https://pics.example/a.jpg"

    folder = tmp_path / "elsewhere"
    folder.mkdir()
    (folder / "one.png").write_bytes(b"x")
    r = client.post("/ui/screensaver/photos/test", json={
        "photo_source": "folder", "photo_folder": str(folder)})
    data = r.json()
    assert data["ok"] is True and data["count"] == 1

    r = client.post("/ui/screensaver/photos/test", json={
        "photo_source": "folder", "photo_folder": str(tmp_path / "missing")})
    data = r.json()
    assert data["ok"] is False and "does not exist" in data["detail"]

    r = client.post("/ui/screensaver/photos/test", json={
        "photo_source": "built-in"})
    assert r.json()["ok"] is False


# --------------------------------------------------------------------- /save

def test_save_normalizes_photo_source_and_keeps_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "immich_api_key", "stored-key", raising=False)
    with _configured():
        r = client.post("/setup/save", json={
            "photo_source": "google-photos",   # unknown: falls back
            "photo_folder": "/mnt/pics",
            "immich_api_key": "",              # blank keeps the stored key
            "photo_urls": "https://x.example/a.jpg",
        })
    assert r.status_code == 200
    assert settings.photo_source == "built-in"
    assert settings.photo_folder == "/mnt/pics"
    assert settings.immich_api_key == "stored-key"
    assert settings.photo_urls == "https://x.example/a.jpg"
    with _configured():
        client.post("/setup/save", json={"photo_source": "immich",
                                         "immich_api_key": "new-key"})
    assert settings.photo_source == "immich"
    assert settings.immich_api_key == "new-key"
    with _configured():
        client.post("/setup/save", json={"photo_source": "folder",
                                         "immich_api_key": "__CLEAR__"})
    assert settings.photo_source == "folder"
    assert settings.immich_api_key == ""
