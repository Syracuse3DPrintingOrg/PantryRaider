"""Tests for the one-image mode switch logic (FoodAssistant-dzx9).

Covers the pure decision/settings-shape helpers that let a pi_hosted appliance
stand down its local stack and run as a satellite, then switch back. The
container stop/start itself lives in the host bridge (tested in
test_host_bridge.py); nothing here needs Docker or a network.

Run: python -m pytest tests/test_deployment_switch.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from app.config import SATELLITE_PULL_FIELDS, _SAVEABLE, SECRET_SETTING_KEYS  # noqa: E402
from app.services import deployment_switch as ds  # noqa: E402


# --- validate_server_url ----------------------------------------------------

def test_validate_url_accepts_http_and_strips_slash():
    ok, url = ds.validate_server_url("http://192.168.1.10:9284/")
    assert ok is True
    assert url == "http://192.168.1.10:9284"


def test_validate_url_accepts_https_and_trims_whitespace():
    ok, url = ds.validate_server_url("  https://pantry.example.com  ")
    assert ok is True
    assert url == "https://pantry.example.com"


def test_validate_url_rejects_empty():
    ok, err = ds.validate_server_url("")
    assert ok is False and "required" in err


def test_validate_url_rejects_missing_scheme():
    ok, err = ds.validate_server_url("192.168.1.10:9284")
    assert ok is False and "http" in err


def test_validate_url_rejects_other_schemes():
    ok, _ = ds.validate_server_url("ftp://192.168.1.10")
    assert ok is False


# --- can_switch_to_satellite ------------------------------------------------

def test_switch_to_satellite_allowed_only_from_pi_hosted():
    assert ds.can_switch_to_satellite("pi_hosted") == (True, "")


def test_switch_to_satellite_refused_when_already_satellite():
    ok, err = ds.can_switch_to_satellite("pi_remote")
    assert ok is False and "already" in err


def test_switch_to_satellite_refused_on_server_and_unset_modes():
    for mode in ("server", "", "something_else"):
        ok, err = ds.can_switch_to_satellite(mode)
        assert ok is False
        assert "Pi Hosted" in err


# --- can_switch_back --------------------------------------------------------

def test_switch_back_allowed_when_parked_satellite():
    assert ds.can_switch_back("pi_remote", True) == (True, "")


def test_switch_back_refused_when_not_satellite():
    ok, err = ds.can_switch_back("pi_hosted", True)
    assert ok is False and "not running as a satellite" in err


def test_switch_back_refused_on_a_born_satellite():
    # A device flashed as a plain Pi Remote has no parked stack: never touch it.
    ok, err = ds.can_switch_back("pi_remote", False)
    assert ok is False and "no parked local stack" in err


# --- hosted_snapshot --------------------------------------------------------

def test_snapshot_keeps_exactly_the_pulled_fields():
    values = {f: f"v-{f}" for f in SATELLITE_PULL_FIELDS}
    values["kiosk_pin"] = "1234"           # device-local: not snapshotted
    values["unknown_key"] = "x"            # ignored
    snap = ds.hosted_snapshot(values)
    assert set(snap) == set(SATELLITE_PULL_FIELDS)
    assert snap["grocy_base_url"] == "v-grocy_base_url"


def test_snapshot_tolerates_missing_fields():
    snap = ds.hosted_snapshot({"grocy_api_key": "k"})
    assert snap == {"grocy_api_key": "k"}


# --- satellite_switch_settings / hosted_restore_settings --------------------

def test_switch_settings_shape():
    snap = {"grocy_base_url": "http://localhost:9383", "grocy_api_key": "local"}
    data = ds.satellite_switch_settings("http://srv:9284", "key123", snap)
    assert data["deployment_mode"] == "pi_remote"
    assert data["remote_server_url"] == "http://srv:9284"
    assert data["upstream_api_key"] == "key123"
    assert data["hosted_stack_parked"] is True
    assert data["hosted_config_snapshot"] == snap


def test_restore_settings_brings_back_snapshot_and_clears_state():
    snap = {"grocy_base_url": "http://localhost:9383", "grocy_api_key": "local",
            "mealie_base_url": "http://localhost:9285"}
    data = ds.hosted_restore_settings(snap)
    assert data["deployment_mode"] == "pi_hosted"
    assert data["hosted_stack_parked"] is False
    assert data["hosted_config_snapshot"] == {}
    assert data["grocy_base_url"] == "http://localhost:9383"
    assert data["grocy_api_key"] == "local"
    assert data["mealie_base_url"] == "http://localhost:9285"
    # The upstream link is kept (harmless in pi_hosted; pre-fills a re-switch).
    assert "remote_server_url" not in data
    assert "upstream_api_key" not in data


def test_restore_settings_tolerates_empty_snapshot():
    data = ds.hosted_restore_settings({})
    assert data["deployment_mode"] == "pi_hosted"
    data = ds.hosted_restore_settings(None)
    assert data["deployment_mode"] == "pi_hosted"


def test_restore_does_not_mutate_the_snapshot():
    snap = {"grocy_api_key": "local"}
    ds.hosted_restore_settings(snap)
    assert snap == {"grocy_api_key": "local"}


# --- settings wiring ---------------------------------------------------------

def test_switch_state_fields_are_persistable():
    # Both switch-state fields must be in _SAVEABLE or settings.save drops them
    # and the switch would not survive a restart.
    assert "hosted_stack_parked" in _SAVEABLE
    assert "hosted_config_snapshot" in _SAVEABLE


def test_snapshot_is_treated_as_a_secret():
    # The snapshot carries pre-switch API keys, so backups must redact it.
    assert "hosted_config_snapshot" in SECRET_SETTING_KEYS


def test_switch_settings_fields_all_persistable():
    data = ds.satellite_switch_settings("http://srv:9284", "k", {})
    assert all(k in _SAVEABLE for k in data)


def test_restore_settings_fields_all_persistable():
    snap = {f: "" for f in SATELLITE_PULL_FIELDS}
    data = ds.hosted_restore_settings(snap)
    assert all(k in _SAVEABLE for k in data)
