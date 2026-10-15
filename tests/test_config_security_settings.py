"""The 0.21 access and relay settings: defaults, persistence and the helpers.

Every test builds its own Settings() with a cleaned environment, so nothing
here reads or changes the process-wide settings object or /app/data.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app import config  # noqa: E402
from app.config import (  # noqa: E402
    Settings, _SAVEABLE, SATELLITE_PULL_FIELDS, SECRET_SETTING_KEYS,
    SATELLITE_READONLY_FIELDS, SATELLITE_WITHHELD_FIELDS,
    UNSIGNED_COSMETIC_FIELDS,
)

NEW_FIELDS = {
    "kiosk_screen_is_admin": True,
    "lan_cidrs": "",
    "home_host_names": "",
    "legacy_mirror": True,
    "legacy_key_auto_retire": True,
    "satellite_legacy_credentials": "auto",
    "require_key_families": {},
    "fleet_relay": {},
    "upstream_credential_id": "",
    "upstream_ha_policy_version": 0,
    "ha_key_policy_version": 0,
    "ha_token_kind": "",
    "ha_token_admin": None,
    "ha_token_expires": "",
}


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    """Build a Settings() free of the host's environment and any .env file."""
    monkeypatch.chdir(tmp_path)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)
    data = tmp_path / "data"
    data.mkdir()

    def build(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        s = Settings()
        object.__setattr__(s, "data_dir", str(data))
        return s
    build.data = data
    return build


# -- Defaults and persistence -------------------------------------------------

def test_new_defaults(fresh):
    s = fresh()
    for name, default in NEW_FIELDS.items():
        assert getattr(s, name) == default, name
    assert s.update_channel == "stable"
    assert s.proxy_path_policy == "enforce"
    assert s.upstream_ha_policy_version == 0


def test_new_fields_are_saveable_device_local_and_not_secret():
    for name in NEW_FIELDS:
        assert name in _SAVEABLE, name
        assert name not in SATELLITE_PULL_FIELDS, name
        assert name not in SECRET_SETTING_KEYS, name
    assert _SAVEABLE.count("update_channel") == 1
    assert len(_SAVEABLE) == len(set(_SAVEABLE))


def test_backend_keys_are_never_pulled():
    assert "grocy_api_key" not in SATELLITE_PULL_FIELDS
    assert "mealie_api_key" not in SATELLITE_PULL_FIELDS
    assert "grocy_api_key" in SATELLITE_READONLY_FIELDS
    assert "mealie_api_key" in SATELLITE_READONLY_FIELDS


def test_withheld_and_cosmetic_lists():
    # Both are derived from the Settings fields, so they come out in field
    # order; membership is what matters.
    assert set(SATELLITE_WITHHELD_FIELDS) == {
        "gemini_api_key", "openai_api_key", "anthropic_api_key",
        "ollama_base_url", "themealdb_api_key", "spoonacular_api_key",
        "streamdeck_ha_token", "grocy_api_key", "mealie_api_key"}
    assert set(UNSIGNED_COSMETIC_FIELDS) == {
        "ui_theme", "clock_format", "streamdeck_weather_location",
        "streamdeck_weather_units", "perishable_days", "expiring_soon_days",
        "suggest_per_tier"}
    # The server decides per requester, so the relayed credentials stay
    # pullable for satellites from before 0.21.
    for name in SATELLITE_WITHHELD_FIELDS:
        if name not in ("grocy_api_key", "mealie_api_key"):
            assert name in SATELLITE_PULL_FIELDS, name
    for name in UNSIGNED_COSMETIC_FIELDS:
        assert name in SATELLITE_PULL_FIELDS, name
        assert name in Settings.model_fields, name


def _config_in_subprocess(data_dir: Path, cwd: Path, code: str,
                          **env_extra: str) -> str:
    """Import app.config in a clean interpreter and print code's result.

    A new process is the only way to run the real start-up overlay (and the
    SETTINGS_EXISTED_AT_START check) without touching the shared object."""
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in {n.upper() for n in Settings.model_fields}}
    env["DATA_DIR"] = str(data_dir)
    env["PYTHONPATH"] = str(SERVICE)
    env.update(env_extra)
    out = subprocess.run(
        [sys.executable, "-c", "from app import config\n" + code],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


def test_saved_values_beat_the_new_defaults(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text(json.dumps({
        "secret_key": "s", "device_id": "d",
        "update_channel": "main", "proxy_path_policy": "report",
        "kiosk_screen_is_admin": False, "legacy_mirror": False,
        "require_key_families": {"ha": False}}))
    code = ("s = config.settings\n"
            "print(repr((s.update_channel, s.proxy_path_policy,"
            " s.kiosk_screen_is_admin, s.legacy_mirror,"
            " s.require_key_families, s.update_channel_is_explicit())))")
    assert _config_in_subprocess(data, tmp_path, code) == repr(
        ("main", "report", False, False, {"ha": False}, True))
    # An install that never chose gets the new defaults, and the environment
    # still beats a saved value.
    (data / "settings.json").write_text(json.dumps(
        {"secret_key": "s", "device_id": "d"}))
    assert _config_in_subprocess(data, tmp_path, code) == repr(
        ("stable", "enforce", True, True, {}, False))
    (data / "settings.json").write_text(json.dumps(
        {"secret_key": "s", "device_id": "d", "update_channel": "stable"}))
    assert _config_in_subprocess(
        data, tmp_path, code, UPDATE_CHANNEL="main",
        PROXY_PATH_POLICY="report") == repr(
        ("main", "report", True, True, {}, True))
    # The start-up overlay itself never writes a migrated value back.
    assert json.loads((data / "settings.json").read_text()) == {
        "secret_key": "s", "device_id": "d", "update_channel": "stable"}


def test_environment_overrides_the_flipped_defaults(fresh):
    s = fresh(UPDATE_CHANNEL="main", PROXY_PATH_POLICY="report")
    assert s.update_channel == "main"
    assert s.proxy_path_policy == "report"


def test_list_shaped_env_values_stay_plain_strings(fresh):
    s = fresh(LAN_CIDRS="10.8.0.0/24, 192.168.50.0/24",
              HOME_HOST_NAMES="pantry.example.com")
    assert s.lan_cidrs == "10.8.0.0/24, 192.168.50.0/24"
    assert s.home_host_names == "pantry.example.com"
    s = fresh(LAN_CIDRS="10.8.0.0/24 192.168.50.0/24 not-a-cidr")
    assert isinstance(s.lan_cidrs, str)


def test_save_round_trips_the_new_fields(fresh):
    s = fresh()
    s.save({"require_key_families": {"ha": True, "events": False},
            "ha_token_admin": False, "upstream_ha_policy_version": 1,
            "fleet_relay": {"v": 1, "caps": ["ai"]}})
    on_disk = json.loads((fresh.data / "settings.json").read_text())
    assert on_disk["require_key_families"] == {"ha": True, "events": False}
    assert on_disk["ha_token_admin"] is False
    assert on_disk["upstream_ha_policy_version"] == 1
    assert s.fleet_relay == {"v": 1, "caps": ["ai"]}


def test_save_can_forget_the_token_admin_answer(fresh):
    # None means "not checked yet" for ha_token_admin, so a token change must
    # be able to write it back; every other key still treats None as "keep".
    s = fresh()
    s.save({"ha_token_admin": True, "ha_token_kind": "manual"})
    s.save({"ha_token_admin": None, "ha_token_kind": None})
    on_disk = json.loads((fresh.data / "settings.json").read_text())
    assert on_disk["ha_token_admin"] is None and s.ha_token_admin is None
    assert on_disk["ha_token_kind"] == "manual" and s.ha_token_kind == "manual"


# -- update_channel_is_explicit ----------------------------------------------

def _write(path: Path, text: str, bump: int = 0) -> None:
    path.write_text(text)
    st = path.stat()
    # Move the mtime on so the (path, mtime) cache sees every rewrite even on
    # a filesystem with a coarse clock.
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + bump * 1_000_000_000))


