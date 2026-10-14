"""services/ha_client.py: the one place the app calls Home Assistant.

Covers the connection mode matrix, the key policy check before any request,
quoted URL parts, the upstream (relay) form, that no detail ever carries the
token or the address, list_states with an injected client, the admin and
expiry checks, and the daily checks with the weekly owner-token reminder.
No network: the HTTP clients are fakes.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import ha_client  # noqa: E402

TOKEN = "fake-ha-token-for-tests"
BASE = "http://192.168.1.40:8123"


class _Resp:
    def __init__(self, status=200, body=None, content=b"", headers=None):
        self.status_code = status
        self._body = body
        self.content = content
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeClient:
    """Records every call; answers from a status or raises an error."""

    def __init__(self, status=200, body=None, raise_exc=None):
        self.status = status
        self.body = body if body is not None else {}
        self.raise_exc = raise_exc
        self.calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aclose(self):
        return None

    def _answer(self):
        if self.raise_exc is not None:
            raise self.raise_exc
        return _Resp(self.status, self.body)

    async def get(self, url, headers=None, **kw):
        self.calls.append(("GET", url, headers if headers is not None else kw.get("headers"), None))
        return self._answer()

    async def post(self, url, headers=None, json=None, **kw):
        self.calls.append(("POST", url, headers if headers is not None else kw.get("headers"), json))
        return self._answer()


@pytest.fixture
def direct(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", BASE, raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", TOKEN, raising=False)
    monkeypatch.setattr(settings, "ha_key_policy_version", 0, raising=False)
    monkeypatch.setattr(settings, "ha_token_kind", "", raising=False)
    monkeypatch.setattr(settings, "ha_token_admin", None, raising=False)
    monkeypatch.setattr(settings, "ha_token_expires", "", raising=False)
    fake = _FakeClient()
    monkeypatch.setattr(ha_client, "_direct_client", lambda timeout=6.0: fake)
    return fake


@pytest.fixture
def relay(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "http://192.168.1.10:9284/", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "upstream-test-key", raising=False)
    monkeypatch.setattr(settings, "fleet_relay",
                        {"v": 1, "caps": ["ha"], "ha_available": True}, raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    monkeypatch.setattr(settings, "upstream_ha_policy_version", 0, raising=False)
    fake = _FakeClient()
    monkeypatch.setattr(ha_client, "_upstream_client", lambda timeout=6.0: fake)
    return fake


def _run(coro):
    return asyncio.run(coro)


# -- mode() ------------------------------------------------------------------

def test_mode_server_with_and_without_token(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", BASE, raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", TOKEN, raising=False)
    assert ha_client.mode() == "direct"
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    assert ha_client.mode() == "none"


def test_mode_satellite_legacy_with_local_token(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "fleet_relay", {}, raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", BASE, raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", TOKEN, raising=False)
    assert ha_client.mode() == "direct"


def test_mode_satellite_relay(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", BASE, raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", TOKEN, raising=False)
    monkeypatch.setattr(settings, "fleet_relay",
                        {"v": 1, "caps": ["ha"], "ha_available": True}, raising=False)
    assert ha_client.mode() == "upstream"
    monkeypatch.setattr(settings, "fleet_relay",
                        {"v": 1, "caps": ["ha"], "ha_available": False}, raising=False)
    assert ha_client.mode() == "none"


# -- call_service --------------------------------------------------------------

def test_elevated_key_refused_without_http_when_enforced(direct, monkeypatch):
    monkeypatch.setattr(settings, "ha_key_policy_version", 1, raising=False)
    out = _run(ha_client.call_service("lock.front_door", "lock.unlock", source="deck",
                                      entry={"entity_id": "lock.front_door"}))
    assert out["ok"] is False and out["status"] == 0
    assert "Settings, Start Page" in out["detail"]
    assert direct.calls == []


def test_elevated_key_runs_when_allowed_or_not_enforced(direct, monkeypatch):
    out = _run(ha_client.call_service("lock.front_door", "lock.unlock", source="deck"))
    assert out == {"ok": True, "status": 200, "detail": "Sent lock.unlock"}
    monkeypatch.setattr(settings, "ha_key_policy_version", 1, raising=False)
    out = _run(ha_client.call_service("lock.front_door", "lock.unlock", source="screen",
                                      entry={"allow_elevated": True}))
    assert out["ok"] is True
    assert len(direct.calls) == 2


def test_ble_source_never_runs_elevated(direct):
    out = _run(ha_client.call_service("lock.front_door", "lock.unlock", source="ble"))
    assert out["ok"] is False and out["status"] == 0
    assert direct.calls == []


def test_invalid_call_refused(direct):
    out = _run(ha_client.call_service("all", "light.turn_on", source="deck"))
    assert out["ok"] is False
    assert direct.calls == []


def test_direct_call_sends_normalized_pair_with_quoted_parts(direct):
    out = _run(ha_client.call_service(" Light.Kitchen , light.hall ", " LIGHT.Toggle ",
                                      source="deck"))
    assert out["ok"] is True
    method, url, headers, body = direct.calls[0]
    assert method == "POST"
    assert url == f"{BASE}/api/services/light/toggle"
    assert headers == {"Authorization": f"Bearer {TOKEN}"}
    assert body == {"entity_id": "light.kitchen,light.hall"}


def test_not_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "", raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", "", raising=False)
    out = _run(ha_client.call_service("light.kitchen", "light.toggle", source="deck"))
    assert out == {"ok": False, "status": 0, "detail": ha_client.NOT_CONFIGURED_MSG}


def test_upstream_posts_to_the_server_proxy(relay):
    out = _run(ha_client.call_service("light.kitchen", "light.turn_on", source="satellite"))
    assert out["ok"] is True
    method, url, headers, body = relay.calls[0]
    assert url == "http://192.168.1.10:9284/api/proxy/ha/api/services/light/turn_on"
    assert headers["X-API-Key"] == "upstream-test-key"
    assert headers["X-PR-Client"].startswith("satellite/")
    assert body == {"entity_id": "light.kitchen"}


def test_upstream_refusal_shows_the_servers_own_copy(relay):
    relay.status = 403
    relay.body = {"detail": "That Home Assistant action is not set up here.",
                  "code": "ha_not_allowed"}
    out = _run(ha_client.call_service("light.kitchen", "light.turn_on", source="satellite"))
    assert out == {"ok": False, "status": 403,
                   "detail": "That Home Assistant action is not set up here."}


@pytest.mark.parametrize("status,expected", [
    (401, ha_client.REFUSED_MSG),
    (403, ha_client.REFUSED_MSG),
    (500, "Home Assistant answered 500."),
])
def test_failure_details_never_carry_token_or_url(direct, status, expected):
    direct.status = status
    out = _run(ha_client.call_service("light.kitchen", "light.toggle", source="deck"))
    assert out["ok"] is False and out["status"] == status
    assert out["detail"] == expected
    assert TOKEN not in out["detail"] and "192.168" not in out["detail"]


def test_connection_error_detail_is_plain(direct):
    direct.raise_exc = ConnectionError(f"failed {BASE}/api?token={TOKEN}")
    out = _run(ha_client.call_service("light.kitchen", "light.toggle", source="deck"))
    assert out == {"ok": False, "status": 0, "detail": ha_client.UNREACHABLE_MSG}


# -- list_states ---------------------------------------------------------------

def test_list_states_with_injected_client(direct):
    injected = _FakeClient(body=[{"entity_id": "light.kitchen"}])
    rows = _run(ha_client.list_states(client=injected))
    assert rows == [{"entity_id": "light.kitchen"}]
    assert injected.calls[0][1] == f"{BASE}/api/states"
    assert injected.calls[0][2] == {"Authorization": f"Bearer {TOKEN}"}


def test_list_states_typed_override_and_bad_answers(direct):
    injected = _FakeClient(body={"not": "a list"})
    assert _run(ha_client.list_states("http://192.168.1.41:8123/", "typed", client=injected)) == []
    assert injected.calls[0][1] == "http://192.168.1.41:8123/api/states"
    assert injected.calls[0][2] == {"Authorization": "Bearer typed"}
    assert _run(ha_client.list_states(client=_FakeClient(status=401, body=[]))) == []
    assert _run(ha_client.list_states(client=_FakeClient(raise_exc=OSError("down")))) == []


def test_list_states_is_direct_only(relay):
    with pytest.raises(ha_client.HaNotDirect):
        _run(ha_client.list_states())
    with pytest.raises(ha_client.HaNotDirect):
        _run(ha_client.call_raw_service("persistent_notification", "create", {}))


def test_get_states_caches_and_caps(direct):
    direct.body = {"entity_id": "sensor.a", "state": "1"}
    ids = [f"sensor.s{i}" for i in range(80)]
    out = _run(ha_client.get_states(ids))
    assert len(out) == ha_client.MAX_STATES
    assert len(direct.calls) == ha_client.MAX_STATES
    _run(ha_client.get_states(ids[:3]))
    assert len(direct.calls) == ha_client.MAX_STATES
    assert direct.calls[0][1] == f"{BASE}/api/states/sensor.s0"


# -- admin and expiry checks ---------------------------------------------------

@pytest.mark.parametrize("status,expected", [(200, True), (401, False), (403, False), (500, None)])
def test_is_admin_token(direct, status, expected):
    direct.status = status
    assert _run(ha_client.is_admin_token()) is expected
    method, url, _headers, body = direct.calls[0]
    assert (method, url, body) == ("POST", f"{BASE}/api/template", {"template": "{{ 1 }}"})


def _jwt(claims: dict) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg(claims)}.signature"


def test_token_expiry():
    assert ha_client.token_expiry(_jwt({"exp": 1798761600})) == "2027-01-01"
    assert ha_client.token_expiry(_jwt({"iss": "x"})) == ""
    assert ha_client.token_expiry("not-a-jwt") == ""
    assert ha_client.token_expiry("a.!!!.c") == ""


def test_token_ref_names_the_refresh_token_without_showing_it():
    import hashlib
    ref = ha_client.token_ref(_jwt({"iss": "abc123", "exp": 1798761600}))
    assert ref == hashlib.sha256(b"abc123").hexdigest()[:16]
    assert "abc123" not in ref
    assert ha_client.token_ref(_jwt({"exp": 1798761600})) == ""
    assert ha_client.token_ref("not-a-jwt") == ""


# -- daily checks ----------------------------------------------------------------

def _patch_checks(monkeypatch, admin):
    calls = {"admin": 0, "notify": []}

    async def fake_admin():
        calls["admin"] += 1
        return admin

    async def fake_raw(domain, service, data):
        calls["notify"].append((domain, service, data))
        return 200

    saved = []

    def fake_save(self, data):
        saved.append(dict(data))
        for key, value in data.items():
            setattr(self, key, value)

    monkeypatch.setattr(ha_client, "is_admin_token", fake_admin)
    monkeypatch.setattr(ha_client, "call_raw_service", fake_raw)
    monkeypatch.setattr(type(settings), "save", fake_save)
    calls["saved"] = saved
    return calls


def test_daily_checks_run_once_a_day_and_save_only_changes(direct, monkeypatch):
    calls = _patch_checks(monkeypatch, admin=False)
    t0 = 1_800_000_000.0
    token = _jwt({"iss": "r1", "exp": 1830000000})
    monkeypatch.setattr(settings, "streamdeck_ha_token", token, raising=False)
    _run(ha_client.maybe_run_daily_checks(now=t0))
    assert calls["admin"] == 1
    assert calls["saved"] == [{"ha_token_admin": False, "ha_token_expires": "2027-12-28"}]
    _run(ha_client.maybe_run_daily_checks(now=t0 + 3600))
    assert len(calls["saved"]) == 1
    # A day later the expiry is read again, but a token is asked about its
    # account only once: a limited account's 401 is a failed sign-in in Home
    # Assistant's eyes.
    _run(ha_client.maybe_run_daily_checks(now=t0 + 25 * 3600))
    assert calls["admin"] == 1
    assert len(calls["saved"]) == 1  # nothing changed the second time
    # A new token is asked about once.
    monkeypatch.setattr(settings, "streamdeck_ha_token",
                        _jwt({"iss": "r2", "exp": 1830000000}), raising=False)
    ha_client.invalidate_checks()
    _run(ha_client.maybe_run_daily_checks(now=t0 + 26 * 3600))
    assert calls["admin"] == 2
    assert calls["notify"] == []


def test_daily_checks_never_ask_about_the_limited_account(direct, monkeypatch):
    """The integration's own limited account is never an administrator, and
    asking would cost a failed sign-in in Home Assistant's log every day."""
    calls = _patch_checks(monkeypatch, admin=True)
    monkeypatch.setattr(settings, "ha_token_kind", "dedicated", raising=False)
    for day in range(3):
        _run(ha_client.maybe_run_daily_checks(now=1_800_000_000.0 + day * 90_000))
    assert calls["admin"] == 0
    assert calls["saved"] == [{"ha_token_admin": False}]
    assert calls["notify"] == []


