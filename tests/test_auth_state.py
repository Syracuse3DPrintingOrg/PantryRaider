"""data_dir/auth-state.json (services/auth_state.py, I-4).

Every clock is injected; every file lives under tmp_path.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from app import config
from app.config import settings
from app.services import auth_state

T0 = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture
def disk(tmp_path, monkeypatch):
    """auth_state backed by a real file in tmp_path, starting with no file."""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    auth_state.reset(None, persist=True)
    yield tmp_path
    auth_state.reset(auth_state.test_state(), persist=False)


def _file(tmp_path):
    return json.loads((tmp_path / auth_state.FILENAME).read_text())


def test_first_start_of_an_upgraded_install(disk, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_EXISTED_AT_START", True)
    auth_state.ensure_initialized(now=T0)
    doc = _file(disk)
    assert doc["origin"] == "upgraded"
    assert doc["loopback_trust"]["state"] == "legacy"
    assert auth_state.parse_ts(doc["open_mode"]["grace_until"]) == T0 + 14 * DAY
    assert doc["kiosk_gen"] == 1
    assert auth_state.grace_active(now=T0 + 13 * DAY)
    assert not auth_state.grace_active(now=T0 + 15 * DAY)
    assert auth_state.legacy_trust_active(now=T0)
    mode = stat.S_IMODE(os.stat(disk / auth_state.FILENAME).st_mode)
    assert mode == 0o600


def test_first_start_of_a_fresh_install(disk, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_EXISTED_AT_START", False)
    monkeypatch.setattr(auth_state, "_PRIOR_EVIDENCE", False)
    auth_state.ensure_initialized(now=T0)
    doc = _file(disk)
    assert doc["origin"] == "fresh"
    assert doc["loopback_trust"]["state"] == "retired"
    assert doc["loopback_trust"]["by"] == "fresh"
    assert doc["open_mode"]["grace_until"] is None
    assert not auth_state.grace_active(now=T0)
    assert not auth_state.legacy_trust_active(now=T0)


def test_an_existing_file_is_never_overwritten(disk, monkeypatch):
    seed = {"v": 1, "origin": "fresh", "first_start": "2026-09-01T00:00:00Z",
            "loopback_trust": {"state": "retired", "since": "2026-09-01T00:00:00Z",
                               "by": "firstboot", "retrust_until": None},
            "kiosk_gen": 1}
    raw = json.dumps(seed)
    (disk / auth_state.FILENAME).write_text(raw)
    monkeypatch.setattr(config, "SETTINGS_EXISTED_AT_START", True)
    auth_state.ensure_initialized(now=T0)
    assert (disk / auth_state.FILENAME).read_text() == raw
    assert auth_state.origin() == "fresh"
    assert not auth_state.legacy_trust_active(now=T0)
    assert auth_state.grace_until() is None
    # Missing keys are filled in memory and written with the next change.
    auth_state.bump_kiosk_gen(now=T0)
    doc = _file(disk)
    assert doc["kiosk_gen"] == 2 and doc["loopback_trust"]["by"] == "firstboot"
    assert "open_mode" in doc and doc["password_seen"] is False


def test_unwritable_data_dir_is_lenient(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a directory")
    monkeypatch.setattr(settings, "data_dir", str(blocked), raising=False)
    auth_state.reset(None, persist=True)
    auth_state.ensure_initialized(now=T0)
    assert auth_state.origin() == "upgraded"
    assert auth_state.legacy_trust_active()
    assert auth_state.grace_active()
    auth_state.record_legacy_use("browser", "Mozilla/5.0", "/ui/")
    assert auth_state.load()["legacy_use"]["browser"]["count"] == 1


def test_transitions_write_at_once_and_counters_wait(disk):
    auth_state.reset(auth_state.test_state(now=T0), persist=True)
    auth_state.record_legacy_use("browser", "Mozilla/5.0", "/ui/", now=T0)
    assert _file(disk)["legacy_use"]["browser"]["count"] == 1     # first of its class
    auth_state.record_legacy_use("browser", "Mozilla/5.0", "/ui/", now=T0 + 10)
    auth_state.record_legacy_use("browser", "Mozilla/5.0", "/ui/", now=T0 + 20)
    assert _file(disk)["legacy_use"]["browser"]["count"] == 1     # counters wait
    assert auth_state.load()["legacy_use"]["browser"]["count"] == 3
    auth_state.flush(now=T0 + 30)
    assert _file(disk)["legacy_use"]["browser"]["count"] == 1     # under 10 minutes
    auth_state.flush(now=T0 + 700)
    assert _file(disk)["legacy_use"]["browser"]["count"] == 3


def test_counters_merge_with_another_workers_write(disk):
    auth_state.reset(auth_state.test_state(now=T0), persist=True)
    auth_state.record_keyless_ha("192.168.1.20", now=T0)
    auth_state.record_keyless_ha("192.168.1.20", now=T0 + 5)
    # Another worker writes its own count and a newer time.
    doc = _file(disk)
    doc["keyless_ha_callers"]["count"] = 10
    doc["keyless_ha_callers"]["last"] = auth_state.iso(T0 + 50)
    (disk / auth_state.FILENAME).write_text(json.dumps(doc))
    os.utime(disk / auth_state.FILENAME, ns=(1, 1))
    auth_state.flush(force=True, now=T0 + 60)
    merged = _file(disk)["keyless_ha_callers"]
    assert merged["count"] == 11
    assert auth_state.parse_ts(merged["last"]) == T0 + 50


def test_retire_retrust_and_kiosk_gen(disk):
    auth_state.reset(auth_state.test_state(now=T0), persist=True)
    assert auth_state.legacy_trust_active(now=T0)
    auth_state.retire("admin", now=T0)
    lt = auth_state.loopback_trust()
    assert lt["state"] == "retired" and lt["by"] == "admin"
    assert not auth_state.legacy_trust_active(now=T0 + 1)
    auth_state.retrust(7, now=T0 + 10)
    assert auth_state.loopback_trust()["state"] == "retired"
    assert auth_state.legacy_trust_active(now=T0 + 6 * DAY)
    assert not auth_state.legacy_trust_active(now=T0 + 8 * DAY)
    gen = auth_state.kiosk_gen()
    assert auth_state.bump_kiosk_gen() == gen + 1
    assert _file(disk)["kiosk_gen"] == gen + 1


def test_note_restore_restarts_the_grace(disk):
    state = auth_state.test_state(now=T0, origin="fresh",
                                  open_mode={"grace_until": None,
                                             "seen": {"x.example": {"count": 1, "last": auth_state.iso(T0)}}})
    state["loopback_trust"] = {"state": "retired", "since": auth_state.iso(T0), "by": "firstboot"}
    auth_state.reset(state, persist=True)
    auth_state.note_restore(now=T0 + DAY)
    doc = _file(disk)
    assert doc["origin"] == "upgraded"
    assert auth_state.parse_ts(doc["open_mode"]["grace_until"]) == T0 + 15 * DAY
    assert doc["open_mode"]["seen"] == {}
    assert doc["loopback_trust"]["state"] == "retired"      # trust untouched
    assert doc["restored_at"]


# --- Open-mode sightings ------------------------------------------------------

def test_sightings_are_capped_with_an_overflow_count(disk):
    auth_state.reset(auth_state.test_state(now=T0), persist=True)
    for i in range(30):
        auth_state.record_open_sighting(f"h{i}.example", "proxied", "203.0.113.9",
                                        now=T0 + i * 61)
    seen, other = auth_state.open_sightings()
    assert len(seen) == 20
    assert other == 10
    assert seen[0]["host"] == "h29.example"           # newest first
    auth_state.flush(force=True, now=T0 + 3000)
    doc = _file(disk)["open_mode"]
    assert len(doc["seen"]) == 20 and doc["seen_other"]["count"] == 10


def test_a_flood_of_new_names_writes_at_most_once_a_minute(disk, monkeypatch):
    auth_state.reset(auth_state.test_state(now=T0), persist=True)
    real = os.replace
    calls = []

    def counting(src, dst, *a, **k):
        if str(dst).endswith(auth_state.FILENAME):
            calls.append(dst)
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", counting)
    firsts = 0
    for i in range(10_000):
        if auth_state.record_open_sighting(f"n{i}.example", "proxied", f"198.51.100.{i % 250}",
                                           now=T0 + i * 0.005):
            firsts += 1
    assert len(calls) <= 2
    assert firsts == 1
    seen, other = auth_state.open_sightings()
    assert len(seen) == 20
    assert other > 0
    om = auth_state.load()["open_mode"]
    assert len(om["seen"]) <= 20


def test_known_hosts_only_bump_counters(disk):
    auth_state.reset(auth_state.test_state(now=T0), persist=True)
    assert auth_state.record_open_sighting("pantry.example.com", "proxied", "203.0.113.9", now=T0)
    assert not auth_state.record_open_sighting("pantry.example.com", "proxied", "203.0.113.9",
                                               now=T0 + 120)
    seen, _ = auth_state.open_sightings()
    assert seen[0]["count"] == 2


def test_gateway_episodes(disk):
    auth_state.reset(auth_state.test_state(now=T0), persist=True)
    assert auth_state.record_gateway_peer(now=T0)
    ep = auth_state.gateway_episode()
    assert not auth_state.record_gateway_peer(now=T0 + DAY)
    assert auth_state.gateway_peer_recent(now=T0 + 20 * DAY)
    assert not auth_state.gateway_peer_recent(now=T0 + DAY + 31 * DAY)
    assert auth_state.record_gateway_peer(now=T0 + DAY + 31 * DAY)
    assert auth_state.gateway_episode() != ep


# --- Keyless devices and the first password -----------------------------------

def test_missing_password_seen_is_filled_from_the_current_password(disk, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "hashed", raising=False)
    state = auth_state.test_state(now=T0)
    state.pop("password_seen", None)
    auth_state.reset(state, persist=False)
    assert auth_state.load()["password_seen"] is True
    assert not auth_state.note_password_state(True, now=T0)


def test_note_password_state_transitions(disk):
    state = auth_state.test_state(now=T0)
    state["password_seen"] = False
    auth_state.reset(state, persist=True)
    assert not auth_state.note_password_state(False, now=T0)
    assert auth_state.note_password_state(True, now=T0 + 5)
    assert _file(disk)["password_seen"] is True
    assert auth_state.parse_ts(auth_state.password_set_at()) == T0 + 5
    assert not auth_state.note_password_state(True, now=T0 + 6)
    assert not auth_state.note_password_state(False, now=T0 + 7)
    assert _file(disk)["password_seen"] is False
    assert auth_state.note_password_state(True, now=T0 + 8)


def test_keyless_device_labels(disk):
    auth_state.reset(auth_state.test_state(now=T0), persist=False)
    assert auth_state.keyless_device_labels(30, now=T0) == []
    auth_state.record_keyless_family("reader", "python-httpx", "127.0.0.1", now=T0)
    auth_state.record_keyless_esp("192.168.1.30", now=T0)
    auth_state.record_keyless_family("display", "esphome", "192.168.1.31", now=T0)
    auth_state.record_keyless_ha("192.168.1.20", now=T0 - 40 * DAY)
    assert auth_state.keyless_device_labels(30, now=T0) == [
        "Bandit Cubs and small displays", "ESP buttons and screens", "Sensor readers"]
    auth_state.record_keyless_family("events", "HomeAssistant/2026.9", "192.168.1.20", now=T0)
    assert auth_state.keyless_device_labels(30, now=T0)[0] == "Home Assistant"
    assert auth_state.keyless_device_labels(30, since=auth_state.iso(T0 + 1), now=T0 + 2) == []
    assert auth_state.keyless_device_labels(7, now=T0 + 8 * DAY) == []


def test_same_host_proxy_episodes(disk):
    auth_state.reset(auth_state.test_state(now=T0), persist=True)
    assert auth_state.record_same_host_proxy("http10", now=T0)
    assert not auth_state.record_same_host_proxy("referer", now=T0 + DAY)
    assert auth_state.same_host_proxy_recent(now=T0 + 10 * DAY)
    assert not auth_state.same_host_proxy_recent(now=T0 + DAY + 31 * DAY)
    assert auth_state.record_same_host_proxy("origin", now=T0 + DAY + 31 * DAY)


def test_unknown_keys_are_preserved(disk):
    state = auth_state.test_state(now=T0)
    state["future_field"] = {"keep": True}
    auth_state.reset(state, persist=True)
    auth_state.bump_kiosk_gen(now=T0)
    assert _file(disk)["future_field"] == {"keep": True}


def test_relaunch_bookkeeping(disk):
    auth_state.reset(auth_state.test_state(now=T0), persist=False)
    assert auth_state.relaunches_today(now=T0) == []
    auth_state.record_relaunch(now=T0)
    auth_state.record_relaunch(now=T0 + 60)
    assert len(auth_state.relaunches_today(now=T0 + 120)) == 2
    assert auth_state.relaunches_today(now=T0 + 2 * DAY) == []