def test_channel_not_explicit_without_a_file(fresh):
    assert fresh().update_channel_is_explicit() is False


def test_channel_explicit_when_saved(fresh):
    s = fresh()
    sf = fresh.data / "settings.json"
    _write(sf, json.dumps({"secret_key": "x"}))
    assert s.update_channel_is_explicit() is False
    _write(sf, json.dumps({"secret_key": "x", "update_channel": "main"}), bump=5)
    assert s.update_channel_is_explicit() is True
    _write(sf, json.dumps({"update_channel": "stable"}), bump=10)
    assert s.update_channel_is_explicit() is True
    _write(sf, json.dumps({"device_id": "abc"}), bump=15)
    assert s.update_channel_is_explicit() is False


def test_channel_cache_sees_a_save_within_one_clock_tick(fresh):
    # save() replaces the file; on a coarse file clock the new file can carry
    # the very same mtime as the one before it.
    s = fresh()
    sf = fresh.data / "settings.json"
    sf.write_text(json.dumps({"secret_key": "x"}))
    pinned = sf.stat().st_mtime_ns
    assert s.update_channel_is_explicit() is False
    s.save({"update_channel": "main"})
    os.utime(sf, ns=(pinned, pinned))
    assert sf.stat().st_mtime_ns == pinned
    assert s.update_channel_is_explicit() is True


