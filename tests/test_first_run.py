"""Zero-touch first-run provisioning for Grocy and Mealie (FoodAssistant-syxf).

Pure tests, no network: httpx.AsyncClient is replaced by a scripted fake so
the exact sequences (Grocy session login + manageapikeys, Mealie token +
password + shopping list) and the hands-off rules can be exercised offline.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings, _SAVEABLE, SECRET_SETTING_KEYS  # noqa: E402
from app.services import first_run  # noqa: E402


# -- Scripted httpx stand-in --------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class FakeClient:
    """Maps (METHOD, url) to a FakeResponse, an Exception to raise, or a list
    consumed in order. Records every call for assertions."""

    def __init__(self, routes, calls):
        self.routes = routes
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kw):
        self.calls.append((method.upper(), url, kw))
        key = (method.upper(), url)
        if key not in self.routes:
            raise AssertionError(f"unexpected request: {method} {url}")
        resp = self.routes[key]
        if isinstance(resp, list):
            resp = resp.pop(0) if len(resp) > 1 else resp[0]
        if isinstance(resp, Exception):
            raise resp
        return resp

    async def get(self, url, **kw):
        return await self.request("GET", url, **kw)

    async def post(self, url, **kw):
        return await self.request("POST", url, **kw)

    async def put(self, url, **kw):
        return await self.request("PUT", url, **kw)


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    """A pristine install: writable data dir, nothing configured yet."""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    monkeypatch.setattr(settings, "grocy_admin_password", "", raising=False)
    monkeypatch.setattr(settings, "mealie_base_url", "", raising=False)
    monkeypatch.setattr(settings, "mealie_api_key", "", raising=False)
    monkeypatch.setattr(settings, "mealie_admin_password", "", raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    return tmp_path


def wire(monkeypatch, routes):
    """Point first_run's httpx.AsyncClient at the scripted fake."""
    calls = []
    monkeypatch.setattr(first_run.httpx, "AsyncClient",
                        lambda **kw: FakeClient(routes, calls))
    return calls


def wire_forbidden(monkeypatch):
    """Fail the test if provisioning opens any HTTP client at all."""
    def boom(**kw):
        raise AssertionError("provisioning must not touch the network here")
    monkeypatch.setattr(first_run.httpx, "AsyncClient", boom)


# -- Pure parsing helpers -----------------------------------------------------


GROCY_PAGE = """
<a class="btn btn-danger btn-sm apikey-delete-button" href="#"
   data-apikey-id="1"
   data-apikey-key="oldkeyoldkeyoldkeyoldkeyoldkeyoldkeyoldkeyoldkey01"
   data-apikey-description="">x</a>
<a class="btn btn-danger btn-sm apikey-delete-button" href="#"
   data-apikey-id="2"
   data-apikey-key="newkeynewkeynewkeynewkeynewkeynewkeynewkeynewkey02"
   data-apikey-description="Pantry Raider">x</a>
"""


def test_parse_grocy_api_key_by_id():
    key = first_run.parse_grocy_api_key(GROCY_PAGE, key_id="2")
    assert key == "newkeynewkeynewkeynewkeynewkeynewkeynewkeynewkey02"


def test_parse_grocy_api_key_by_description_fallback():
    key = first_run.parse_grocy_api_key(GROCY_PAGE, key_id="")
    assert key == "newkeynewkeynewkeynewkeynewkeynewkeynewkeynewkey02"


def test_parse_grocy_api_key_missing():
    assert first_run.parse_grocy_api_key("<html></html>", key_id="9") == ""
    assert first_run.parse_grocy_api_key("", key_id="") == ""


def test_grocy_login_redirect_interpretation():
    assert first_run.grocy_login_succeeded(302, "/") is True
    assert first_run.grocy_login_succeeded(302, "/login?invalid=true") is False
    # Not a redirect at all: not answering properly yet, neither yes nor no.
    assert first_run.grocy_login_succeeded(200, "") is None
    assert first_run.grocy_login_succeeded(502, "") is None


def test_grocy_new_key_id_from_redirect():
    assert first_run.grocy_new_key_id("/manageapikeys?key=7") == "7"
    assert first_run.grocy_new_key_id("http://x/manageapikeys?key=12") == "12"
    assert first_run.grocy_new_key_id("/manageapikeys") == ""
    assert first_run.grocy_new_key_id("") == ""


def test_generated_password_is_strong_and_unique():
    a, b = first_run.generate_password(), first_run.generate_password()
    assert a != b
    assert len(a) >= 12


