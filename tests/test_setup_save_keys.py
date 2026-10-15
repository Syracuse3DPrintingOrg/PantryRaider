"""/setup/save in 0.21: API keys, the tunnel password, Home Assistant keys.

- Once set up, keys are managed in Settings, Security: an api_key or
  extra_api_keys in a save is dropped with errors.api_keys and every other
  field still saves. The first-time wizard can still set the first key.
- While remote access is on, the password cannot be turned off or cleared.
- Home Assistant keys go through the key policy: an invalid key drops only
  that list (the stored keys stay), an elevated key without the owner's
  permission saves marked needs_review and is listed under review, and an
  older editor that does not know allow_elevated never loses it for a key
  whose calls did not change.
- A pasted Home Assistant token is a manual one; a Home Assistant camera
  loses any token address; the deck is brought up to date.
- Security settings are not SetupPayload fields; they change nothing here.
- Nothing to report answers exactly {"ok": True}.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402

LAN_PEER = ("192.168.1.20", 50000)

LOCK = {"id": "c_lock", "type": "ha_action", "entity_id": "lock.front_door",
        "service": "lock.unlock", "label": "Front door", "slot": 3}
LIGHT = {"id": "c_light", "type": "ha_action", "entity_id": "light.kitchen",
         "service": "light.turn_on", "label": "Kitchen", "slot": 4}
BAD = {"id": "c_bad", "type": "ha_action", "entity_id": "all",
       "service": "light.turn_on", "label": "Everything", "slot": 5}


@pytest.fixture
def setup_env(monkeypatch, tmp_path):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "tunnel_enabled", False, raising=False)
    monkeypatch.setattr(settings, "api_key", "stored-primary-key", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_cameras", [], raising=False)
    pushes = []

    async def fake_push():
        pushes.append(1)
        return False
    from app.services import deck_config
    monkeypatch.setattr(deck_config, "apush_to_deck", fake_push)
    return pushes


def _client(configured: bool):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app, client=LAN_PEER), patch.object(
        type(settings), "is_configured", lambda self: configured)


@pytest.fixture
def client(setup_env):
    c, p = _client(True)
    with p:
        yield c


# -- API keys -------------------------------------------------------------------

def test_configured_install_refuses_keys_and_saves_the_rest(client):
    r = client.post("/setup/save", json={
        "api_key": "new-primary", "extra_api_keys": [{"key": "sat-key", "name": "Sat"}],
        "expiring_soon_days": 4})
    body = r.json()
    assert body["ok"] is True
    assert body["errors"]["api_keys"] == "Keys are managed in Settings, Security."
    assert settings.api_key == "stored-primary-key"
    assert settings.extra_api_keys == []
    assert settings.expiring_soon_days == 4


def test_a_blank_api_key_is_not_an_error(client):
    r = client.post("/setup/save", json={"api_key": "", "expiring_soon_days": 5})
    assert r.json() == {"ok": True}
    assert settings.api_key == "stored-primary-key"


def test_unconfigured_wizard_can_still_set_the_key(setup_env):
    c, p = _client(False)
    with p:
        r = c.post("/setup/save", json={"api_key": "first-key"})
    assert "errors" not in r.json()
    assert settings.api_key == "first-key"


# -- remote access needs the password ---------------------------------------------

def test_password_cannot_be_turned_off_while_the_tunnel_is_on(client, monkeypatch):
    monkeypatch.setattr(settings, "tunnel_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    r = client.post("/setup/save", json={"auth_required": False, "expiring_soon_days": 6})
    body = r.json()
    assert body["errors"]["auth"] == ("Turn off remote access first: a kitchen reachable "
                                      "from the internet needs a password.")
    assert settings.auth_required is True
    assert settings.expiring_soon_days == 6


def test_password_cannot_be_cleared_while_the_tunnel_is_on(client, monkeypatch):
    monkeypatch.setattr(settings, "tunnel_enabled", True, raising=False)
    r = client.post("/setup/save", json={"auth_password": "__CLEAR__", "perishable_days": 9})
    body = r.json()
    assert "auth" in body["errors"]
    assert settings.perishable_days == 9


# -- Home Assistant keys ----------------------------------------------------------

def test_invalid_override_drops_only_the_overrides(client, monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [dict(LIGHT)], raising=False)
    r = client.post("/setup/save", json={
        "streamdeck_key_overrides": [dict(LIGHT), dict(BAD)], "expiring_soon_days": 3})
    body = r.json()
    errs = body["errors"]["streamdeck_key_overrides"]
    assert [e["label"] for e in errs] == ["Everything"]
    assert [o["id"] for o in settings.streamdeck_key_overrides] == ["c_light"]
    assert settings.expiring_soon_days == 3


def test_an_unchanged_stored_invalid_key_never_blocks_a_save(client, monkeypatch):
    """A key the update flagged as invalid stays as it was and does not stop
    the owner from saving other key edits, even from a page that does not
    show it. A new or changed invalid key is still refused."""
    stored_bad = dict(BAD, disabled_reason="invalid")
    monkeypatch.setattr(settings, "streamdeck_key_overrides",
                        [dict(LIGHT), stored_bad], raising=False)
    edited_light = dict(LIGHT, label="Kitchen lights")
    # The editor posts the stored invalid key back without disabled_reason.
    r = client.post("/setup/save", json={
        "streamdeck_key_overrides": [edited_light, dict(BAD)]})
    body = r.json()
    assert "streamdeck_key_overrides" not in (body.get("errors") or {})
    saved = {o["id"]: o for o in settings.streamdeck_key_overrides}
    assert saved["c_light"]["label"] == "Kitchen lights"
    assert saved["c_bad"]["disabled_reason"] == "invalid"
    # Changing the invalid key (or adding a new one) is still refused.
    r = client.post("/setup/save", json={
        "streamdeck_key_overrides": [edited_light, dict(BAD, service="light.turn_off")]})
    errs = r.json()["errors"]["streamdeck_key_overrides"]
    assert [e["label"] for e in errs] == ["Everything"]


def test_elevated_override_saves_for_review(client):
    r = client.post("/setup/save", json={"streamdeck_key_overrides": [dict(LOCK)]})
    body = r.json()
    review = body["review"]["streamdeck_key_overrides"]
    assert review[0]["id"] == "c_lock" and review[0]["service"] == "lock.unlock"
    assert settings.streamdeck_key_overrides[0]["needs_review"] is True


def test_allowed_elevated_override_saves_clean(client):
    r = client.post("/setup/save", json={
        "streamdeck_key_overrides": [dict(LOCK, allow_elevated=True)]})
    assert r.json() == {"ok": True}
    saved = settings.streamdeck_key_overrides[0]
    assert saved["allow_elevated"] is True and "needs_review" not in saved


def test_start_page_save_keeps_a_grandfathered_choice(client, monkeypatch):
    stored = dict(LOCK, allow_elevated=True, elevated_grandfathered=True)
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [stored], raising=False)
    same = {k: v for k, v in LOCK.items() if k != "slot"}
    r = client.post("/setup/save", json={
        "start_custom_defs": [same], "start_loaded_ids": ["c_lock"]})
    assert r.json() == {"ok": True}
    saved = settings.streamdeck_key_overrides[0]
    assert saved["allow_elevated"] is True and saved["elevated_grandfathered"] is True
    assert saved["slot"] == 3


def test_start_page_save_with_a_changed_service_drops_the_choice(client, monkeypatch):
    stored = dict(LOCK, allow_elevated=True, elevated_grandfathered=True)
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [stored], raising=False)
    changed = {k: v for k, v in LOCK.items() if k != "slot"}
    changed["service"] = "lock.lock"
    r = client.post("/setup/save", json={
        "start_custom_defs": [changed], "start_loaded_ids": ["c_lock"]})
    body = r.json()
    assert body["review"]["streamdeck_key_overrides"][0]["service"] == "lock.lock"
    saved = settings.streamdeck_key_overrides[0]
    assert "allow_elevated" not in saved and saved["needs_review"] is True


def test_slot_keeps_its_choice_by_position(client, monkeypatch):
    stored = [{"entity_id": "cover.garage", "service": "cover.open_cover", "label": "Garage",
               "allow_elevated": True}]
    monkeypatch.setattr(settings, "streamdeck_ha_slots", stored, raising=False)
    r = client.post("/setup/save", json={"streamdeck_ha_slots": [
        {"entity_id": "cover.garage", "service": "cover.open_cover", "label": "Garage door"}]})
    assert r.json() == {"ok": True}
    assert settings.streamdeck_ha_slots[0]["allow_elevated"] is True


# -- the Home Assistant token and cameras ---------------------------------------------

def test_a_pasted_token_is_manual(client, monkeypatch):
    from app.services import ha_client
    calls = []
    monkeypatch.setattr(ha_client, "invalidate_checks", lambda: calls.append(1))
    monkeypatch.setattr(settings, "ha_token_kind", "owner-legacy", raising=False)
    monkeypatch.setattr(settings, "ha_token_expires", "2027-01-01", raising=False)
    client.post("/setup/save", json={"streamdeck_ha_token": "pasted-token"})
    assert settings.ha_token_kind == "manual" and settings.ha_token_expires == ""
    assert calls == [1]
    client.post("/setup/save", json={"streamdeck_ha_token": "__CLEAR__"})
    assert settings.streamdeck_ha_token == "" and settings.ha_token_kind == ""


def test_a_home_assistant_camera_loses_its_token_address(client, setup_env):
    url = "http://ha.example:8123/api/camera_proxy/camera.porch?token=abc"
    r = client.post("/setup/save", json={"streamdeck_cameras": [
        {"name": "Porch", "ha_entity": "camera.porch", "snapshot_url": url,
         "stream_url": url.replace("camera_proxy", "camera_proxy_stream")},
        {"name": "Yard", "snapshot_url": "http://192.0.2.21/snap.jpg"}]})
    assert r.json() == {"ok": True}
    cams = settings.streamdeck_cameras
    assert cams[0]["snapshot_url"] == "" and cams[0]["stream_url"] == ""
    assert cams[1]["snapshot_url"] == "http://192.0.2.21/snap.jpg"
    assert setup_env == [1]  # the deck was brought up to date


def test_no_deck_push_for_unrelated_fields(client, setup_env):
    client.post("/setup/save", json={"expiring_soon_days": 2})
    assert setup_env == []


# -- security settings are not saved here ----------------------------------------------

def test_security_fields_are_not_setup_fields(client, monkeypatch):
    monkeypatch.setattr(settings, "kiosk_screen_is_admin", True, raising=False)
    monkeypatch.setattr(settings, "lan_cidrs", "", raising=False)
    r = client.post("/setup/save", json={"kiosk_screen_is_admin": False,
                                         "lan_cidrs": "10.9.0.0/16"})
    assert r.json() == {"ok": True}
    assert settings.kiosk_screen_is_admin is True
    assert settings.lan_cidrs == ""


def test_nothing_to_report_is_exactly_ok(client):
    assert client.post("/setup/save", json={"perishable_days": 7}).json() == {"ok": True}


# -- Stream Deck profiles ----------------------------------------------------------------

def test_a_profile_with_an_invalid_key_is_refused_with_readable_labels(client):
    bad = dict(BAD, label="Tom's <b>lamps</b> & more")
    r = client.post("/setup/streamdeck/profiles", json={
        "name": "zz-r2-refused-profile", "deck_size": 15, "key_overrides": [bad]})
    body = r.json()
    assert body["ok"] is False
    # The editor escapes this text itself, so it carries no HTML escape codes;
    # angle brackets are dropped so an older open tab cannot render markup.
    assert body["error"] == ("The profile has a Home Assistant key that is not valid: "
                             "Tom's blamps/b & more. Fix it and save again.")
    names = [p.get("name") for p in client.get("/setup/streamdeck/profiles").json()
             .get("profiles", [])]
    assert "zz-r2-refused-profile" not in names
