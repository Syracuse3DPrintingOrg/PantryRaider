"""What a main server hands each satellite on its config pull (0.21).

A satellite from 0.21 sends relay1 and gets a payload without the AI and
recipe keys, plus a relay block. An older satellite keeps getting them while
the owner's policy (auto, allow or off) lets it. A key is ratcheted (pulls
without relay1 get the cut-off payload) only once every device that pulled
with it in 30 days has sent relay1, and never for the primary key.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings, SATELLITE_PULL_FIELDS  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import db_models  # noqa: E402,F401
from app.services import fleet_relay  # noqa: E402

AI_KEY = "AIzaFAKEgeminiKEYnotreal0000"
SPOON_KEY = "spoon-fake-key-0000"
HA_TOKEN = "eyFAKE.ha.token"
REOLINK_PW = "reolink-pw-fake"

CAMERAS = [
    {"name": "Door", "snapshot_url": "http://user:pass@192.168.1.60/snap.jpg"},
    {"name": "Yard", "source": "reolink", "host": "192.168.1.61", "username": "admin",
     "password": REOLINK_PW, "channel": 0},
    {"name": "Porch", "snapshot_url": "http://ha.local:8123/api/camera_proxy/camera.porch?token=abc"},
    {"name": "Garden", "snapshot_url": "http://192.168.1.62/still.jpg"},
    "not-a-dict",
]


def _values(**over):
    v = {f: "" for f in SATELLITE_PULL_FIELDS}
    v.update(gemini_api_key=AI_KEY, spoonacular_api_key=SPOON_KEY,
             streamdeck_ha_token=HA_TOKEN, streamdeck_cameras=list(CAMERAS),
             grocy_base_url="http://server:9383")
    v.update(over)
    return v


def _secrets_in(cfg) -> set:
    text = json.dumps(cfg)
    found = set()
    for name, needle in (("ai", AI_KEY), ("recipe", SPOON_KEY), ("ha", HA_TOKEN),
                         ("reolink", REOLINK_PW), ("userinfo", "user:pass@"),
                         ("token_url", "?token=")):
        if needle in text:
            found.add(name)
    return found


# --- fleet_block --------------------------------------------------------------------


def test_fleet_block_carries_the_password_state(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "satellite_legacy_credentials", "auto")
    b = fleet_relay.fleet_block(settings)
    assert b["v"] == 1
    assert b["caps"] == ["ai", "recipes_external", "ha", "camera"]
    assert b["server_has_password"] is False
    assert b["legacy_credentials"] == "auto"
    monkeypatch.setattr(settings, "auth_password", "set")
    assert fleet_relay.fleet_block(settings)["server_has_password"] is True


def test_fleet_block_reports_what_the_server_can_do(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(settings, "enrich_provider", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "")
    b = fleet_relay.fleet_block(settings)
    assert b["ai_available"] is False and b["enrich_available"] is False
    assert b["ha_available"] is False
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "http://ha.local:8123")
    monkeypatch.setattr(settings, "streamdeck_ha_token", "t")
    b = fleet_relay.fleet_block(settings)
    assert b["ai_available"] is True and b["enrich_available"] is True
    assert b["ha_available"] is True


# --- build_satellite_config -------------------------------------------------------


@pytest.mark.parametrize("relay", [True, False])
@pytest.mark.parametrize("deck_proxy", [True, False])
@pytest.mark.parametrize("service_legacy", [True, False])
@pytest.mark.parametrize("deck_legacy", [True, False])
def test_build_matrix(relay, deck_proxy, service_legacy, deck_legacy):
    block = {"v": 1, "caps": ["ai"]}
    cfg = fleet_relay.build_satellite_config(
        _values(), relay=relay, deck_proxy=deck_proxy, service_legacy=service_legacy,
        deck_legacy=deck_legacy, block=block)
    found = _secrets_in(cfg)
    assert "grocy_api_key" not in cfg and "mealie_api_key" not in cfg
    assert cfg["grocy_base_url"] == "http://server:9383"
    service_sent = (not relay) and service_legacy
    if relay:
        token_sent = (not deck_proxy) and deck_legacy
    else:
        token_sent = deck_legacy
    assert ("ai" in found) is service_sent
    assert ("recipe" in found) is service_sent
    assert ("ha" in found) is token_sent
    # The camera logins go exactly when the token does.
    assert ("reolink" in found) is token_sent
    assert ("userinfo" in found) is token_sent
    assert ("token_url" in found) is token_sent
    cams = cfg["streamdeck_cameras"]
    assert len(cams) == len(CAMERAS)
    names = [c.get("name") if isinstance(c, dict) else None for c in cams]
    assert names[:4] == ["Door", "Yard", "Porch", "Garden"]
    if relay:
        assert cfg["fleet_relay"] == block
        for f in fleet_relay.SERVICE_FIELDS:
            assert f not in cfg
        assert ("streamdeck_ha_token" in cfg) is token_sent
    else:
        assert "fleet_relay" not in cfg
        # An older satellite gets every field, blanked where withheld, so it
        # drops a value it held before.
        for f in fleet_relay.SERVICE_FIELDS:
            assert f in cfg
        assert cfg["streamdeck_ha_token"] == (HA_TOKEN if token_sent else "")
    if not token_sent:
        assert cams[3]["snapshot_url"] == "http://192.168.1.62/still.jpg"
        assert cams[1]["relay"] is True and cams[4] == {}


def test_build_never_adds_backend_keys():
    cfg = fleet_relay.build_satellite_config(
        _values(grocy_api_key="g", mealie_api_key="m"), relay=False, deck_proxy=False,
        service_legacy=True, deck_legacy=True, block=None)
    assert "grocy_api_key" not in cfg and "mealie_api_key" not in cfg


# --- policy_from --------------------------------------------------------------------

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _row(caps="", days=1, mode="pi_remote", source="heartbeat"):
    return {"caps": caps.split(",") if caps else [], "deployment_mode": mode,
            "source": source,
            "last_seen": (NOW - timedelta(days=days)).isoformat(timespec="seconds")}


@pytest.mark.parametrize("setting, rows, kind, expected", [
    ("allow", [_row("relay1,deckproxy1")], "service", "allow"),
    ("off", [], "service", "off"),
    ("off", [_row()], "deck", "off"),
    ("auto", [], "service", "allow"),
    ("auto", [], "deck", "allow"),
    ("auto", [_row("relay1"), _row("")], "service", "allow"),
    ("auto", [_row("relay1"), _row("relay1")], "service", "off"),
    ("auto", [_row("relay1"), _row("relay1")], "deck", "allow"),
    ("auto", [_row("relay1,deckproxy1"), _row("deckproxy1,relay1")], "deck", "off"),
    # A satellite not seen for over 30 days no longer holds the policy open.
    ("auto", [_row("relay1"), _row("", days=31)], "service", "off"),
    # Scan rows and other modes never count.
    ("auto", [_row("relay1"), _row("", source="scan")], "service", "off"),
    ("auto", [_row("relay1"), _row("", mode="pi_hosted")], "service", "off"),
    ("auto", [_row("", source="scan")], "service", "allow"),
    ("anything-else", [_row("relay1")], "service", "off"),
])
def test_policy_truth_table(setting, rows, kind, expected):
    assert fleet_relay.policy_from(setting, rows, kind, NOW) == expected


def test_a_fresh_install_with_no_satellites_shares_nothing():
    assert fleet_relay.policy_from("auto", [], "service", NOW, fresh=True) == "off"
    assert fleet_relay.policy_from("auto", [], "deck", NOW, fresh=True) == "off"
    # The owner's explicit choice still wins, and a seen older satellite
    # still holds auto open.
    assert fleet_relay.policy_from("allow", [], "service", NOW, fresh=True) == "allow"
    assert fleet_relay.policy_from("auto", [_row("")], "service", NOW, fresh=True) == "allow"


def test_effective_policy_reads_the_install_origin(monkeypatch):
    from app.services import auth_state
    from app.config import settings
    monkeypatch.setattr(settings, "satellite_legacy_credentials", "auto", raising=False)
    monkeypatch.setattr(fleet_relay, "_list_devices", lambda: [])
    monkeypatch.setattr(auth_state, "origin", lambda: "fresh")
    assert fleet_relay.effective_policy("service") == "off"
    monkeypatch.setattr(auth_state, "origin", lambda: "upgraded")
    assert fleet_relay.effective_policy("service") == "allow"


def test_policy_reads_caps_as_a_string_too():
    rows = [dict(_row(), caps="deckproxy1,relay1")]
    assert fleet_relay.policy_from("auto", rows, "deck", NOW) == "off"


# --- the one-time advisory -----------------------------------------------------------


@pytest.fixture
def db_session(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    from app.services import devices
    monkeypatch.setattr(devices, "SessionLocal", Session)
    fleet_relay.reset_advisory_check()
    s = Session()
    yield s
    s.close()
    fleet_relay.reset_advisory_check()


def _items(db):
    from app.models.db_models import ActionItem
    return db.query(ActionItem).filter(ActionItem.kind == fleet_relay.ADVISORY_KIND).all()


def _heartbeat(device_id, caps):
    from app.services import devices
    devices.record_heartbeat(device_id, deployment_mode="pi_remote", caps=caps,
                             ip="192.168.1.30")


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "satellite_legacy_credentials", "auto")
    monkeypatch.setattr(settings, "gemini_api_key", AI_KEY)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "")
    monkeypatch.setattr(settings, "ha_token_kind", "")


def test_advisory_once_every_satellite_relays(server, db_session):
    _heartbeat("a", ["relay1"])
    _heartbeat("b", ["relay1", "deckproxy1"])
    assert fleet_relay.maybe_raise_rotation_advisory(db_session) is True
    fleet_relay.reset_advisory_check()
    assert fleet_relay.maybe_raise_rotation_advisory(db_session) is True
    items = _items(db_session)
    assert len(items) == 1
    assert items[0].title == fleet_relay.ADVISORY_TITLE
    assert "Home Assistant" not in (items[0].body or "")


def test_advisory_under_off_does_not_claim_old_bandits_relay(server, db_session, monkeypatch):
    """With older Bandits' keys switched off, a 0.20 Bandit (no relay1) gets
    no keys and runs no AI through this kitchen either."""
    monkeypatch.setattr(settings, "satellite_legacy_credentials", "off")
    _heartbeat("a", ["relay1"])
    _heartbeat("b", [])
    assert fleet_relay.maybe_raise_rotation_advisory(db_session) is True
    body = _items(db_session)[0].body
    assert body.startswith(fleet_relay.ADVISORY_BODY_OFF)
    assert "now use this kitchen" not in body


def test_advisory_under_off_with_every_bandit_relaying(server, db_session, monkeypatch):
    monkeypatch.setattr(settings, "satellite_legacy_credentials", "off")
    _heartbeat("a", ["relay1"])
    assert fleet_relay.maybe_raise_rotation_advisory(db_session) is True
    assert _items(db_session)[0].body.startswith(fleet_relay.ADVISORY_BODY)


def test_advisory_checks_at_most_hourly(server, db_session):
    _heartbeat("a", ["relay1"])
    assert fleet_relay.maybe_raise_rotation_advisory(db_session) is True
    assert fleet_relay.maybe_raise_rotation_advisory(db_session) is False


def test_advisory_names_home_assistant_for_a_shared_token(server, db_session, monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_token", "owner-token")
    monkeypatch.setattr(settings, "ha_token_kind", "owner-legacy")
    _heartbeat("a", ["relay1"])
    assert fleet_relay.maybe_raise_rotation_advisory(db_session) is True
    assert "Home Assistant" in _items(db_session)[0].body


def test_advisory_skips_home_assistant_for_a_dedicated_token(server, db_session, monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_token", "own-token")
    monkeypatch.setattr(settings, "ha_token_kind", "dedicated")
    _heartbeat("a", ["relay1"])
    fleet_relay.maybe_raise_rotation_advisory(db_session)
    assert "Home Assistant" not in _items(db_session)[0].body


@pytest.mark.parametrize("case", ["allow", "legacy_satellite", "no_satellites", "no_keys"])
def test_no_advisory(server, db_session, monkeypatch, case):
    if case == "allow":
        monkeypatch.setattr(settings, "satellite_legacy_credentials", "allow")
        _heartbeat("a", ["relay1"])
    elif case == "legacy_satellite":
        _heartbeat("a", ["relay1"])
        _heartbeat("b", [])
    elif case == "no_keys":
        _heartbeat("a", ["relay1"])
        for f in fleet_relay.SERVICE_FIELDS:
            monkeypatch.setattr(settings, f, "")
    assert fleet_relay.maybe_raise_rotation_advisory(db_session) is False
    assert _items(db_session) == []


def test_advisory_never_raises(server, db_session, monkeypatch):
    from app.services import action_items

    def _boom(*a, **k):
        raise RuntimeError("database gone")
    monkeypatch.setattr(action_items, "create", _boom)
    _heartbeat("a", ["relay1"])
    assert fleet_relay.maybe_raise_rotation_advisory(db_session) is False


# --- the pull, through the app ------------------------------------------------------

EXTRA_KEY = "extra-satellite-key-fake-0001"
PRIMARY = "primary-key-fake-0001"


@pytest.fixture
def pull_app(monkeypatch, tmp_path, server):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.routers import satellite as sat_router
    from app.services import devices

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(devices, "SessionLocal", Session)
    monkeypatch.setattr(sat_router, "SessionLocal", Session)
    fleet_relay.reset_advisory_check()
    monkeypatch.setattr(settings, "api_key", PRIMARY)
    monkeypatch.setattr(settings, "extra_api_keys", [EXTRA_KEY])
    monkeypatch.setattr(settings, "extra_api_key_names", ["Shared"])
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "spoonacular_api_key", SPOON_KEY)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "http://ha.local:8123")
    monkeypatch.setattr(settings, "streamdeck_ha_token", HA_TOKEN)
    monkeypatch.setattr(settings, "streamdeck_cameras", list(CAMERAS[:4]))
    monkeypatch.setattr(settings, "ha_key_policy_version", 0)
    return TestClient(app, client=("192.168.1.20", 50000))


def pull(client, key, device, caps="", nonce="n0nce", **headers):
    h = {"X-API-Key": key, "X-Device-Id": device, "X-Device-Mode": "pi_remote",
         "X-Device-Version": "0.21.0" if "relay1" in caps else "0.20.0",
         "X-Config-Nonce": nonce}
    if caps:
        h["X-PR-Caps"] = caps
    h.update(headers)
    return client.get("/api/config/satellite", headers=h)


def kind_of(body) -> str:
    cfg = body["config"]
    if "fleet_relay" in cfg:
        return "relay"
    if cfg.get("gemini_api_key") == AI_KEY and cfg.get("streamdeck_ha_token") == HA_TOKEN:
        return "legacy"
    if cfg.get("gemini_api_key") == "" and cfg.get("streamdeck_ha_token") == "":
        return "cutoff"
    return "other"


def _seed_puller(key, device, days_ago=2):
    """Record an older pull, so a shared key has its day of history."""
    from app.services import credentials
    rec = credentials.lookup(key)
    credentials.mark_caps(rec["id"], [], device_id=device,
                          now=time.time() - days_ago * 86400)
    return rec["id"]


def test_extra_key_relay_then_rollback_is_cut_off(pull_app):
    _seed_puller(EXTRA_KEY, "dev-a")
    r = pull(pull_app, EXTRA_KEY, "dev-a", "relay1")
    assert r.status_code == 200
    body = r.json()
    assert kind_of(body) == "relay"
    assert body["config"]["fleet_relay"]["v"] == 1
    assert _secrets_in(body["config"]) & {"ai", "recipe"} == set()
    # The same Bandit flashed back to 0.20.x: cut off.
    body = pull(pull_app, EXTRA_KEY, "dev-a").json()
    assert kind_of(body) == "cutoff"
    assert body["config"]["spoonacular_api_key"] == ""
    assert _secrets_in(body["config"]) == set()


def test_shared_extra_key_waits_for_every_bandit(pull_app):
    _seed_puller(EXTRA_KEY, "dev-a")
    _seed_puller(EXTRA_KEY, "dev-b")
    pull(pull_app, EXTRA_KEY, "dev-b")  # B's heartbeat row, still on 0.20
    assert kind_of(pull(pull_app, EXTRA_KEY, "dev-a", "relay1").json()) == "relay"
    body = pull(pull_app, EXTRA_KEY, "dev-b").json()
    assert kind_of(body) == "legacy"
    assert body["config"]["streamdeck_cameras"] == CAMERAS[:4]
    assert kind_of(pull(pull_app, EXTRA_KEY, "dev-b", "relay1").json()) == "relay"
    # Both have updated: now a pull without relay1 is cut off under either id.
    assert kind_of(pull(pull_app, EXTRA_KEY, "dev-a").json()) == "cutoff"
    assert kind_of(pull(pull_app, EXTRA_KEY, "dev-b").json()) == "cutoff"


def test_paired_satellite_key_ratchets_on_its_first_relay_pull(pull_app):
    from app.services import credentials
    rec, secret = credentials.mint("satellite", "Bandit", "paired", device_id="dev-p")
    assert kind_of(pull(pull_app, secret, "dev-p", "relay1").json()) == "relay"
    assert credentials.relay_ratcheted(rec["id"]) is True
    assert kind_of(pull(pull_app, secret, "dev-p").json()) == "cutoff"


def test_primary_key_is_never_ratcheted(pull_app):
    from app.services import credentials
    _seed_puller(PRIMARY, "dev-a")
    assert kind_of(pull(pull_app, PRIMARY, "dev-a", "relay1").json()) == "relay"
    assert credentials.relay_ratcheted(credentials.lookup(PRIMARY)["id"]) is False
    # Without a ratchet, the policy decides: the heartbeat runs first, so this
    # pull's own missing relay1 keeps auto open for it.
    assert kind_of(pull(pull_app, PRIMARY, "dev-a").json()) == "legacy"


def test_relay_without_deckproxy_keeps_the_token_under_auto(pull_app, monkeypatch):
    cfg = pull(pull_app, PRIMARY, "dev-a", "relay1").json()["config"]
    assert cfg["streamdeck_ha_token"] == HA_TOKEN
    assert cfg["streamdeck_cameras"] == CAMERAS[:4]
    cfg = pull(pull_app, PRIMARY, "dev-a", "relay1,deckproxy1").json()["config"]
    assert "streamdeck_ha_token" not in cfg
    assert _secrets_in(cfg) == set()
    monkeypatch.setattr(settings, "satellite_legacy_credentials", "off")
    cfg = pull(pull_app, PRIMARY, "dev-a", "relay1").json()["config"]
    assert "streamdeck_ha_token" not in cfg
    assert _secrets_in(cfg) == set()
    assert len(cfg["streamdeck_cameras"]) == 4


@pytest.mark.parametrize("scope", ["ha", "ha_rest", "display"])
def test_other_scopes_are_refused(pull_app, scope):
    from app.services import access, credentials
    _rec, secret = credentials.mint(scope, "Other", "manual")
    r = pull(pull_app, secret, "dev-x")
    assert r.status_code == 403
    assert r.json()["detail"] == access.SCOPE_MSG


def test_no_key_and_no_keys_anywhere(pull_app, monkeypatch):
    r = pull_app.get("/api/config/satellite")
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid API key."
    assert pull(pull_app, "wrong-key", "dev-x").status_code == 401
    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "extra_api_keys", [])
    r = pull_app.get("/api/config/satellite")
    assert r.status_code == 503
    assert r.json()["detail"] == "This server has no API key set; set one to enable satellites."


def test_signature_and_digest_cover_the_new_fields(pull_app):
    from app.services import satellite as sat
    body = pull(pull_app, PRIMARY, "dev-a", "relay1", nonce="abc123").json()
    cfg = body["config"]
    assert {"fleet_relay", "ha_policy_version", "extras_sha256"} <= set(cfg)
    assert sat.signature_ok(PRIMARY, "abc123", cfg, body["config_signature"])
    assert cfg["extras_sha256"] == sat.extras_digest(body)
    # Every extra still sits beside the config, where older satellites read it.
    for k in sat.EXTRAS_KEYS:
        assert k in body


def test_policy_version_follows_the_server_migration(pull_app, monkeypatch):
    assert pull(pull_app, PRIMARY, "dev-a").json()["config"]["ha_policy_version"] == 0
    monkeypatch.setattr(settings, "ha_key_policy_version", 1)
    assert pull(pull_app, PRIMARY, "dev-a").json()["config"]["ha_policy_version"] == 1


def test_heartbeat_stores_what_the_pull_reported(pull_app):
    from app.services import devices
    pull(pull_app, PRIMARY, "dev-a", "relay1,deckproxy1", **{
        "X-PR-Client": "satellite/0.21.0", "X-Device-Trust": "SHA256:fake",
        "X-Device-Ip": "10.1.2.3"})
    d = next(x for x in devices.list_devices() if x["device_id"] == "dev-a")
    assert d["ip"] == "192.168.1.20" and d["ip_unverified"] is False
    assert d["relay"] is True and d["deck_proxy"] is True
    assert d["client"] == "satellite/0.21.0"
    assert d["trust_fp"] == "SHA256:fake"


def test_the_service_field_lists_agree():
    """The server withholds these from relaying satellites (fleet_relay) and
    a satellite scrubs its old copies (satellite); a field added to one list
    and not the other would be withheld but never scrubbed, or the reverse.
    Both match the withheld fields minus the deck's token and the Grocy and
    Mealie keys, which have their own rules."""
    from app.config import SATELLITE_WITHHELD_FIELDS
    from app.services import satellite
    assert satellite.SERVICE_FIELDS == fleet_relay.SERVICE_FIELDS
    assert set(fleet_relay.SERVICE_FIELDS) | {
        fleet_relay.DECK_TOKEN_FIELD, "grocy_api_key", "mealie_api_key",
    } == set(SATELLITE_WITHHELD_FIELDS)