def test_channel_explicit_from_the_environment(fresh):
    s = fresh(UPDATE_CHANNEL="stable")
    assert "update_channel" in s.model_fields_set
    assert s.update_channel_is_explicit() is True


@pytest.mark.parametrize("text", ["not json", "[\"update_channel\"]",
                                  "\"update_channel\"", "", "\xff\xfe"])
def test_channel_bad_files_are_not_explicit(fresh, text):
    s = fresh()
    sf = fresh.data / "settings.json"
    if text == "\xff\xfe":
        sf.write_bytes(b"\xff\xfe\x00{")
    else:
        sf.write_text(text)
    assert s.update_channel_is_explicit() is False


def test_channel_unreadable_file_is_not_explicit(fresh):
    s = fresh()
    (fresh.data / "settings.json").mkdir()   # stat works, read fails
    assert s.update_channel_is_explicit() is False


def test_channel_missing_data_dir_is_not_explicit(fresh, tmp_path):
    s = fresh()
    object.__setattr__(s, "data_dir", str(tmp_path / "nowhere"))
    assert s.update_channel_is_explicit() is False


# -- SETTINGS_EXISTED_AT_START ------------------------------------------------

def test_settings_existed_at_start_is_a_bool():
    assert isinstance(config.SETTINGS_EXISTED_AT_START, bool)


def _existed_at_start(data_dir: Path, cwd: Path) -> str:
    return _config_in_subprocess(
        data_dir, cwd, "print(config.SETTINGS_EXISTED_AT_START)")