def test_new_settings_are_saveable_secrets():
    for key in ("grocy_admin_password", "mealie_admin_password"):
        assert key in _SAVEABLE
        assert key in SECRET_SETTING_KEYS


# -- Grocy provisioning -------------------------------------------------------


def grocy_routes():
    base = "http://grocy.test"
    return {
        ("POST", f"{base}/login"): FakeResponse(302, headers={"location": "/"}),
        ("GET", f"{base}/manageapikeys/new"): FakeResponse(
            302, headers={"location": "/manageapikeys?key=2"}),
        ("GET", f"{base}/manageapikeys"): FakeResponse(200, text=GROCY_PAGE),
        ("GET", f"{base}/api/system/info"): FakeResponse(
            200, json_data={"grocy_version": {"Version": "4.6.0"}}),
        ("GET", f"{base}/api/users"): FakeResponse(200, json_data=[
            {"id": 1, "username": "admin", "first_name": None,
             "last_name": None, "picture_file_name": None},
        ]),
        ("PUT", f"{base}/api/users/1"): FakeResponse(204),
    }


@pytest.mark.anyio
async def test_grocy_happy_path(fresh, monkeypatch):
    calls = wire(monkeypatch, grocy_routes())
    report = await first_run.provision_grocy()
    assert report["ok"] and report["configured"] and not report["retryable"]
    assert settings.grocy_api_key == (
        "newkeynewkeynewkeynewkeynewkeynewkeynewkeynewkey02")
    assert settings.grocy_admin_password  # admin secured with a generated one
    # Persisted, not just applied in memory.
    saved = json.loads((fresh / "settings.json").read_text())
    assert saved["grocy_api_key"] == settings.grocy_api_key
    assert saved["grocy_admin_password"] == settings.grocy_admin_password
    assert saved["grocy_base_url"] == "http://grocy.test"
    # The password change went to the admin user with the factory username kept.
    put = next(c for c in calls if c[0] == "PUT")
    assert put[2]["json"]["username"] == "admin"
    assert put[2]["json"]["password"] == settings.grocy_admin_password
    done = {s["step"] for s in report["steps"] if s["done"]}
    assert {"sign-in", "create API key", "verify",
            "save settings", "secure admin sign-in"} <= done


@pytest.mark.anyio
async def test_grocy_password_change_failure_still_connects(fresh, monkeypatch):
    routes = grocy_routes()
    routes[("PUT", "http://grocy.test/api/users/1")] = FakeResponse(400)
    wire(monkeypatch, routes)
    report = await first_run.provision_grocy()
    # The key is the deliverable and lands first; the password step degrades.
    assert report["ok"] and report["configured"]
    assert settings.grocy_api_key
    assert settings.grocy_admin_password == ""
    step = next(s for s in report["steps"] if s["step"] == "secure admin sign-in")
    assert step["skipped"] and not step["done"]


@pytest.mark.anyio
async def test_grocy_already_configured_is_untouched(fresh, monkeypatch):
    monkeypatch.setattr(settings, "grocy_api_key", "existing", raising=False)
    wire_forbidden(monkeypatch)
    report = await first_run.provision_grocy()
    assert report["ok"] and report["configured"]
    assert report["steps"][0]["skipped"]
    assert settings.grocy_api_key == "existing"


@pytest.mark.anyio
async def test_grocy_changed_password_means_hands_off(fresh, monkeypatch):
    routes = {("POST", "http://grocy.test/login"): FakeResponse(
        302, headers={"location": "/login?invalid=true"})}
    calls = wire(monkeypatch, routes)
    report = await first_run.provision_grocy()
    assert report["ok"] and not report["configured"] and not report["retryable"]
    assert settings.grocy_api_key == ""
    # Exactly one login attempt and nothing else was touched.
    assert calls == [c for c in calls if c[1].endswith("/login")]
    assert len(calls) == 1


@pytest.mark.anyio
async def test_grocy_unreachable_is_retryable(fresh, monkeypatch):
    routes = {("POST", "http://grocy.test/login"):
              httpx.ConnectError("connection refused")}
    wire(monkeypatch, routes)
    report = await first_run.provision_grocy()
    assert not report["ok"] and report["retryable"] and not report["configured"]
    assert settings.grocy_api_key == ""


@pytest.mark.anyio
async def test_grocy_odd_answer_is_retryable_not_hands_off(fresh, monkeypatch):
    # A proxy 502 or a still-booting app must read as "try again", never as
    # "the password was changed".
    routes = {("POST", "http://grocy.test/login"): FakeResponse(502)}
    wire(monkeypatch, routes)
    report = await first_run.provision_grocy()
    assert not report["ok"] and report["retryable"]


