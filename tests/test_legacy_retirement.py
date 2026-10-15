"""credentials.retirement_pass: older keys nothing needs any more (I-3)."""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import access, credentials

T0 = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "api_key", "", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", ["shared-key"], raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", ["HA"], raising=False)
    monkeypatch.setattr(settings, "legacy_key_auto_retire", True, raising=False)
    monkeypatch.setattr(settings, "legacy_mirror", False, raising=False)
    credentials.reset(None, persist=True)
    items = []
    monkeypatch.setattr(access, "raise_item",
                        lambda kind, title, body, dedupe, level="info": items.append((kind, dedupe)))
    yield items
    credentials.reset(persist=False)


def _use(cid, device_id="", client="", mode="", cub="", now=T0, ip="192.168.1.20"):
    credentials.record_use(cid, {"ip": ip, "client": client, "ua": "HomeAssistant/2026.9",
                                 "device_id": device_id or cub, "device_mode": mode,
                                 "cub": bool(cub), "family": "ha", "method": "GET",
                                 "path": "/ha/state"}, now=now)


def test_holders_that_all_moved_make_a_key_retiring_then_revoked(store):
    cid = credentials.lookup("shared-key", now=T0)["id"]
    _use(cid, device_id="ha-1")
    credentials.retirement_pass(now=T0 + DAY)
    assert credentials.get(cid)["status"] == "active"
    # The device signs in with a key of its own.
    rec, _ = credentials.mint("ha", "HA", "exchanged", now=T0 + DAY)
    credentials.record_use(rec["id"], {"ip": "192.168.1.20", "device_id": "ha-1",
                                       "family": "ha", "method": "GET", "path": "/ha/state"},
                           now=T0 + DAY)
    credentials.retirement_pass(now=T0 + 2 * DAY)
    assert credentials.get(cid)["status"] == "retiring"
    assert credentials.lookup("shared-key") is not None          # still works
    credentials.retirement_pass(now=T0 + 91 * DAY)
    assert credentials.get(cid)["status"] == "revoked"
    assert settings.extra_api_keys == []
    assert ("legacy_key_retired", f"legacy_key_retired:{cid}") in store


def test_unmoved_use_makes_a_retiring_key_active_again(store):
    cid = credentials.lookup("shared-key", now=T0)["id"]
    _use(cid, device_id="ha-1")
    credentials.mark_holder_moved(cid, "ha-1")
    credentials.retirement_pass(now=T0 + DAY)
    assert credentials.get(cid)["status"] == "retiring"
    _use(cid, device_id="ha-1", now=T0 + 2 * DAY)
    assert credentials.get(cid)["status"] == "active"


@pytest.mark.parametrize("kind", ["pi_remote", "cub"])
def test_satellite_and_cub_holders_get_an_item_instead(store, kind):
    cid = credentials.lookup("shared-key", now=T0)["id"]
    if kind == "pi_remote":
        _use(cid, device_id="sat-1", client="satellite/0.20.0", mode="pi_remote")
        credentials.mark_holder_moved(cid, "sat-1")
    else:
        _use(cid, cub="cub-aa", ip="192.168.1.40")
        credentials.mark_holder_moved(cid, "cub-aa")
    credentials.retirement_pass(now=T0 + 91 * DAY)
    assert credentials.get(cid)["status"] == "retiring"
    assert credentials.lookup("shared-key") is not None
    assert ("legacy_key_retire", f"legacy_key_retire:{cid}") in store


def test_the_switch_off_keeps_retiring_keys(store, monkeypatch):
    monkeypatch.setattr(settings, "legacy_key_auto_retire", False, raising=False)
    cid = credentials.lookup("shared-key", now=T0)["id"]
    _use(cid, device_id="ha-1")
    credentials.mark_holder_moved(cid, "ha-1")
    credentials.retirement_pass(now=T0 + 200 * DAY)
    assert credentials.get(cid)["status"] == "retiring"


def test_env_keys_never_change(store, monkeypatch):
    monkeypatch.setenv("API_KEY", "env-key")
    monkeypatch.setattr(settings, "api_key", "env-key", raising=False)
    cid = credentials.lookup("env-key", now=T0)["id"]
    _use(cid, device_id="ha-1")
    credentials.mark_holder_moved(cid, "ha-1")
    credentials.retirement_pass(now=T0 + 200 * DAY)
    assert credentials.get(cid)["status"] == "active"


def test_admin_keys_near_expiry_raise_one_item(store):
    rec, _ = credentials.mint("admin", "script", "manual", now=T0)
    credentials.retirement_pass(now=T0 + 10 * DAY)
    assert not any(k == "admin_key_expiry" for k, _ in store)
    credentials.retirement_pass(now=T0 + 80 * DAY)
    assert ("admin_key_expiry", f"admin_key_expiry:{rec['id']}") in store


def test_an_expired_key_loses_its_copy_for_older_versions(store, monkeypatch):
    """The satellite pull and the proxy still read extra_api_keys, and so does
    an older version after a rollback: an expired admin key's copy goes at the
    next hourly pass, while keys that have not expired keep theirs."""
    monkeypatch.setattr(settings, "legacy_mirror", True, raising=False)
    admin, admin_key = credentials.mint("admin", "Script", "manual", now=T0)
    sat, sat_key = credentials.mint("satellite", "kitchen-pi", "paired", now=T0)
    assert admin_key in settings.extra_api_keys and sat_key in settings.extra_api_keys
    credentials.retirement_pass(now=T0 + 30 * DAY)
    assert admin_key in settings.extra_api_keys
    credentials.retirement_pass(now=T0 + 91 * DAY)
    assert admin_key not in settings.extra_api_keys
    assert sat_key in settings.extra_api_keys
    assert "(mirror) Script" not in settings.extra_api_key_names
    assert credentials.get(admin["id"])["mirrored"] is False
    assert credentials.lookup(admin_key, now=T0 + 91 * DAY) is None
    assert credentials.lookup(sat_key, now=T0 + 91 * DAY)["id"] == sat["id"]
    assert "shared-key" in settings.extra_api_keys        # older keys untouched
