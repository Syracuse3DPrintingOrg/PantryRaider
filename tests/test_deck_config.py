"""The Stream Deck config the app writes (services/deck_config.py, I-12).

merge_deck_config is pure: the proxy form once the deck has proven its agent
key (Home Assistant through this app, cameras through this app, no token and
no camera login in config.toml), the legacy form before that (today's stamp).
Both run every Home Assistant key through the key policy for the deck, and
neither ever touches the deck's api_key. push_to_deck never posts after a
failed read, posts nothing when nothing changed, and records each push in
data_dir/deck-push.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import deck_config, local_agents  # noqa: E402

AGENT_KEY = "pra1.streamdeck." + "a" * 64
ORIGIN = "http://localhost:9284"
HA_TOKEN = "ha-long-lived-token-xyz"
REO_PASS = "reo-pass-123"
CRED_SNAP = "http://admin:hunter2@192.0.2.20/cgi-bin/snap.cgi"

LOCK = {"id": "c_lock", "type": "ha_action", "entity_id": "lock.front_door",
        "service": "lock.unlock", "label": "Door", "slot": 3}
LIGHT = {"id": "c_light", "type": "ha_action", "entity_id": "light.kitchen",
         "service": "light.turn_on", "label": "Light", "slot": 4}
BAD = {"id": "c_bad", "type": "ha_action", "entity_id": "all",
       "service": "light.turn_on", "label": "All", "slot": 5}

CAMERAS = [
    {"name": "Porch Cam", "ha_entity": "camera.porch", "snapshot_url": ""},
    {"name": "Reo", "source": "reolink", "host": "192.0.2.30", "username": "admin",
     "password": REO_PASS, "channel": 0},
    {"name": "Garage", "snapshot_url": CRED_SNAP},
    {"name": "Yard", "snapshot_url": "", "relay": True},
    {"name": "Clean", "snapshot_url": "http://192.0.2.40/snap.jpg"},
]


def _inputs(**over):
    base = {
        "weather_location": "Syracuse", "weather_units": "imperial", "theme": "dark",
        "key_style": "rich", "icon_color": "full", "ha_base_url": "http://ha.example:8123",
        "ha_token": HA_TOKEN, "ha_slots": [], "cameras": [dict(c) for c in CAMERAS],
        "key_overrides": [], "idle_timeout_minutes": 5, "logo_when_display_off": True,
        "clock_format": "12", "is_satellite": False, "agent_key": "", "proxy_origin": "",
        "policy_enforced": False,
    }
    base.update(over)
    return base


def _proxy(**over):
    return _inputs(agent_key=AGENT_KEY, proxy_origin=ORIGIN, **over)


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "http://ha.example:8123", raising=False)
    deck_config._mem.update(path=None, mtime=None, data=None)
    yield tmp_path
    deck_config._mem.update(path=None, mtime=None, data=None)


# -- forms ----------------------------------------------------------------------

def test_form_follows_the_inputs():
    assert deck_config.merge_deck_config({}, _inputs())[1] == "legacy"
    assert deck_config.merge_deck_config({}, _inputs(agent_key=AGENT_KEY))[1] == "legacy"
    assert deck_config.merge_deck_config({}, _inputs(proxy_origin=ORIGIN))[1] == "legacy"
    assert deck_config.merge_deck_config({}, _proxy())[1] == "proxy"


@pytest.mark.parametrize("make", [_inputs, _proxy])
def test_merge_never_adds_or_changes_api_key(make):
    merged, _ = deck_config.merge_deck_config({"api_key": "deck-own-key"}, make())
    assert merged["api_key"] == "deck-own-key"
    merged, _ = deck_config.merge_deck_config({}, make())
    assert "api_key" not in merged


def test_proxy_form_holds_the_agent_key_and_no_secret():
    merged, form = deck_config.merge_deck_config({"rotation": 90}, _proxy())
    assert form == "proxy"
    assert merged["ha_base_url"] == ORIGIN + "/api/proxy/ha"
    assert merged["ha_token"] == AGENT_KEY
    text = json.dumps(merged)
    assert HA_TOKEN not in text
    assert REO_PASS not in text
    assert "user:pass@" not in text and "hunter2" not in text
    assert "?token=" not in text
    assert merged["rotation"] == 90


def test_proxy_form_camera_mapping():
    cams = deck_config.merge_deck_config({}, _proxy())[0]["cameras"]
    assert cams[0] == {"name": "Porch Cam",
                       "snapshot_url": ORIGIN + "/ui/camera/0/snapshot?name=Porch%20Cam",
                       "ha_entity": ""}
    assert cams[1]["snapshot_url"] == ORIGIN + "/ui/camera/1/snapshot?name=Reo"
    assert cams[2]["snapshot_url"] == ORIGIN + "/ui/camera/2/snapshot?name=Garage"
    assert cams[3]["snapshot_url"] == ORIGIN + "/ui/camera/3/snapshot?name=Yard"
    assert cams[4] == {"name": "Clean", "snapshot_url": "http://192.0.2.40/snap.jpg",
                       "ha_entity": ""}


def test_proxy_camera_index_counts_every_saved_entry():
    inp = _proxy(cameras=["junk", {"name": "A/B", "ha_entity": "camera.a"}])
    cams = deck_config.merge_deck_config({}, inp)[0]["cameras"]
    assert cams == [{"name": "A/B", "snapshot_url": ORIGIN + "/ui/camera/1/snapshot?name=A%2FB",
                     "ha_entity": ""}]


def test_legacy_form_is_todays_stamp_on_pi_hosted():
    merged, form = deck_config.merge_deck_config({}, _inputs())
    assert form == "legacy"
    assert merged["ha_base_url"] == "http://ha.example:8123"
    assert merged["ha_token"] == HA_TOKEN
    cams = {c["name"]: c for c in merged["cameras"]}
    assert REO_PASS in cams["Reo"]["snapshot_url"]
    assert cams["Porch Cam"]["ha_entity"] == "camera.porch"
    assert cams["Garage"]["snapshot_url"] == CRED_SNAP


def test_legacy_form_on_a_satellite_does_not_compose_reolink_urls():
    merged, _ = deck_config.merge_deck_config({}, _inputs(is_satellite=True))
    cams = {c["name"]: c for c in merged["cameras"]}
    assert cams["Reo"]["snapshot_url"] == ""
    assert REO_PASS not in json.dumps(merged)


def test_weather_is_overlaid_only_on_a_satellite():
    cfg = {"weather_location": "Local Town", "weather_units": "metric"}
    merged, _ = deck_config.merge_deck_config(cfg, _inputs())
    assert merged["weather_location"] == "Local Town" and merged["weather_units"] == "metric"
    merged, _ = deck_config.merge_deck_config(cfg, _inputs(is_satellite=True))
    assert merged["weather_location"] == "Syracuse" and merged["weather_units"] == "imperial"


def test_stamped_device_fields_and_rotation():
    merged, _ = deck_config.merge_deck_config({"rotation": 0}, _inputs(), rotation=180)
    assert merged["theme"] == "dark" and merged["clock_format"] == "12"
    assert merged["idle_timeout_minutes"] == 5 and merged["logo_when_display_off"] is True
    assert merged["rotation"] == 180
    assert deck_config.merge_deck_config({"rotation": 90}, _inputs(), rotation=45)[0]["rotation"] == 90
    assert deck_config.merge_deck_config({"rotation": 90}, _inputs())[0]["rotation"] == 90


def test_key_overrides_come_from_inputs_not_the_posted_config():
    cfg = {"key_overrides": [{"id": "posted", "type": "default", "slot": 1}]}
    merged, _ = deck_config.merge_deck_config(cfg, _inputs(key_overrides=[LIGHT]))
    assert [o.get("id") for o in merged["key_overrides"]] == ["c_light"]


# -- the key policy -------------------------------------------------------------

def test_enforced_policy_filters_an_unallowed_elevated_override():
    merged, _ = deck_config.merge_deck_config(
        {}, _inputs(key_overrides=[LOCK, LIGHT], policy_enforced=True))
    assert merged["key_overrides"][0] == {"type": "default", "slot": 3}
    assert merged["key_overrides"][1]["id"] == "c_light"
    assert merged["ha_policy_version"] == 1


def test_enforced_policy_keeps_an_allowed_elevated_override():
    allowed = dict(LOCK, allow_elevated=True, elevated_grandfathered=True)
    merged, _ = deck_config.merge_deck_config(
        {}, _proxy(key_overrides=[allowed], policy_enforced=True))
    kept = merged["key_overrides"][0]
    assert kept["allow_elevated"] is True and kept["elevated_grandfathered"] is True


def test_policy_off_keeps_elevated_and_writes_zero():
    merged, _ = deck_config.merge_deck_config({}, _inputs(key_overrides=[LOCK]))
    assert merged["key_overrides"][0]["id"] == "c_lock"
    assert merged["ha_policy_version"] == 0


@pytest.mark.parametrize("enforced", [False, True])
def test_invalid_entries_are_always_filtered(enforced):
    merged, _ = deck_config.merge_deck_config(
        {}, _inputs(key_overrides=[BAD, "junk"], policy_enforced=enforced))
    assert merged["key_overrides"] == [{"type": "default", "slot": 5}]


def test_slots_keep_their_positions():
    slots = [{"entity_id": "light.kitchen", "service": "light.turn_on", "label": "A"},
             {"entity_id": "lock.front_door", "service": "lock.unlock", "label": "B"},
             {"entity_id": "switch.fan", "service": "switch.turn_on", "label": "C"}]
    merged, _ = deck_config.merge_deck_config({}, _inputs(ha_slots=slots, policy_enforced=True))
    assert merged["ha_slots"][0]["entity_id"] == "light.kitchen"
    assert merged["ha_slots"][1] == {"entity_id": "", "service": "", "label": "B"}
    assert merged["ha_slots"][2]["entity_id"] == "switch.fan"


# -- inputs and the latch -------------------------------------------------------

def test_latch_keeps_the_proxy_form_for_the_same_key(monkeypatch):
    monkeypatch.setattr(local_agents, "deck_proven", lambda *a, **k: None)
    monkeypatch.setattr(local_agents, "agent_key", lambda agent: AGENT_KEY)
    assert deck_config.deck_inputs()["proxy_origin"] == ""
    deck_config.record_push("proxy", AGENT_KEY, ORIGIN)
    assert deck_config.deck_inputs()["proxy_origin"] == ORIGIN
    monkeypatch.setattr(local_agents, "agent_key", lambda agent: "pra1.streamdeck." + "b" * 64)
    assert deck_config.deck_inputs()["proxy_origin"] == ""


def test_proven_origin_wins(monkeypatch):
    monkeypatch.setattr(local_agents, "deck_proven",
                        lambda *a, **k: {"origin": "http://127.0.0.1:9284", "at": "x"})
    monkeypatch.setattr(local_agents, "agent_key", lambda agent: AGENT_KEY)
    inp = deck_config.deck_inputs()
    assert inp["proxy_origin"] == "http://127.0.0.1:9284" and inp["agent_key"] == AGENT_KEY


def test_no_bridge_token_means_no_agent_key(monkeypatch):
    monkeypatch.setattr(local_agents, "agent_key", lambda agent: None)
    assert deck_config.deck_inputs()["agent_key"] == ""


def test_push_record_is_private(_data_dir):
    deck_config.record_push("proxy", AGENT_KEY, ORIGIN)
    f = _data_dir / "deck-push.json"
    assert (f.stat().st_mode & 0o777) == 0o600
    lp = deck_config.last_push()
    assert lp["form"] == "proxy" and lp["origin"] == ORIGIN
    assert AGENT_KEY not in f.read_text()
    assert deck_config.last_push_form() == "proxy"


# -- push_to_deck ----------------------------------------------------------------

class _R:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest.fixture
def on_pi(monkeypatch):
    from app import hardware
    monkeypatch.setattr(hardware, "is_raspberry_pi", lambda: True)
    monkeypatch.setattr(settings, "has_streamdeck", True, raising=False)
    monkeypatch.setattr(settings, "streamdeck_rotation", 0, raising=False)
    monkeypatch.setattr(local_agents, "deck_proven",
                        lambda *a, **k: {"origin": ORIGIN, "at": "x"})
    monkeypatch.setattr(local_agents, "agent_key", lambda agent: AGENT_KEY)


def _fake_http(monkeypatch, get_resp):
    import httpx
    posts = []
    monkeypatch.setattr(httpx, "get", lambda *a, **k: get_resp)

    def post(url, json=None, **k):
        posts.append(json)
        return _R(200, {"ok": True})
    monkeypatch.setattr(httpx, "post", post)
    return posts


def test_push_off_a_pi_does_nothing(monkeypatch):
    from app import hardware
    monkeypatch.setattr(hardware, "is_raspberry_pi", lambda: False)
    posts = _fake_http(monkeypatch, _R(200, {"config": {}}))
    assert deck_config.push_to_deck() is False
    assert posts == []


@pytest.mark.parametrize("resp", [_R(500, {"ok": False}), _R(401, {}), _R(200, {"ok": True}),
                                  _R(200, None)])
def test_a_failed_read_never_posts(on_pi, monkeypatch, resp):
    posts = _fake_http(monkeypatch, resp)
    assert deck_config.push_to_deck() is False
    assert posts == []


def test_a_successful_push_records_form_key_hash_and_origin(on_pi, monkeypatch):
    import hashlib
    posts = _fake_http(monkeypatch, _R(200, {"ok": True, "config": {"rotation": 0,
                                                                    "api_key": "keep"}}))
    assert deck_config.push_to_deck() is True
    assert len(posts) == 1
    written = posts[0]["config"]
    assert written["ha_token"] == AGENT_KEY and written["api_key"] == "keep"
    lp = deck_config.last_push()
    assert lp["form"] == "proxy" and lp["origin"] == ORIGIN
    assert lp["key_sha256"] == hashlib.sha256(AGENT_KEY.encode()).hexdigest()


def test_an_unchanged_config_is_not_posted(on_pi, monkeypatch):
    current, _ = deck_config.merge_deck_config({"rotation": 0}, deck_config.deck_inputs())
    posts = _fake_http(monkeypatch, _R(200, {"ok": True, "config": current}))
    assert deck_config.push_to_deck() is True
    assert posts == []
    assert deck_config.last_push_form() == "proxy"