@pytest.mark.anyio
async def test_grocy_key_not_readable_yet_is_retryable(fresh, monkeypatch):
    """Sign-in succeeds but the Manage API keys page has not rendered the new
    key yet (a Grocy still finishing its first-boot init). This must be
    retryable, never a definite give-up: treating it as final made the retry
    loop stop for good AND release the readiness gate, dropping the user into a
    Grocy-less app seconds before Grocy was ready (FoodAssistant-n2b4)."""
    routes = grocy_routes()
    # The key page comes back with nothing our parser can read.
    routes[("GET", "http://grocy.test/manageapikeys")] = FakeResponse(
        200, text="<html><body>still starting</body></html>")
    wire(monkeypatch, routes)
    report = await first_run.provision_grocy()
    assert not report["configured"] and report["retryable"]
    assert settings.grocy_api_key == ""


# -- Mealie provisioning ------------------------------------------------------


def mealie_routes(shopping_items=None, scope="households"):
    base = "http://mealie.test"
    return {
        ("POST", f"{base}/api/auth/token"): FakeResponse(
            200, json_data={"access_token": "AT", "token_type": "bearer"}),
        ("POST", f"{base}/api/users/api-tokens"): FakeResponse(
            201, json_data={"token": "TOK", "name": "Pantry Raider", "id": 1}),
        ("PUT", f"{base}/api/users/password"): FakeResponse(
            200, json_data={"message": "Password updated"}),
        ("GET", f"{base}/api/{scope}/shopping/lists"): FakeResponse(
            200, json_data={"items": shopping_items or []}),
        ("POST", f"{base}/api/{scope}/shopping/lists"): FakeResponse(
            201, json_data={"id": "abc", "name": "Groceries"}),
    }


@pytest.mark.anyio
async def test_mealie_happy_path(fresh, monkeypatch):
    calls = wire(monkeypatch, mealie_routes())
    report = await first_run.provision_mealie("http://mealie.test")
    assert report["ok"] and report["configured"] and not report["retryable"]
    assert settings.mealie_api_key == "TOK"
    assert settings.mealie_base_url == "http://mealie.test"
    assert settings.mealie_admin_password
    saved = json.loads((fresh / "settings.json").read_text())
    assert saved["mealie_api_key"] == "TOK"
    assert saved["mealie_admin_password"] == settings.mealie_admin_password
    # The API token was created with the factory bearer, before anything else.
    token_call = next(c for c in calls if c[1].endswith("/api/users/api-tokens"))
    assert token_call[2]["json"] == {"name": "Pantry Raider",
                                     "integrationId": "generic"}
    # The password change used the documented body shape.
    pw_call = next(c for c in calls if c[0] == "PUT")
    assert pw_call[2]["json"]["currentPassword"] == "MyPassword"
    assert pw_call[2]["json"]["newPassword"] == settings.mealie_admin_password
    # A Groceries list was created because none existed.
    create = next(c for c in calls
                  if c[0] == "POST" and c[1].endswith("/shopping/lists"))
    assert create[2]["json"] == {"name": "Groceries"}


@pytest.mark.anyio
async def test_mealie_existing_shopping_list_is_kept(fresh, monkeypatch):
    calls = wire(monkeypatch, mealie_routes(
        shopping_items=[{"id": "1", "name": "My list"}]))
    report = await first_run.provision_mealie("http://mealie.test")
    assert report["ok"] and report["configured"]
    # No second list is ever created.
    assert not [c for c in calls
                if c[0] == "POST" and c[1].endswith("/shopping/lists")]
    step = next(s for s in report["steps"]
                if s["step"] == "default shopping list")
    assert step["skipped"]


@pytest.mark.anyio
async def test_mealie_v1_groups_fallback(fresh, monkeypatch):
    # Mealie 1.x has no /api/households routes: they 404 and /api/groups works.
    routes = mealie_routes(scope="groups")
    base = "http://mealie.test"
    routes[("GET", f"{base}/api/households/shopping/lists")] = FakeResponse(404)
    routes[("POST", f"{base}/api/households/shopping/lists")] = FakeResponse(404)
    wire(monkeypatch, routes)
    report = await first_run.provision_mealie("http://mealie.test")
    assert report["ok"] and report["configured"]
    step = next(s for s in report["steps"]
                if s["step"] == "default shopping list")
    assert step["done"]


