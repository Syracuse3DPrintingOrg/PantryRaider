"""Going back to v0.20.0 keeps working.

While the key mirror is on, every key this release mints is also written to
settings.json's extra_api_keys, where v0.20.0 looks for keys, so an owner who
rolls back keeps every paired device. The v0.20.0 rule that reads them is
copied below from the tag (Settings.valid_api_keys in service/app/config.py
and _api_key_ok in service/app/main.py, about 15 lines). The copy is checked
against the tag at build time by
scripts/compat/capture-client-fixtures.py --check-vendored, which exits 1 when
they differ; this test never reads git.

A satellite on this release whose main server went back to v0.20.0 pulls a
config without the relay block, and puts its own credentials back from it.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

import httpx
import pytest
from starlette.datastructures import Headers
from starlette.requests import Request

# The v0.20.0 rule reads a module-level settings object; old_rule_accepts()
# points it at what settings.json holds.
settings = None


class _V020Settings:
    """The two fields v0.20.0's rule reads, from a settings.json."""

    def __init__(self, data: dict):
        self.api_key = data.get("api_key", "")
        self.extra_api_keys = data.get("extra_api_keys", [])

    # From v0.20.0 (tag v0.20.0), service/app/config.py, Settings.valid_api_keys.
    # BEGIN VENDORED v0.20.0 valid_api_keys
    def valid_api_keys(self) -> list[str]:
        """All currently accepted satellite/headless-client API keys.

        The primary api_key is listed first for backward compatibility.
        Extra keys let each satellite use its own key so one can be rotated
        or removed without touching the others.
        """
        keys: list[str] = []
        if self.api_key:
            keys.append(self.api_key)
        for k in (self.extra_api_keys if isinstance(self.extra_api_keys, list) else []):
            if k and k not in keys:
                keys.append(k)
        return keys
    # END VENDORED v0.20.0 valid_api_keys


# From v0.20.0 (tag v0.20.0), service/app/main.py, _api_key_ok.
# BEGIN VENDORED v0.20.0 _api_key_ok
def _api_key_ok(request: Request) -> bool:
    """True when the request carries one of this install's API keys.

    Single definition on purpose: the no-password branch and the main auth path
    both decide on it, and the comparison must stay constant-time in both.
    """
    sent = request.headers.get("X-API-Key", "")
    valid = settings.valid_api_keys()
    return bool(sent) and bool(valid) and any(
        secrets.compare_digest(sent, k) for k in valid
    )
# END VENDORED v0.20.0 _api_key_ok


class _OldRequest:
    def __init__(self, headers: dict):
        self.headers = Headers(headers)


def old_rule_accepts(data_dir, secret: str) -> bool:
    """Whether v0.20.0, started on this data_dir's settings.json, accepts the key."""
    global settings
    path = Path(data_dir) / "settings.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    settings = _V020Settings(data)
    return _api_key_ok(_OldRequest({"X-API-Key": secret}))


@pytest.fixture
def store(tmp_path, monkeypatch, compat):
    """A configured install with no older keys, the mirror on, and its
    credentials on disk."""
    from app.services import credentials
    compat.configure_server(monkeypatch, tmp_path, password="none", api_key="",
                            extra_api_keys=[], extra_api_key_names=[], legacy_mirror=True)
    credentials.reset(None, persist=True)
    yield tmp_path
    credentials.reset(persist=False)


def _extra_keys(data_dir) -> list:
    return json.loads((Path(data_dir) / "settings.json").read_text()).get("extra_api_keys", [])


def test_the_vendored_rule_is_the_old_one():
    """A sanity check on the copy itself: the primary key first, extras after,
    duplicates and blanks dropped, constant-time comparison."""
    s = _V020Settings({"api_key": "a", "extra_api_keys": ["b", "", "a", "c"]})
    assert s.valid_api_keys() == ["a", "b", "c"]