def test_settings_existed_at_start_tells_new_from_upgraded(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    # A first start: no file yet, then the secret_key auto-save creates it.
    assert _existed_at_start(data, tmp_path) == "False"
    assert (data / "settings.json").exists()
    # Every later start sees the file the first one wrote.
    assert _existed_at_start(data, tmp_path) == "True"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root reads through permissions")
def test_settings_existed_at_start_errs_towards_upgraded(tmp_path):
    locked = tmp_path / "locked"
    data = locked / "data"
    data.mkdir(parents=True)
    locked.chmod(0)
    try:
        assert _existed_at_start(data, tmp_path) == "True"
    finally:
        locked.chmod(0o700)


def _env_only_origin(data_dir: Path, cwd: Path) -> str:
    code = ("from app.services import auth_state\n"
            "auth_state.ensure_initialized()\n"
            "print(config.SETTINGS_EXISTED_AT_START, auth_state.origin())")
    return _config_in_subprocess(data_dir, cwd, code,
                                 SECRET_KEY="env-secret", DEVICE_ID="envdevice")


def test_an_env_only_upgrade_is_not_taken_for_a_fresh_install(tmp_path):
    # SECRET_KEY and DEVICE_ID from the environment and the wizard never
    # saved: no settings.json ever exists, so the database an earlier version
    # left behind is what marks the upgrade.
    data = tmp_path / "data"
    data.mkdir()
    (data / "foodassistant.db").write_bytes(b"")
    assert _env_only_origin(data, tmp_path) == "False upgraded"
    assert not (data / "settings.json").exists()
    # A genuinely new env-only install still starts fresh.
    new = tmp_path / "new"
    new.mkdir()
    assert _env_only_origin(new, tmp_path) == "False fresh"


# -- The relay-aware helpers --------------------------------------------------

RELAY = {"v": 1, "caps": ["ai", "ha"], "ai_available": True,
         "ha_available": False}


def _server(fresh):
    s = fresh()
    object.__setattr__(s, "deployment_mode", "server")
    return s


def _satellite(fresh, relay=None, **fields):
    s = fresh()
    object.__setattr__(s, "deployment_mode", "pi_remote")
    object.__setattr__(s, "remote_server_url", "http://192.168.1.50:9284")
    if relay is not None:
        object.__setattr__(s, "fleet_relay", relay)
    for k, v in fields.items():
        object.__setattr__(s, k, v)
    return s


def test_fleet_relay_active(fresh):
    assert _server(fresh).fleet_relay_active("ai") is False
    # A server never relays to itself, even with a stray block saved.
    s = _server(fresh)
    object.__setattr__(s, "fleet_relay", RELAY)
    assert s.fleet_relay_active("ai") is False
    assert _satellite(fresh).fleet_relay_active("ai") is False
    sat = _satellite(fresh, RELAY)
    assert sat.fleet_relay_active("ai") is True
    assert sat.fleet_relay_active("ha") is True
    assert sat.fleet_relay_active("camera") is False
    assert _satellite(fresh, {"v": 1}).fleet_relay_active("ai") is False
    assert _satellite(fresh, {"caps": None}).fleet_relay_active("ai") is False
    assert _satellite(fresh, "junk").fleet_relay_active("ai") is False


def test_upstream_linked(fresh):
    assert _server(fresh).upstream_linked() is False
    assert _satellite(fresh).upstream_linked() is False
    assert _satellite(fresh, upstream_api_key="k").upstream_linked() is True
    assert _satellite(fresh, upstream_credential_id="cred-1").upstream_linked() is True
    s = _satellite(fresh, upstream_api_key="k")
    object.__setattr__(s, "remote_server_url", "")
    assert s.upstream_linked() is False


def test_ha_connected(fresh):
    ha = {"streamdeck_ha_base_url": "http://ha.local:8123",
          "streamdeck_ha_token": "t"}
    s = _server(fresh)
    assert s.ha_connected() is False
    for k, v in ha.items():
        object.__setattr__(s, k, v)
    assert s.ha_connected() is True
    assert _satellite(fresh, **ha).ha_connected() is True
    assert _satellite(fresh).ha_connected() is False
    # With the relay the server's answer wins, local token or not.
    assert _satellite(fresh, RELAY, **ha).ha_connected() is False
    assert _satellite(fresh, dict(RELAY, ha_available=True)).ha_connected() is True


def test_ai_configured(fresh):
    s = _server(fresh)
    assert s.ai_configured() is False
    object.__setattr__(s, "gemini_api_key", "g")
    assert s.ai_configured() is True
    assert _satellite(fresh).ai_configured() is False
    assert _satellite(fresh, gemini_api_key="g").ai_configured() is True
    assert _satellite(fresh, RELAY).ai_configured() is True
    assert _satellite(fresh, dict(RELAY, ai_available=False),
                      gemini_api_key="g").ai_configured() is False


def test_llm_expiry_effective(fresh):
    s = _server(fresh)
    assert s.llm_expiry_effective() is False
    object.__setattr__(s, "gemini_api_key", "g")
    assert s.llm_expiry_effective() is True
    object.__setattr__(s, "llm_expiry_enabled", False)
    assert s.llm_expiry_effective() is False
    assert _satellite(fresh).llm_expiry_effective() is False
    assert _satellite(fresh, RELAY).llm_expiry_effective() is True
    off = dict(RELAY, ai_available=False)
    assert _satellite(fresh, off).llm_expiry_effective() is False
    assert _satellite(fresh, dict(off, enrich_available=True)).llm_expiry_effective() is True
    # An explicit choice still wins over the relay's answer.
    assert _satellite(fresh, RELAY, llm_expiry_enabled=False).llm_expiry_effective() is False
    assert _satellite(fresh, off, llm_expiry_enabled=True).llm_expiry_effective() is True


def test_is_configured_for_satellites(fresh):
    sat = _satellite(fresh, upstream_credential_id="cred-1",
                     auth_required=False)
    assert sat.upstream_api_key == ""
    assert sat.is_configured() is True
    assert _satellite(fresh, upstream_api_key="k",
                      auth_required=False).is_configured() is True
    assert _satellite(fresh, auth_required=False).is_configured() is False
    # A password is still needed when one is required.
    assert _satellite(fresh, upstream_credential_id="cred-1").is_configured() is False