@pytest.mark.anyio
async def test_mealie_already_configured_is_untouched(fresh, monkeypatch):
    monkeypatch.setattr(settings, "mealie_base_url", "http://m", raising=False)
    monkeypatch.setattr(settings, "mealie_api_key", "existing", raising=False)
    wire_forbidden(monkeypatch)
    report = await first_run.provision_mealie()
    assert report["ok"] and report["configured"]
    assert settings.mealie_api_key == "existing"


@pytest.mark.anyio
async def test_mealie_changed_password_means_hands_off(fresh, monkeypatch):
    routes = {("POST", "http://mealie.test/api/auth/token"): FakeResponse(401)}
    calls = wire(monkeypatch, routes)
    report = await first_run.provision_mealie("http://mealie.test")
    assert report["ok"] and not report["configured"] and not report["retryable"]
    assert settings.mealie_api_key == ""
    # One login attempt only: never retried, so Mealie's failed-login lockout
    # can never be tripped by provisioning.
    assert len(calls) == 1


@pytest.mark.anyio
async def test_mealie_locked_account_means_hands_off(fresh, monkeypatch):
    routes = {("POST", "http://mealie.test/api/auth/token"): FakeResponse(423)}
    wire(monkeypatch, routes)
    report = await first_run.provision_mealie("http://mealie.test")
    assert not report["configured"] and not report["retryable"]


@pytest.mark.anyio
async def test_mealie_unreachable_is_retryable(fresh, monkeypatch):
    routes = {("POST", "http://mealie.test/api/auth/token"):
              httpx.ConnectError("connection refused")}
    wire(monkeypatch, routes)
    report = await first_run.provision_mealie("http://mealie.test")
    assert not report["ok"] and report["retryable"]
    assert settings.mealie_api_key == ""


@pytest.mark.anyio
async def test_mealie_no_address_is_a_clean_no(fresh, monkeypatch):
    wire_forbidden(monkeypatch)
    report = await first_run.provision_mealie()
    assert not report["ok"] and not report["retryable"] and not report["configured"]


# -- Retry wrapper and startup trigger ----------------------------------------


@pytest.mark.anyio
async def test_provision_mealie_when_up_stops_on_hands_off(fresh, monkeypatch):
    attempts = []

    async def fake_provision(base):
        attempts.append(base)
        return {"ok": True, "configured": False, "retryable": False}

    monkeypatch.setattr(first_run, "provision_mealie", fake_provision)
    report = await first_run.provision_mealie_when_up("http://m", attempts=5,
                                                      delay=0)
    assert len(attempts) == 1
    assert not report["configured"]


@pytest.mark.anyio
async def test_provision_mealie_when_up_retries_while_booting(fresh, monkeypatch):
    outcomes = [
        {"ok": False, "configured": False, "retryable": True},
        {"ok": False, "configured": False, "retryable": True},
        {"ok": True, "configured": True, "retryable": False},
    ]
    attempts = []

    async def fake_provision(base):
        attempts.append(base)
        return outcomes[len(attempts) - 1]

    monkeypatch.setattr(first_run, "provision_mealie", fake_provision)
    report = await first_run.provision_mealie_when_up("http://m", attempts=10,
                                                      delay=0)
    assert len(attempts) == 3
    assert report["configured"]


@pytest.mark.anyio
async def test_startup_never_runs_on_a_satellite(fresh, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    wire_forbidden(monkeypatch)
    await first_run.startup_first_run(attempts=1, delay=0)


@pytest.mark.anyio
async def test_startup_skips_configured_backends(fresh, monkeypatch):
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "mealie_base_url", "http://m", raising=False)
    monkeypatch.setattr(settings, "mealie_api_key", "t", raising=False)
    wire_forbidden(monkeypatch)
    await first_run.startup_first_run(attempts=1, delay=0)


@pytest.mark.anyio
async def test_startup_provisions_the_unconfigured_backend(fresh, monkeypatch):
    grocy_calls, mealie_calls = [], []

    async def fake_grocy(base=""):
        grocy_calls.append(base)
        return {"ok": True, "configured": True, "retryable": False}

    async def fake_mealie(base=""):
        mealie_calls.append(base)
        # Optional backend not running: unreachable every time is fine.
        return {"ok": False, "configured": False, "retryable": True}

    monkeypatch.setattr(first_run, "provision_grocy", fake_grocy)
    monkeypatch.setattr(first_run, "provision_mealie", fake_mealie)
    await first_run.startup_first_run(attempts=2, delay=0, initial_delay=0)
    assert grocy_calls == ["http://grocy.test"]  # settled on the first pass
    assert len(mealie_calls) >= 2                # kept retrying, gave up quietly
    assert settings.mealie_api_key == ""