def test_minted_keys_work_after_a_rollback_until_revoked(store):
    from app.services import credentials
    minted = {}
    for scope in ("ha", "display", "satellite"):
        rec, secret = credentials.mint(scope, f"compat {scope}", "paired")
        assert rec["mirrored"] is True
        minted[scope] = (rec, secret)
    extras = _extra_keys(store)
    for scope, (_rec, secret) in minted.items():
        assert secret in extras, scope
        assert old_rule_accepts(store, secret), scope
    assert not old_rule_accepts(store, "pr1.c_0000000000000000.not-a-minted-key")

    credentials.revoke(minted["ha"][0]["id"])
    assert minted["ha"][1] not in _extra_keys(store)
    assert not old_rule_accepts(store, minted["ha"][1])
    for scope in ("display", "satellite"):
        assert old_rule_accepts(store, minted[scope][1]), scope


def test_turning_the_mirror_off_takes_the_copies_away(store, compat):
    """Settings, Security: turning the mirror off removes every copy, so a
    rollback no longer accepts those keys (and this release still does)."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services import credentials
    keys = [credentials.mint(scope, f"compat {scope}", "paired")[1]
            for scope in ("ha", "display", "satellite")]
    assert all(old_rule_accepts(store, k) for k in keys)
    owner = TestClient(app, client=("127.0.0.1", 50000), base_url="http://localhost:9284")
    r = owner.post("/setup/security/settings", json={"legacy_mirror": False},
                   follow_redirects=False)
    assert r.status_code == 200 and r.json()["saved"] == ["legacy_mirror"], r.text
    for k in keys:
        assert not old_rule_accepts(store, k)
        assert credentials.lookup(k) is not None


def test_a_satellite_takes_its_credentials_back_from_an_old_server(compat, monkeypatch,
                                                                    tmp_path):
    """A Bandit on this release was relaying through its server, so it held
    no AI key or Home Assistant token. The server went back to v0.20.0: the
    pull it answers has no relay block, the Bandit leaves relay mode and
    _apply_config puts the credentials the pull carries back."""
    from app.config import settings as app_settings
    from app.services import satellite

    pull = compat.RESPONSES["satellite_pull"]
    cfg = pull["json"]["config"]
    compat.configure_satellite(
        monkeypatch, tmp_path, upstream_api_key=pull["key"], gemini_api_key="",
        streamdeck_ha_token="",
        fleet_relay={"v": 1, "caps": ["ai", "recipes_external", "ha", "camera"],
                     "ai_available": True, "enrich_available": True, "ha_available": True,
                     "legacy_credentials": False, "server_has_password": True})

    applied = satellite._apply_config(dict(cfg), allow_sensitive=True, relay=False)
    assert "gemini_api_key" in applied and "streamdeck_ha_token" in applied
    assert app_settings.gemini_api_key == cfg["gemini_api_key"]
    assert app_settings.streamdeck_ha_token == cfg["streamdeck_ha_token"]

    # And the whole pull, as the Bandit runs it, leaves relay mode.
    monkeypatch.setattr(app_settings, "gemini_api_key", "", raising=False)
    monkeypatch.setattr(satellite.httpx, "get", lambda url, headers=None, timeout=None:
                        httpx.Response(pull["status"], json=pull["json"],
                                       request=httpx.Request("GET", url)))
    monkeypatch.setattr(satellite._secrets, "token_hex", lambda n=16: pull["nonce"])
    for name in ("_apply_defaults", "_apply_profiles"):
        monkeypatch.setattr(satellite, name, lambda rows: 0)
    monkeypatch.setattr(satellite, "_apply_gadget_config", lambda data: None)
    monkeypatch.setattr(satellite, "mirror_alarms", lambda alarms: 0)
    monkeypatch.setattr(satellite, "_push_streamdeck_settings", lambda *a, **k: False)
    monkeypatch.setattr(satellite, "_resolve_host", lambda host: "")
    result = satellite._do_sync_from_upstream()
    assert result["ok"] is True, result
    assert app_settings.fleet_relay == {}
    assert app_settings.gemini_api_key == cfg["gemini_api_key"]
