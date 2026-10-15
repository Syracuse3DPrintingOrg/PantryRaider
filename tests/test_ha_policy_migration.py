"""The one-time Home Assistant key policy migration (services/ha_policy_migration).

Option A: existing keys that unlock, open or disarm keep working, carry
allow_elevated and elevated_grandfathered, and raise one inbox item; invalid
keys are marked and raise their own item; Bluetooth presses mapped to such a
key raise one item and are never rewritten; the policy version becomes 1 in
the same save, and a failure leaves it at 0. On a satellite the keys pulled
from the server are left alone. No network: in-memory SQLite and a temporary
data_dir.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import db_models  # noqa: E402,F401 - registers every model
from app.models.db_models import ActionItem, StreamDeckProfile  # noqa: E402
from app.services import ha_policy_migration as mig  # noqa: E402


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "ha_key_policy_version", 0, raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [], raising=False)
    monkeypatch.setattr(settings, "button_devices", [], raising=False)
    return tmp_path


def _items(db, kind=None):
    q = db.query(ActionItem)
    if kind:
        q = q.filter(ActionItem.kind == kind)
    return q.all()


def _saved(tmp_path) -> dict:
    return json.loads((tmp_path / "settings.json").read_text())


LOCK = {"id": "door", "label": "Front door", "type": "ha_action",
        "entity_id": "lock.front_door", "service": "lock.unlock"}
LIGHT = {"id": "lamp", "label": "Lamp", "type": "ha_action",
         "entity_id": "light.kitchen", "service": "light.toggle"}
BAD = {"id": "bad", "label": "Everything", "type": "ha_action",
       "entity_id": "all", "service": "light.turn_on"}


def test_option_a_grandfathers_elevated_keys_once(server, db, monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_key_overrides",
                        [dict(LOCK), dict(LIGHT)], raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_slots",
                        [{"entity_id": "cover.garage", "service": "cover.open_cover",
                          "label": "Garage"},
                         {"entity_id": "light.hall"}], raising=False)
    out = mig.migrate_once(db)
    assert out == {"skipped": False, "grandfathered": 2, "invalid": 0, "ble_paused": 0}

    door, lamp = settings.streamdeck_key_overrides
    assert door["allow_elevated"] is True and door["elevated_grandfathered"] is True
    assert door["entity_id"] == "lock.front_door" and door["service"] == "lock.unlock"
    assert "allow_elevated" not in lamp and "elevated_grandfathered" not in lamp
    garage, hall = settings.streamdeck_ha_slots
    assert garage["allow_elevated"] is True and garage["elevated_grandfathered"] is True
    assert garage["service"] == "cover.open_cover"
    assert "allow_elevated" not in hall
    assert settings.ha_key_policy_version == 1
    saved = _saved(server)
    assert saved["ha_key_policy_version"] == 1
    assert saved["streamdeck_key_overrides"][0]["allow_elevated"] is True

    items = _items(db)
    assert len(items) == 1
    item = items[0]
    assert item.kind == mig.KIND_GRANDFATHERED and item.dedupe_key == mig.DEDUPE_GRANDFATHERED
    assert item.title == mig.TITLE_GRANDFATHERED
    assert item.body.startswith(mig.BODY_GRANDFATHERED)
    assert "Front door: lock.front_door (lock.unlock)" in item.body
    assert "Garage: cover.garage (cover.open_cover)" in item.body

    # A second run is skipped and raises nothing new.
    assert mig.migrate_once(db) == {"skipped": True}
    assert len(_items(db)) == 1


def test_grandfathered_keys_keep_running_with_the_policy_on(server, db, monkeypatch):
    from app.services import ha_allowlist, ha_policy
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [dict(LOCK)], raising=False)
    mig.migrate_once(db)
    assert ha_allowlist.policy_enforced(settings) is True
    entry = settings.streamdeck_key_overrides[0]
    assert ha_policy.permitted(entry["entity_id"], entry["service"], entry, "screen",
                               enforce=True) == (True, "")
    assert ha_policy.permitted(entry["entity_id"], entry["service"], entry, "ble",
                               enforce=True)[0] is False


def test_invalid_keys_are_marked_and_listed(server, db, monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_key_overrides",
                        [dict(BAD), dict(LIGHT)], raising=False)
    out = mig.migrate_once(db)
    assert out["invalid"] == 1 and out["grandfathered"] == 0
    bad, lamp = settings.streamdeck_key_overrides
    assert bad["disabled_reason"] == "invalid"
    assert bad["entity_id"] == "all" and "allow_elevated" not in bad
    assert "disabled_reason" not in lamp
    items = _items(db)
    assert [i.kind for i in items] == [mig.KIND_INVALID]
    assert items[0].title == mig.TITLE_INVALID
    assert items[0].dedupe_key == mig.DEDUPE_INVALID
    assert "Everything: all (light.turn_on)" in items[0].body


def test_macro_children_are_marked_on_their_slot(server, db, monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_ha_slots",
                        [{"entity_id": "lock.back_door", "service": "lock.unlock"}],
                        raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides",
                        [{"id": "mac", "type": "macro", "actions": ["ha_1"]}],
                        raising=False)
    out = mig.migrate_once(db)
    assert out["grandfathered"] == 1
    assert settings.streamdeck_ha_slots[0]["allow_elevated"] is True
    assert "allow_elevated" not in settings.streamdeck_key_overrides[0]


def test_profiles_are_marked_in_the_same_pass(server, db):
    db.add(StreamDeckProfile(name="Night", deck_size=15,
                             key_overrides=json.dumps([dict(LOCK), dict(LIGHT)])))
    db.add(StreamDeckProfile(name="Broken", deck_size=6, key_overrides="not json"))
    db.commit()
    out = mig.migrate_once(db)
    assert out["grandfathered"] == 1
    row = db.query(StreamDeckProfile).filter_by(name="Night").one()
    door, lamp = json.loads(row.key_overrides)
    assert door["allow_elevated"] is True and door["elevated_grandfathered"] is True
    assert "allow_elevated" not in lamp
    assert db.query(StreamDeckProfile).filter_by(name="Broken").one().key_overrides == "not json"
    item = _items(db, mig.KIND_GRANDFATHERED)[0]
    assert "Front door (profile Night): lock.front_door (lock.unlock)" in item.body
    # A profile key is changed by loading the profile, not on the Start Page.
    assert mig.BODY_GRANDFATHERED not in item.body
    assert item.body.startswith(mig.BODY_PROFILE_KEYS)


def test_start_page_and_profile_keys_each_say_where_to_change_them(server, db,
                                                                    monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_key_overrides",
                        [dict(LOCK, label="Garage")], raising=False)
    db.add(StreamDeckProfile(name="Night", deck_size=15,
                             key_overrides=json.dumps([dict(LOCK)])))
    db.commit()
    out = mig.migrate_once(db)
    assert out["grandfathered"] == 2
    body = _items(db, mig.KIND_GRANDFATHERED)[0].body
    start, profiles = body.split(mig.BODY_PROFILE_KEYS)
    assert start.startswith(mig.BODY_GRANDFATHERED)
    assert "Garage: lock.front_door (lock.unlock)" in start
    assert "(profile Night)" not in start
    assert "Front door (profile Night): lock.front_door (lock.unlock)" in profiles


def test_bluetooth_presses_to_elevated_keys_are_listed_not_rewritten(server, db,
                                                                     monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [dict(LOCK)], raising=False)
    buttons = [{"id": "F0:11:22:33:44:55", "name": "Pantry fob",
                "mappings": {"single": {"action": "esp_action", "token": "door"},
                             "long": {"action": "esp_action", "token": "timer_eggs"}}}]
    monkeypatch.setattr(settings, "button_devices", json.loads(json.dumps(buttons)),
                        raising=False)
    out = mig.migrate_once(db)
    assert out["ble_paused"] == 1
    assert settings.button_devices == buttons
    assert "button_devices" not in _saved(server)
    item = _items(db, mig.KIND_BLE)[0]
    assert item.title == mig.TITLE_BLE and item.dedupe_key == mig.DEDUPE_BLE
    assert "Pantry fob, single press: door" in item.body
    assert "timer_eggs" not in item.body


def test_satellite_leaves_pulled_keys_and_profiles_alone(server, db, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [dict(LOCK), dict(BAD)],
                        raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_slots",
                        [{"entity_id": "cover.garage", "service": "cover.open_cover"}],
                        raising=False)
    monkeypatch.setattr(settings, "button_devices",
                        [{"id": "AA", "name": "Shelf",
                          "mappings": {"double": {"action": "esp_action",
                                                  "token": "door"}}}], raising=False)
    db.add(StreamDeckProfile(name="Night", deck_size=15,
                             key_overrides=json.dumps([dict(LOCK)])))
    db.commit()
    out = mig.migrate_once(db)
    assert out == {"skipped": False, "grandfathered": 0, "invalid": 0, "ble_paused": 1}
    assert settings.streamdeck_key_overrides == [LOCK, BAD]
    assert settings.streamdeck_ha_slots == [{"entity_id": "cover.garage",
                                             "service": "cover.open_cover"}]
    row = db.query(StreamDeckProfile).filter_by(name="Night").one()
    assert json.loads(row.key_overrides) == [LOCK]
    saved = _saved(server)
    assert "streamdeck_key_overrides" not in saved and "streamdeck_ha_slots" not in saved
    assert saved["ha_key_policy_version"] == 1
    assert [i.kind for i in _items(db)] == [mig.KIND_BLE]
    assert "Shelf, double press: door" in _items(db)[0].body


def test_a_failure_leaves_the_policy_off(server, db, monkeypatch):
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [dict(LOCK)], raising=False)
    from app.services import action_items

    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(action_items, "create", broken)
    with pytest.raises(RuntimeError):
        mig.migrate_once(db)
    assert settings.ha_key_policy_version == 0
    assert not (server / "settings.json").exists()
    assert "allow_elevated" not in settings.streamdeck_key_overrides[0]


def test_fresh_install_turns_the_policy_on_quietly(server, db):
    out = mig.migrate_once(db)
    assert out == {"skipped": False, "grandfathered": 0, "invalid": 0, "ble_paused": 0}
    assert settings.ha_key_policy_version == 1
    assert _items(db) == []


def test_a_rerun_after_a_failed_save_lists_the_keys_again(server, db, monkeypatch):
    db.add(StreamDeckProfile(name="Night", deck_size=15,
                             key_overrides=json.dumps([dict(LOCK)])))
    db.commit()
    real_save = type(settings).save
    calls = {"n": 0}

    def flaky_save(self, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return real_save(self, data)

    monkeypatch.setattr(type(settings), "save", flaky_save)
    with pytest.raises(OSError):
        mig.migrate_once(db)
    assert settings.ha_key_policy_version == 0
    out = mig.migrate_once(db)
    assert out["grandfathered"] == 1
    items = _items(db, mig.KIND_GRANDFATHERED)
    assert len(items) == 1
    assert "Front door (profile Night)" in items[0].body
    assert settings.ha_key_policy_version == 1