# -- Where provisioning looks for a co-hosted Grocy ----------------------------
#
# The gap this pins (Dan, 2026-07-16): the readiness gate and the setup page's
# "Grocy detected locally" probe both try loopback unconditionally, while
# provisioning used to try it only when it could tell it was on a Pi. A Grocy
# those two could see was therefore left unprovisioned, and the user got a
# settings page with an empty inventory pane and no explanation.


def test_candidates_add_local_addresses_when_no_grocy_chosen(monkeypatch):
    monkeypatch.setattr(settings, "grocy_base_url", "", raising=False)
    assert first_run._grocy_candidates() == first_run._LOCAL_GROCY_CANDIDATES


def test_candidates_add_local_addresses_for_the_compose_default(monkeypatch):
    """The unseeded appliance case: the default address is not a choice."""
    from app.config import _DEFAULT_GROCY_URL
    monkeypatch.setattr(settings, "grocy_base_url", _DEFAULT_GROCY_URL, raising=False)
    cands = first_run._grocy_candidates()
    assert cands[0] == _DEFAULT_GROCY_URL
    assert "http://localhost:9383" in cands   # tried even off a detected Pi
    assert len(cands) == len(set(cands))      # no duplicate probing


def test_candidates_do_not_second_guess_a_chosen_grocy(monkeypatch):
    """A user who named their own server must not have the box's own Grocy
    connected behind their back, overwriting the address they asked for."""
    monkeypatch.setattr(settings, "grocy_base_url", "http://nas.local:9383",
                        raising=False)
    assert first_run._grocy_candidates() == ["http://nas.local:9383"]


@pytest.mark.anyio
async def test_startup_falls_through_to_loopback_when_the_default_is_dead(
        fresh, monkeypatch):
    """The compose service name does not resolve on an appliance (host
    networking), so provisioning must go on to try loopback."""
    from app.config import _DEFAULT_GROCY_URL
    monkeypatch.setattr(settings, "grocy_base_url", _DEFAULT_GROCY_URL, raising=False)
    tried = []

    async def fake_grocy(base=""):
        tried.append(base)
        if base == "http://localhost:9383":
            return {"ok": True, "configured": True, "retryable": False}
        return {"ok": False, "configured": False, "retryable": True}

    async def fake_mealie(base=""):
        return {"ok": False, "configured": False, "retryable": True}

    monkeypatch.setattr(first_run, "provision_grocy", fake_grocy)
    monkeypatch.setattr(first_run, "provision_mealie", fake_mealie)
    await first_run.startup_first_run(attempts=1, delay=0, initial_delay=0)
    assert tried[0] == _DEFAULT_GROCY_URL
    assert "http://localhost:9383" in tried


# -- Handing the first-boot readiness page back to the user --------------------


@pytest.mark.anyio
async def test_startup_releases_the_readiness_gate_when_grocy_settles(
        fresh, monkeypatch):
    from app.services import readiness
    released = []
    monkeypatch.setattr(readiness, "mark_provisioning_done",
                        lambda: released.append(True))

    async def hands_off(base=""):
        # Someone's own Grocy: a definite no, so no key is ever coming.
        return {"ok": True, "configured": False, "retryable": False}

    async def fake_mealie(base=""):
        return {"ok": False, "configured": False, "retryable": True}

    monkeypatch.setattr(first_run, "provision_grocy", hands_off)
    monkeypatch.setattr(first_run, "provision_mealie", fake_mealie)
    await first_run.startup_first_run(attempts=1, delay=0, initial_delay=0)
    assert released, "the user must not be left on the getting-ready page"


@pytest.mark.anyio
async def test_startup_giveup_keeps_the_gate_holding(fresh, monkeypatch):
    """Grocy has not come up within this bounded task's attempts. It must NOT
    release the gate: the appliance's Grocy is co-hosted and WILL come up, and
    the getting-ready page keeps re-driving provisioning every few seconds while
    it is shown. Releasing on a bounded give-up dropped users into a Grocy-less
    app while Grocy was still finishing its first-boot init (FoodAssistant-n2b4).
    Only a connect or a definite hands-off releases it (the backstop deadline and
    the user's dismissal cover a genuinely wedged backend)."""
    from app.services import readiness
    released = []
    monkeypatch.setattr(readiness, "mark_provisioning_done",
                        lambda: released.append(True))

    async def never_up(base=""):
        return {"ok": False, "configured": False, "retryable": True}

    monkeypatch.setattr(first_run, "provision_grocy", never_up)
    monkeypatch.setattr(first_run, "provision_mealie", never_up)
    await first_run.startup_first_run(attempts=2, delay=0, initial_delay=0)
    assert not released, "a bounded give-up must not wall the user into a broken app"


