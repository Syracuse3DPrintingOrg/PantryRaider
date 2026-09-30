"""The Stream Deck config the Settings page reads back carries no secrets.

GET /setup/streamdeck/config blanks the Home Assistant token (adding
ha_token_set so the page can still say one is stored) and any camera snapshot
URL with a login in it (adding relay), in every deployment mode. The POST
re-stamps both from settings, so a page that posts the blanked config back,
new or old, never wipes the deck's token or camera logins.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.routers import setup as setup_router  # noqa: E402
from app.services.deck_config import redact_deck_config, strip_page_only_keys  # noqa: E402

_TOKEN = "ha-long-lived-token-abc"
_CRED_SNAP = "http://admin:hunter2@192.0.2.20/cgi-bin/api.cgi?cmd=Snap"


def _deck_config():
    return {
        "rotation": 90, "brightness": 60, "keys": ["inventory", "cook"],
        "ha_base_url": "http://ha.example:8123", "ha_token": _TOKEN,
        "cameras": [
            {"name": "Door", "snapshot_url": _CRED_SNAP, "ha_entity": ""},
            {"name": "Yard", "snapshot_url": "http://192.0.2.21/snap.jpg", "ha_entity": ""},
            {"name": "Porch", "snapshot_url": "", "ha_entity": "camera.porch"},
        ],
    }


# -- redact_deck_config -------------------------------------------------------

def test_redact_blanks_token_and_flags_it():
    out = redact_deck_config(_deck_config())
    assert out["ha_token"] == "" and out["ha_token_set"] is True
    assert redact_deck_config({"ha_token": ""})["ha_token_set"] is False
    assert redact_deck_config({})["ha_token_set"] is False


def test_redact_blanks_credentialed_camera_urls_only():
    cams = redact_deck_config(_deck_config())["cameras"]
    assert cams[0] == {"name": "Door", "snapshot_url": "", "ha_entity": "", "relay": True}
    assert cams[1]["snapshot_url"] == "http://192.0.2.21/snap.jpg" and "relay" not in cams[1]
    assert cams[2]["ha_entity"] == "camera.porch"


def test_redact_leaves_the_input_alone_and_keeps_layout():
    cfg = _deck_config()
    out = redact_deck_config(cfg)
    assert cfg["ha_token"] == _TOKEN and cfg["cameras"][0]["snapshot_url"] == _CRED_SNAP
    assert out["rotation"] == 90 and out["keys"] == ["inventory", "cook"]
    assert _TOKEN not in repr(out) and "hunter2" not in repr(out)


def test_redact_passes_through_non_dicts():
    assert redact_deck_config(None) is None
    assert redact_deck_config(["x"]) == ["x"]


def test_strip_page_only_keys():
    assert strip_page_only_keys({"ha_token_set": True, "rotation": 0}) == {"rotation": 0}


# -- the routes ---------------------------------------------------------------

class _Resp:
    def __init__(self, status, content):
        self.status_code = status
        self._content = content

    def json(self):
        return self._content


class _Bridge:
    """Stands in for the host bridge: GET returns the deck config, POST records
    what would be written to config.toml."""
    def __init__(self, config):
        self.config = config
        self.posted = []

    def __call__(self, timeout=None):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **k):
        return _Resp(200, {"ok": True, "config": dict(self.config)})

    async def post(self, url, json=None, **k):
        self.posted.append(json)
        return _Resp(200, {"ok": True})


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def bridge(monkeypatch):
    b = _Bridge(_deck_config())
    monkeypatch.setattr(setup_router, "_HOST_BRIDGE", "http://127.0.0.1:9299")
    monkeypatch.setattr(setup_router, "bridge_client", b)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "http://ha.example:8123", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", _TOKEN, raising=False)
    monkeypatch.setattr(settings, "streamdeck_cameras", [
        {"name": "Door", "snapshot_url": _CRED_SNAP},
        {"name": "Reo", "source": "reolink", "host": "192.0.2.30",
         "username": "admin", "password": "s3cret"},
    ], raising=False)
    return b


def _json(resp):
    import json
    return json.loads(resp.body)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["server", "pi_hosted", "pi_remote"])
async def test_get_is_redacted_in_every_mode(bridge, monkeypatch, mode):
    monkeypatch.setattr(settings, "deployment_mode", mode, raising=False)
    resp = await setup_router.streamdeck_config_get()
    body = _json(resp)
    assert resp.status_code == 200 and body["ok"] is True
    assert body["config"]["ha_token"] == "" and body["config"]["ha_token_set"] is True
    assert body["config"]["cameras"][0]["snapshot_url"] == ""
    assert _TOKEN not in resp.body.decode() and "hunter2" not in resp.body.decode()


@pytest.mark.anyio
async def test_posting_the_redacted_config_back_keeps_the_secrets(bridge, monkeypatch):
    # What every settings pane does: GET, merge its edits, POST back.
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    cfg = _json(await setup_router.streamdeck_config_get())["config"]
    cfg["brightness"] = 80
    await setup_router.streamdeck_config_set(_Req({"config": cfg}))
    written = bridge.posted[-1]["config"]
    assert written["ha_token"] == _TOKEN
    assert "ha_token_set" not in written
    assert written["brightness"] == 80
    snaps = {c["name"]: c["snapshot_url"] for c in written["cameras"]}
    assert snaps["Door"] == _CRED_SNAP
    assert "s3cret" in snaps["Reo"]  # the deck fetches on the LAN and needs it
    assert all("relay" not in c for c in written["cameras"])


@pytest.mark.anyio
async def test_older_page_posting_a_full_config_still_works(bridge, monkeypatch):
    # A page cached from before this release posts the token field it read
    # (now ""); the server stamps the stored one regardless.
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    await setup_router.streamdeck_config_set(_Req({"config": {"ha_token": "", "rotation": 0}}))
    assert bridge.posted[-1]["config"]["ha_token"] == _TOKEN
