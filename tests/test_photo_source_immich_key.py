"""The saved Immich key only goes to the saved Immich server.

POST /ui/screensaver/photos/test tries the photo settings in the form before
they are saved. Leaving the key field blank uses the saved key, which is what
lets the owner test a saved setup without typing the key again. The address
is taken from the form, though, so anyone who can open a kitchen page could
send the saved key to a server of their choosing. The saved key is now used
only with the saved address; a different address needs its key typed in.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.services import photo_source as ps

SERVICE = Path(__file__).resolve().parents[1] / "service"
ALBUM = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def sent(monkeypatch, tmp_path):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "photo_source", "immich", raising=False)
    monkeypatch.setattr(settings, "immich_base_url", "http://immich.lan:2283/", raising=False)
    monkeypatch.setattr(settings, "immich_api_key", "saved-immich-key", raising=False)
    monkeypatch.setattr(settings, "immich_album_id", ALBUM, raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True)
    calls = []

    async def fake_ids(base_url, api_key, album_id):
        calls.append((base_url, api_key))
        return ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]

    monkeypatch.setattr(ps, "immich_album_asset_ids", fake_ids)
    return calls


def _test(body):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app).post("/ui/screensaver/photos/test",
                                json={"photo_source": "immich", "immich_album_id": ALBUM, **body})


def test_the_saved_key_tests_the_saved_server(sent):
    r = _test({"immich_base_url": "http://immich.lan:2283", "immich_api_key": ""})
    assert r.json()["ok"] is True
    assert sent == [("http://immich.lan:2283", "saved-immich-key")]


def test_the_saved_key_never_goes_to_another_server(sent):
    r = _test({"immich_base_url": "http://collector.example", "immich_api_key": ""})
    data = r.json()
    assert data["ok"] is False and data["count"] == 0
    assert "API key" in data["detail"]
    assert sent == []


def test_a_typed_key_tests_any_server(sent):
    r = _test({"immich_base_url": "http://immich-new.lan:2283", "immich_api_key": "typed-key"})
    assert r.json()["ok"] is True
    assert sent == [("http://immich-new.lan:2283", "typed-key")]