def test_weekly_notice_only_for_owner_legacy_admin(direct, monkeypatch):
    calls = _patch_checks(monkeypatch, admin=True)
    monkeypatch.setattr(settings, "ha_token_kind", "owner-legacy", raising=False)
    t0 = 1_800_000_000.0
    _run(ha_client.maybe_run_daily_checks(now=t0))
    assert len(calls["notify"]) == 1
    domain, service, data = calls["notify"][0]
    assert (domain, service) == ("persistent_notification", "create")
    assert data["notification_id"] == "pantry_raider_limited_account"
    assert "Settings, Bandit Remotes" in data["message"]
    # A day later: checked again, but no second notice within the week.
    _run(ha_client.maybe_run_daily_checks(now=t0 + 25 * 3600))
    assert len(calls["notify"]) == 1
    _run(ha_client.maybe_run_daily_checks(now=t0 + 8 * 86400))
    assert len(calls["notify"]) == 2


@pytest.mark.parametrize("kind", ["manual", "dedicated", ""])
def test_no_notice_for_other_token_kinds(direct, monkeypatch, kind):
    calls = _patch_checks(monkeypatch, admin=True)
    monkeypatch.setattr(settings, "ha_token_kind", kind, raising=False)
    _run(ha_client.maybe_run_daily_checks(now=1_800_000_000.0))
    assert calls["admin"] == (0 if kind == "dedicated" else 1)
    assert calls["notify"] == []


def test_daily_checks_skip_satellites_and_unconfigured(relay, monkeypatch):
    calls = _patch_checks(monkeypatch, admin=True)
    _run(ha_client.maybe_run_daily_checks(now=1_800_000_000.0))
    assert calls["admin"] == 0


def test_status_snapshot_and_probe_alert(direct, monkeypatch):
    monkeypatch.setattr(settings, "ha_token_kind", "dedicated", raising=False)
    monkeypatch.setattr(settings, "ha_token_expires", "2026-10-01", raising=False)
    ha_client.record_probe_alert(False, ha_client.PROBE_ALERT_UPDATE_MSG)
    snap = ha_client.status_snapshot(now=1_790_000_000.0)  # 2026-09-21
    assert snap["kind"] == "dedicated"
    assert snap["expires_soon"] is True
    assert snap["probe_alert"]["ok"] is False
    assert snap["probe_alert"]["detail"] == ha_client.PROBE_ALERT_UPDATE_MSG
    path = Path(settings.data_dir) / "ha-status.json"
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    monkeypatch.setattr(settings, "ha_token_expires", "2027-06-01", raising=False)
    assert ha_client.status_snapshot(now=1_790_000_000.0)["expires_soon"] is False