# -- Stillborn Grocy self-heal (0-byte database wedge) --------------------------
#
# Grocy only builds its schema while serving a GET of its root route, which a
# hands-off appliance boot never requests, so grocy.db can stay a permanent
# 0-byte stub with every sign-in answering HTTP 500. The heal watches for that
# exact signature, nudges the root route (the actual migration trigger), and
# only escalates to the host bridge's container-level repair when the nudge
# cannot cure it.


def _wedge_report(status=500):
    """The exact report provision_grocy emits for a 5xx sign-in answer."""
    return {"ok": False, "configured": False, "retryable": True,
            "message": "Grocy is not answering properly yet; "
                       "try again in a moment.",
            "steps": [{"step": "sign-in", "done": False, "skipped": False,
                       "reason": f"Unexpected answer (HTTP {status})."}]}


def _unreachable_report():
    return {"ok": False, "configured": False, "retryable": True,
            "message": "Grocy is not answering yet.",
            "steps": [{"step": "sign-in", "done": False, "skipped": False,
                       "reason": "Grocy is not reachable."}]}


@pytest.fixture
def heal_state():
    first_run._reset_grocy_heal_state()
    yield
    first_run._reset_grocy_heal_state()


@pytest.fixture
def appliance(fresh, monkeypatch, heal_state):
    """A pi_hosted appliance with its seeded loopback Grocy address."""
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://localhost:9383",
                        raising=False)
    return fresh


def test_signin_5xx_signature_matches_only_server_errors():
    assert first_run.grocy_signin_answered_5xx(_wedge_report(500)) is True
    assert first_run.grocy_signin_answered_5xx(_wedge_report(503)) is True
    # Unreachable is normal booting, never the wedge.
    assert first_run.grocy_signin_answered_5xx(_unreachable_report()) is False
    # A non-5xx odd answer (say a proxy page) is not the wedge either.
    assert first_run.grocy_signin_answered_5xx(_wedge_report(200)) is False


def test_signin_5xx_signature_ignores_other_outcomes():
    hands_off = {"ok": True, "configured": False, "retryable": False,
                 "steps": [{"step": "sign-in", "done": False, "skipped": True,
                            "reason": "The admin password has been changed."}]}
    assert first_run.grocy_signin_answered_5xx(hands_off) is False
    key_pending = {"ok": False, "configured": False, "retryable": True,
                   "steps": [{"step": "sign-in", "done": True, "skipped": False,
                              "reason": ""},
                             {"step": "create API key", "done": False,
                              "skipped": False,
                              "reason": "Could not read the new key back yet."}]}
    assert first_run.grocy_signin_answered_5xx(key_pending) is False
    assert first_run.grocy_signin_answered_5xx({}) is False


@pytest.mark.anyio
async def test_wedged_grocy_emits_the_signature_on_the_wire(fresh, monkeypatch):
    """A real provision run against a 500-answering login POST must produce
    exactly the report the heal detector matches."""
    routes = {("POST", "http://grocy.test/login"): FakeResponse(500)}
    wire(monkeypatch, routes)
    report = await first_run.provision_grocy()
    assert report["retryable"]
    assert first_run.grocy_signin_answered_5xx(report) is True


def test_heal_eligibility_is_appliance_loopback_only(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    assert first_run._grocy_heal_eligible("http://localhost:9383") is True
    assert first_run._grocy_heal_eligible("http://127.0.0.1:9383") is True
    assert first_run._grocy_heal_eligible("http://nas.local:9383") is False
    assert first_run._grocy_heal_eligible("http://grocy:80") is False
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    assert first_run._grocy_heal_eligible("http://localhost:9383") is False


# The migration nudge itself: one GET of Grocy's root route, following the
# one-shot back-to-root redirect a brand-new Grocy answers first.


@pytest.mark.anyio
async def test_nudge_follows_the_one_shot_redirect_then_succeeds(
        fresh, monkeypatch):
    base = "http://localhost:9383"
    routes = {("GET", f"{base}/"): [
        FakeResponse(302, headers={"location": "/"}),
        FakeResponse(302, headers={"location": "/stockoverview"}),
    ]}
    calls = wire(monkeypatch, routes)
    assert await first_run._nudge_grocy_migrations(base) is True
    assert len(calls) == 2


@pytest.mark.anyio
async def test_nudge_success_without_redirect_is_one_request(fresh, monkeypatch):
    base = "http://localhost:9383"
    routes = {("GET", f"{base}/"): FakeResponse(
        302, headers={"location": "/stockoverview"})}
    calls = wire(monkeypatch, routes)
    assert await first_run._nudge_grocy_migrations(base) is True
    assert len(calls) == 1


@pytest.mark.anyio
async def test_nudge_reports_failure_on_500_and_unreachable(fresh, monkeypatch):
    base = "http://localhost:9383"
    routes = {("GET", f"{base}/"): FakeResponse(500)}
    wire(monkeypatch, routes)
    assert await first_run._nudge_grocy_migrations(base) is False
    routes = {("GET", f"{base}/"): httpx.ConnectError("refused")}
    wire(monkeypatch, routes)
    assert await first_run._nudge_grocy_migrations(base) is False


# The startup-loop trigger: three consecutive 5xx sign-ins fire one heal.


def _wire_heal(monkeypatch, *, reports, nudge_result=False,
               repair_answer=None):
    """Drive startup_first_run with scripted provisioning reports and record
    every nudge and bridge-repair call."""
    nudges, repairs = [], []
    seen = {"n": 0}

    async def fake_grocy(base=""):
        seen["n"] += 1
        return reports[min(seen["n"], len(reports)) - 1]

    async def fake_mealie(base=""):
        return {"ok": False, "configured": False, "retryable": True}

    async def fake_nudge(base):
        nudges.append(base)
        return nudge_result

    async def fake_repair():
        repairs.append(True)
        return repair_answer or {"ok": True, "action": "repaired",
                                 "moved": "/opt/x/grocy.db.stillborn-1"}

    monkeypatch.setattr(first_run, "provision_grocy", fake_grocy)
    monkeypatch.setattr(first_run, "provision_mealie", fake_mealie)
    monkeypatch.setattr(first_run, "_nudge_grocy_migrations", fake_nudge)
    monkeypatch.setattr(first_run, "_request_grocy_repair", fake_repair)
    return nudges, repairs


@pytest.mark.anyio
async def test_three_consecutive_5xx_signins_trigger_one_repair(
        appliance, monkeypatch):
    nudges, repairs = _wire_heal(monkeypatch, reports=[_wedge_report()])
    await first_run.startup_first_run(attempts=4, delay=0, initial_delay=0)
    # The nudge runs first; it could not cure, so the bridge was asked once.
    assert nudges == ["http://localhost:9383"]
    assert len(repairs) == 1


@pytest.mark.anyio
async def test_two_5xx_signins_are_not_enough(appliance, monkeypatch):
    nudges, repairs = _wire_heal(monkeypatch, reports=[_wedge_report()])
    await first_run.startup_first_run(attempts=2, delay=0, initial_delay=0)
    assert nudges == [] and repairs == []


@pytest.mark.anyio
async def test_a_successful_nudge_skips_the_bridge_repair(
        appliance, monkeypatch):
    nudges, repairs = _wire_heal(monkeypatch, reports=[_wedge_report()],
                                 nudge_result=True)
    await first_run.startup_first_run(attempts=4, delay=0, initial_delay=0)
    assert len(nudges) == 1
    assert repairs == []


@pytest.mark.anyio
async def test_streak_resets_on_any_non_5xx_answer(appliance, monkeypatch):
    # 5xx, 5xx, unreachable, 5xx, 5xx, 5xx: only the last three are
    # consecutive, so exactly one heal fires, on the sixth attempt.
    reports = [_wedge_report(), _wedge_report(), _unreachable_report(),
               _wedge_report(), _wedge_report(), _wedge_report()]
    nudges, repairs = _wire_heal(monkeypatch, reports=reports)
    await first_run.startup_first_run(attempts=6, delay=0, initial_delay=0)
    assert len(nudges) == 1 and len(repairs) == 1


@pytest.mark.anyio
async def test_repair_requests_are_capped_per_process(appliance, monkeypatch):
    # Twelve straight 5xx attempts reach the streak threshold four times, but
    # the bridge is only ever asked twice; the nudge (harmless, unprivileged)
    # keeps trying each time.
    nudges, repairs = _wire_heal(monkeypatch, reports=[_wedge_report()])
    await first_run.startup_first_run(attempts=12, delay=0, initial_delay=0)
    assert len(repairs) == 2
    assert len(nudges) == 4


@pytest.mark.anyio
async def test_no_heal_on_server_mode(fresh, heal_state, monkeypatch):
    monkeypatch.setattr(settings, "grocy_base_url", "http://localhost:9383",
                        raising=False)  # local base, but not an appliance
    nudges, repairs = _wire_heal(monkeypatch, reports=[_wedge_report()])
    await first_run.startup_first_run(attempts=6, delay=0, initial_delay=0)
    assert nudges == [] and repairs == []


@pytest.mark.anyio
async def test_no_heal_for_a_remote_grocy(fresh, heal_state, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://nas.local:9383",
                        raising=False)
    nudges, repairs = _wire_heal(monkeypatch, reports=[_wedge_report()])
    await first_run.startup_first_run(attempts=6, delay=0, initial_delay=0)
    assert nudges == [] and repairs == []


@pytest.mark.anyio
async def test_bridge_404_degrades_quietly_and_is_asked_only_once(
        appliance, monkeypatch):
    # An older deployed bridge has no /grocy/repair route. The heal must note
    # that once, keep retrying provisioning normally, and never ask again.
    nudges, repairs = _wire_heal(
        monkeypatch, reports=[_wedge_report()],
        repair_answer={"ok": False, "unavailable": True})
    await first_run.startup_first_run(attempts=9, delay=0, initial_delay=0)
    assert len(repairs) == 1
    assert len(nudges) == 3  # the unprivileged nudge still tries each streak


@pytest.mark.anyio
async def test_a_refusing_bridge_keeps_the_loop_retrying(
        appliance, monkeypatch):
    nudges, repairs = _wire_heal(
        monkeypatch, reports=[_wedge_report()],
        repair_answer={"ok": False, "action": "refused",
                       "reason": "The Grocy database has content, so it was "
                                 "left untouched."})
    # Must not raise, must not stop provisioning, and stays within the cap.
    await first_run.startup_first_run(attempts=8, delay=0, initial_delay=0)
    assert len(repairs) == 2


@pytest.mark.anyio
async def test_heal_never_releases_the_readiness_gate(appliance, monkeypatch):
    from app.services import readiness
    released = []
    monkeypatch.setattr(readiness, "mark_provisioning_done",
                        lambda: released.append(True))
    _wire_heal(monkeypatch, reports=[_wedge_report()])
    await first_run.startup_first_run(attempts=4, delay=0, initial_delay=0)
    assert not released, "the getting-ready page must keep holding mid-heal"


# -- Router endpoints ---------------------------------------------------------


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    # Configured, so the setup-redirect middleware lets /setup/* through.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.example", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c
    os.chdir(cwd)


def test_first_run_route_calls_engine(client, monkeypatch):
    seen = {}

    async def fake_mealie(base=""):
        seen["base"] = base
        return {"ok": True, "configured": True, "retryable": False,
                "message": "done", "steps": []}

    monkeypatch.setattr(first_run, "provision_mealie", fake_mealie)
    r = client.post("/setup/first-run/mealie",
                    json={"base_url": "http://mealie.example"})
    assert r.status_code == 200
    assert r.json()["configured"] is True
    assert seen["base"] == "http://mealie.example"


def test_first_run_route_rejects_unknown_service(client):
    r = client.post("/setup/first-run/nonsense", json={})
    assert r.status_code == 404


def test_first_run_route_refuses_on_satellite(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    # A configured satellite (paired to its main server), so the setup-redirect
    # middleware lets the request through to the route itself.
    monkeypatch.setattr(settings, "remote_server_url", "http://srv:9284", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "u", raising=False)
    r = client.post("/setup/first-run/grocy", json={})
    assert r.json()["ok"] is False


def test_reveal_returns_stored_login_only(client, monkeypatch):
    r = client.post("/setup/first-run/reveal", json={"service": "grocy"})
    assert r.json()["ok"] is False  # nothing stored yet
    monkeypatch.setattr(settings, "grocy_admin_password", "pw1", raising=False)
    r = client.post("/setup/first-run/reveal", json={"service": "grocy"})
    assert r.json() == {"ok": True, "username": "admin", "password": "pw1"}
    monkeypatch.setattr(settings, "mealie_admin_password", "pw2", raising=False)
    r = client.post("/setup/first-run/reveal", json={"service": "mealie"})
    body = r.json()
    assert body["ok"] and body["password"] == "pw2"
    assert body["username"] == "changeme@example.com"
