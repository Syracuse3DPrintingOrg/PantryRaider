"""Zero-touch first-run provisioning for Grocy and Mealie (FoodAssistant-syxf).

Pure tests, no network: httpx.AsyncClient is replaced by a scripted fake so
the exact sequences (Grocy session login + manageapikeys, Mealie token +
password + shopping list) and the hands-off rules can be exercised offline.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings, _SAVEABLE, SECRET_SETTING_KEYS  # noqa: E402
from app.services import first_run  # noqa: E402


# -- Scripted httpx stand-in --------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None,
                 cookies=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = dict(headers or {})
        # The cookies this answer sets, by name (httpx's Response.cookies).
        self.cookies = dict(cookies or {})
        if json_data is not None:
            # A real JSON answer says so, and the Mealie scope detection reads it.
            self.headers.setdefault("content-type", "application/json")

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class FakeClient:
    """Maps (METHOD, url) to a FakeResponse, an Exception to raise, a list
    consumed in order, or a no-argument callable that decides the answer at
    request time (for a backend whose state changes). Records every call for
    assertions."""

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
        if callable(resp):
            resp = resp()
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


@pytest.mark.parametrize("location", ["http://grocy.test/", "/", "http://grocy.test",
                                      "http://grocy.test/index.php/"])
def test_the_one_shot_redirect_is_a_root_redirect_without_a_session(location):
    # Grocy builds it with the same ConstructUrl('/') as a sign-in, so only
    # the missing session cookie says nobody was signed in.
    base = "http://grocy.test"
    assert first_run.grocy_first_request_redirect(302, location, [], base) is True


@pytest.mark.parametrize("cookie", ["grocy_session",                  # 4.6.0
                                    "grocy_session_access_token"])    # 4.7.1
def test_a_real_sign_in_is_never_taken_for_the_one_shot_redirect(cookie):
    base = "http://grocy.test"
    for location in ("http://grocy.test/", "/"):
        assert first_run.grocy_first_request_redirect(
            302, location, ["PHPSESSID", cookie], base) is False


@pytest.mark.parametrize("status, location", [
    (302, "/login?invalid=true"),     # wrong password: hands off, never retried
    (302, "http://grocy.test/login?invalid=true"),
    (302, "/stockoverview"),          # an entry page, not the root
    (302, ""),                        # a redirect that names no address
    (200, "/"),                       # not a redirect at all
    (500, ""),
])
def test_other_sign_in_answers_are_not_the_one_shot_redirect(status, location):
    assert first_run.grocy_first_request_redirect(
        status, location, [], "http://grocy.test") is False


def test_the_one_shot_redirect_under_a_sub_path():
    base = "http://nas.local/grocy"
    assert first_run.grocy_first_request_redirect(302, "http://nas.local/grocy/", [], base)
    assert not first_run.grocy_first_request_redirect(
        302, "http://nas.local/grocy/stockoverview", [], base)


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


def signed_in_answer():
    """Grocy's answer to a sign-in that worked: a 302 to its root that sets
    the session cookie (grocy_session_access_token on 4.7.1)."""
    return FakeResponse(302, headers={"location": "/"},
                        cookies={"grocy_session_access_token": "f00d"})


def grocy_routes(base="http://grocy.test"):
    return {
        ("POST", f"{base}/login"): signed_in_answer(),
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


# -- Concurrency (one run at a time) -------------------------------------------


@pytest.mark.anyio
async def test_two_grocy_runs_do_not_overlap(fresh, monkeypatch):
    """Two provisioners racing both changed the admin password and left
    settings holding the loser's, so the stored sign-in no longer opened
    Grocy. One run at a time, and the second sees the first one's saved key."""
    calls = wire(monkeypatch, grocy_routes())
    import asyncio
    first, second = await asyncio.gather(first_run.provision_grocy(),
                                         first_run.provision_grocy())
    # Exactly one PUT: the second run skipped on the saved key.
    assert len([c for c in calls if c[0] == "PUT"]) == 1
    reports = {r["message"] for r in (first, second)}
    assert any("already connected" in m for m in reports)
    assert settings.grocy_admin_password


@pytest.mark.anyio
async def test_grocy_never_changes_an_already_stored_admin_password(fresh, monkeypatch):
    """A stored generated password is the one that opens Grocy, so a later run
    must not replace it behind the user's back."""
    monkeypatch.setattr(settings, "grocy_admin_password", "already-set", raising=False)
    calls = wire(monkeypatch, grocy_routes())
    report = await first_run.provision_grocy()
    assert report["ok"] and report["configured"]
    assert settings.grocy_admin_password == "already-set"
    assert not [c for c in calls if c[0] == "PUT"]


def test_provisioning_in_progress_is_false_when_idle():
    assert first_run.provisioning_in_progress() is False


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
    # "the password was changed". The proxy answers the migration nudge the
    # same way, so there is no second sign-in.
    routes = {("POST", "http://grocy.test/login"): FakeResponse(502),
              ("GET", "http://grocy.test/"): FakeResponse(502)}
    calls = wire(monkeypatch, routes)
    report = await first_run.provision_grocy()
    assert not report["ok"] and report["retryable"]
    assert len([c for c in calls if c[:2] == ("POST", "http://grocy.test/login")]) == 1


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


def mealie_web_page():
    """A web page where JSON was expected. Mealie 3.24 and later answer a GET
    of an /api path they do not route this way (their web app's page, HTTP
    200), where older versions answered 404."""
    return FakeResponse(200, text="<!DOCTYPE html><html><body>Mealie</body></html>",
                        headers={"content-type": "text/html; charset=utf-8"})


@pytest.mark.anyio
async def test_mealie_scope_falls_back_past_a_web_page():
    base = "http://mealie.test"
    calls = []
    client = FakeClient({
        ("GET", f"{base}/api/households/shopping/lists"): mealie_web_page(),
        ("GET", f"{base}/api/groups/shopping/lists"): FakeResponse(
            200, json_data={"items": [{"id": "1", "name": "Groceries"}]}),
    }, calls)
    r = await first_run._mealie_scoped(client, base, "GET", "/shopping/lists", {})
    # The page reads as "not routed here", so the groups path gets its turn.
    assert r.json() == {"items": [{"id": "1", "name": "Groceries"}]}
    assert [c[1] for c in calls] == [f"{base}/api/households/shopping/lists",
                                      f"{base}/api/groups/shopping/lists"]


@pytest.mark.anyio
async def test_mealie_scope_never_hands_back_a_web_page():
    base = "http://mealie.test"
    client = FakeClient({
        ("POST", f"{base}/api/households/shopping/lists"): mealie_web_page(),
        ("POST", f"{base}/api/groups/shopping/lists"): mealie_web_page(),
    }, [])
    with pytest.raises(ValueError):
        await first_run._mealie_scoped(client, base, "POST", "/shopping/lists",
                                       {}, json={"name": "Groceries"})


@pytest.mark.anyio
async def test_mealie_web_page_is_a_failed_step_not_a_created_list(
        fresh, monkeypatch):
    """A web page answering the create call used to count as success (any
    status under 400), so the report claimed a Groceries list that was never
    made. The API token still lands; only the list step reports the failure."""
    base = "http://mealie.test"
    routes = mealie_routes()
    routes[("POST", f"{base}/api/households/shopping/lists")] = mealie_web_page()
    routes[("POST", f"{base}/api/groups/shopping/lists")] = mealie_web_page()
    wire(monkeypatch, routes)
    report = await first_run.provision_mealie(base)
    assert report["ok"] and report["configured"]
    assert settings.mealie_api_key == "TOK"
    step = next(s for s in report["steps"]
                if s["step"] == "default shopping list")
    assert not step["done"] and not step["skipped"]


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
    exactly the report the heal detector matches. Here the database is too
    broken for the sign-in step's own migration nudge to cure (its root route
    fails too), which is the case the appliance heal exists for."""
    routes = {("POST", "http://grocy.test/login"): FakeResponse(500),
              ("GET", "http://grocy.test/"): FakeResponse(500)}
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

    async def fake_nudge(base, **kw):
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


# -- A fresh Grocy's first migration, on any install but a satellite ----------
#
# The gap this pins: the nudge above only ever ran from the appliance heal
# (pi_hosted, loopback), so a plain server install, the README's one-command
# install included, got HTTP 500 on every sign-in. "Set up for me" kept saying
# Grocy was not answering properly and the background loop gave up after about
# half an hour. The sign-in step now nudges the root route itself on a 5xx
# answer and signs in again in the same run.


def stillborn_grocy(base):
    """Routes for a factory-fresh Grocy whose database was never built: every
    sign-in answers HTTP 500 until its root route has been requested (the one
    thing that runs Grocy's migrations), then the ordinary factory answers.

    A real one also answers its very first request with a one-shot 302 to
    its root; this fake starts after that redirect, and brand_new_grocy below
    adds it."""
    routes = grocy_routes(base)
    signed_in = routes[("POST", f"{base}/login")]
    migrated = []

    def root():
        migrated.append(True)
        return FakeResponse(302, headers={"location": "/stockoverview"})

    routes[("GET", f"{base}/")] = root
    routes[("POST", f"{base}/login")] = (
        lambda: signed_in if migrated else FakeResponse(500))
    return routes


def one_shot_redirect(base):
    """What Grocy answers its very first request of any kind with: a 302 to
    its own root (an absolute address built from the request's host), with
    no session cookie, before it has looked at the request."""
    return FakeResponse(302, headers={"location": f"{base}/"})


def brand_new_grocy(base, routes=None):
    """A factory-fresh Grocy as it really starts: the first request of any
    kind gets the one-shot redirect, then it behaves like ``routes`` (the
    stillborn Grocy above by default). Its key pages need a session, as a
    real Grocy's do: until a sign-in has handed out the session cookie they
    send the request to the sign-in page, which is how the one-shot redirect,
    taken for a sign-in, ended at an unreadable key page."""
    routes = stillborn_grocy(base) if routes is None else routes
    served, sessions = [], []

    def first_request_or(key, answer):
        def respond():
            if not served:
                served.append(True)
                return one_shot_redirect(base)
            if key[1].startswith(f"{base}/manageapikeys") and not sessions:
                return FakeResponse(302, headers={"location": "/login"})
            result = answer() if callable(answer) else answer
            if key == ("POST", f"{base}/login") and result.cookies:
                sessions.append(True)
            return result
        return respond

    return {key: first_request_or(key, answer) for key, answer in routes.items()}


def count_nudges(monkeypatch):
    """Record each migration nudge while still running the real one."""
    nudges = []
    real = first_run._nudge_grocy_migrations

    async def counted(base, **kw):
        nudges.append(base)
        return await real(base, **kw)

    monkeypatch.setattr(first_run, "_nudge_grocy_migrations", counted)
    return nudges


def record_bridge_repairs(monkeypatch):
    repairs = []

    async def repair():
        repairs.append(True)
        return {"ok": False, "error": "not expected here"}

    monkeypatch.setattr(first_run, "_request_grocy_repair", repair)
    return repairs


def _requests(calls, method, url):
    return [c for c in calls if c[:2] == (method, url)]


@pytest.mark.anyio
async def test_server_install_connects_a_fresh_grocy_after_one_nudge(
        fresh, heal_state, monkeypatch):
    base = "http://grocy:80"
    monkeypatch.setattr(settings, "grocy_base_url", base, raising=False)
    calls = wire(monkeypatch, stillborn_grocy(base))
    nudges = count_nudges(monkeypatch)
    repairs = record_bridge_repairs(monkeypatch)
    report = await first_run.provision_grocy()
    assert report["ok"] and report["configured"] and not report["retryable"]
    assert settings.grocy_api_key == (
        "newkeynewkeynewkeynewkeynewkeynewkeynewkeynewkey02")
    assert settings.grocy_admin_password
    # One nudge (a GET of the root, and a second only when Grocy hands out its
    # one-shot back-to-root redirect), then one more sign-in, which worked.
    assert nudges == [base]
    assert 1 <= len(_requests(calls, "GET", f"{base}/")) <= 2
    assert len(_requests(calls, "POST", f"{base}/login")) == 2
    assert repairs == []


@pytest.mark.anyio
async def test_server_startup_connects_a_fresh_grocy_on_its_first_pass(
        fresh, heal_state, monkeypatch):
    """The background loop on a plain server with the compose default address:
    connected on the first pass, with the appliance-only bridge repair never
    involved, and the getting-ready page released."""
    from app.services import readiness
    released = []
    monkeypatch.setattr(readiness, "mark_provisioning_done",
                        lambda: released.append(True))
    from app.config import _DEFAULT_GROCY_URL
    base = _DEFAULT_GROCY_URL
    monkeypatch.setattr(settings, "grocy_base_url", base, raising=False)
    routes = stillborn_grocy(base)
    for local in ("http://localhost:9383", "http://127.0.0.1:9383"):
        routes[("POST", f"{local}/login")] = httpx.ConnectError("refused")
    wire(monkeypatch, routes)
    nudges = count_nudges(monkeypatch)
    repairs = record_bridge_repairs(monkeypatch)

    async def no_mealie(base=""):
        return {"ok": False, "configured": False, "retryable": True}

    monkeypatch.setattr(first_run, "provision_mealie", no_mealie)
    await first_run.startup_first_run(attempts=3, delay=0, initial_delay=0)
    assert settings.grocy_api_key
    assert nudges == [base]
    assert repairs == []
    assert released


@pytest.mark.anyio
async def test_a_satellite_never_nudges_grocy(fresh, heal_state, monkeypatch):
    """A satellite's Grocy belongs to its main server: no GET / from here."""
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    base = "http://grocy:80"
    calls = wire(monkeypatch, stillborn_grocy(base))
    report = await first_run.provision_grocy(base)
    assert not _requests(calls, "GET", f"{base}/")
    assert len(calls) == 1  # the one sign-in and nothing else
    assert report["retryable"] and not report["configured"]
    assert first_run.grocy_signin_answered_5xx(report)


@pytest.mark.anyio
async def test_a_healthy_grocy_gets_no_nudge(fresh, monkeypatch):
    calls = wire(monkeypatch, grocy_routes())
    report = await first_run.provision_grocy()
    assert report["configured"]
    assert not _requests(calls, "GET", "http://grocy.test/")
    assert len(_requests(calls, "POST", "http://grocy.test/login")) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("status", [200, 403, 404])
async def test_a_sign_in_answer_that_is_not_a_server_error_gets_no_nudge(
        fresh, monkeypatch, status):
    """Only a 5xx sign-in is the missing-database symptom. A web page, a
    refusal or a not-found (a proxy, or an address that is not Grocy at all)
    is reported as before: no GET / and no second sign-in."""
    base = "http://grocy.test"
    calls = wire(monkeypatch, {
        ("POST", f"{base}/login"): FakeResponse(status),
        ("GET", f"{base}/"): FakeResponse(
            302, headers={"location": "/stockoverview"}),
    })
    report = await first_run.provision_grocy()
    assert report["retryable"] and not report["configured"]
    assert not _requests(calls, "GET", f"{base}/")
    assert len(_requests(calls, "POST", f"{base}/login")) == 1
    assert not first_run.grocy_signin_answered_5xx(report)


@pytest.mark.anyio
@pytest.mark.parametrize("root_answer, sign_ins", [
    # Grocy cannot serve its root either: no point signing in again.
    (FakeResponse(500), 1),
    # The root answered, yet the sign-in still fails: one retry, never more.
    (FakeResponse(302, headers={"location": "/stockoverview"}), 2),
])
async def test_a_sign_in_that_stays_5xx_keeps_the_retryable_report(
        fresh, monkeypatch, root_answer, sign_ins):
    base = "http://grocy.test"
    calls = wire(monkeypatch, {("POST", f"{base}/login"): FakeResponse(500),
                               ("GET", f"{base}/"): root_answer})
    report = await first_run.provision_grocy()
    assert report["retryable"] and not report["configured"]
    # Still exactly the signature the appliance heal watches for.
    assert first_run.grocy_signin_answered_5xx(report)
    assert _requests(calls, "GET", f"{base}/")
    assert len(_requests(calls, "POST", f"{base}/login")) == sign_ins


@pytest.mark.anyio
async def test_a_failing_nudge_leaves_the_ordinary_report(fresh, monkeypatch):
    """The nudge is best-effort. An unexpected error inside it must end in the
    usual retryable report, never an exception, which the startup loop would
    take as final and stop trying."""
    wire(monkeypatch, {("POST", "http://grocy.test/login"): FakeResponse(500)})

    async def broken(base, **kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(first_run, "_nudge_grocy_migrations", broken)
    report = await first_run.provision_grocy()
    assert report["retryable"] and not report["configured"]
    assert first_run.grocy_signin_answered_5xx(report)


# -- The sign-in nudge holds the provisioning lock, so it is bounded ----------
#
# While an install is not set up, anyone on the network can point "Set up for
# me" at a server that answers the sign-in with a 5xx and then never answers
# GET /. The nudge used to wait out a 300 s read timeout on each request with
# the provisioning lock held, so every other setup attempt was told to wait.
# These run the real httpx client over a MockTransport, which applies no
# network timeouts of its own, so only the nudge's own deadline ends a stall.


def wire_transport(monkeypatch, handler):
    """Run first_run's real httpx client over a MockTransport that answers
    with ``handler`` (sync or async). Returns the keyword arguments each
    client was built with."""
    real = httpx.AsyncClient
    built = []

    def factory(**kw):
        built.append(kw)
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(first_run.httpx, "AsyncClient", factory)
    return built


async def _drip(chunks: list):
    """A response body that never ends: a byte, a pause, forever."""
    while True:
        chunks.append(b"<")
        yield b"<"
        await asyncio.sleep(0.01)


def _hostile(stall: str, root_hits: list, chunks: list):
    """A server that answers the sign-in with a 500 and then stalls GET /:
    "silent" never answers, "drip" answers a byte at a time, and "redirect"
    hands out the one-shot back-to-root redirect first and then goes silent."""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/login":
            return httpx.Response(500, text="boom")
        assert (request.method, request.url.path) == ("GET", "/"), request.url
        root_hits.append(request.url)
        if stall == "redirect" and len(root_hits) == 1:
            return httpx.Response(302, headers={"location": "/"})
        if stall == "drip":
            return httpx.Response(200, content=_drip(chunks))
        await asyncio.sleep(60)
        raise AssertionError("the nudge should have given up long before this")
    return handler


@pytest.mark.anyio
@pytest.mark.parametrize("stall", ["silent", "drip", "redirect"])
async def test_a_stalled_root_route_frees_the_lock_promptly(fresh, monkeypatch, stall):
    monkeypatch.setattr(first_run, "_SIGNIN_NUDGE_DEADLINE", 0.3)
    root_hits, chunks = [], []
    wire_transport(monkeypatch, _hostile(stall, root_hits, chunks))
    started = time.monotonic()
    report = await first_run.provision_grocy("http://stall.test")
    assert time.monotonic() - started < 5
    # The ordinary retryable sign-in report, and nothing saved.
    assert report["retryable"] and not report["configured"]
    assert first_run.grocy_signin_answered_5xx(report)
    assert settings.grocy_api_key == ""
    assert root_hits
    if stall == "drip":
        assert chunks  # it was answering, just never finishing
    # The lock is free, so the next "Set up for me" runs instead of waiting.
    assert not first_run.provisioning_in_progress()


@pytest.mark.anyio
async def test_the_next_run_proceeds_after_a_stalled_one(fresh, monkeypatch):
    monkeypatch.setattr(first_run, "_SIGNIN_NUDGE_DEADLINE", 0.3)
    wire_transport(monkeypatch, _hostile("silent", [], []))
    stalled = asyncio.create_task(first_run.provision_grocy("http://stall.test"))
    await asyncio.sleep(0.05)
    assert first_run.provisioning_in_progress()
    await asyncio.wait_for(stalled, 5)
    wire(monkeypatch, grocy_routes())
    report = await asyncio.wait_for(first_run.provision_grocy(), 5)
    assert report["configured"]


@pytest.mark.anyio
async def test_a_slow_first_migration_inside_the_limits_still_connects(fresh, monkeypatch):
    """A real fresh Grocy that takes its time over GET / is not cut short:
    the run still nudges, signs in again and connects in one go."""
    monkeypatch.setattr(first_run, "_SIGNIN_NUDGE_DEADLINE", 2.0)
    migrated = []

    async def handler(request: httpx.Request) -> httpx.Response:
        route = (request.method, request.url.path)
        if route == ("POST", "/login"):
            if not migrated:
                return httpx.Response(500)
            return httpx.Response(302, headers={
                "location": "/", "set-cookie": "grocy_session_access_token=abc; path=/"})
        if route == ("GET", "/"):
            await asyncio.sleep(0.2)  # building the schema
            migrated.append(True)
            return httpx.Response(302, headers={"location": "/stockoverview"})
        if route == ("GET", "/manageapikeys/new"):
            return httpx.Response(302, headers={"location": "/manageapikeys?key=2"})
        if route == ("GET", "/manageapikeys"):
            return httpx.Response(200, text=GROCY_PAGE)
        if route == ("GET", "/api/system/info"):
            return httpx.Response(200, json={"grocy_version": {"Version": "4.7.1"}})
        if route == ("GET", "/api/users"):
            return httpx.Response(200, json=[{"id": 1, "username": "admin"}])
        if route == ("PUT", "/api/users/1"):
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {route}")

    wire_transport(monkeypatch, handler)
    report = await first_run.provision_grocy("http://grocy.test")
    assert report["configured"] and not report["retryable"]
    assert migrated == [True]
    assert settings.grocy_api_key == (
        "newkeynewkeynewkeynewkeynewkeynewkeynewkeynewkey02")


@pytest.mark.anyio
async def test_the_sign_in_nudge_uses_the_short_limits(fresh, monkeypatch):
    built = wire_transport(monkeypatch, lambda request: httpx.Response(
        302, headers={"location": "/stockoverview"}))
    assert await first_run._nudge_grocy_migrations_safely("http://grocy.test") is True
    assert built[0]["timeout"] == first_run._SIGNIN_NUDGE_TIMEOUT
    assert built[0]["follow_redirects"] is False


@pytest.mark.anyio
async def test_the_appliance_heal_keeps_its_patient_nudge(fresh, monkeypatch):
    # The heal only reaches the device's own Grocy, outside the lock, and a
    # Pi's SD card can take minutes over the first migration.
    built = wire_transport(monkeypatch, lambda request: httpx.Response(
        302, headers={"location": "/stockoverview"}))
    assert await first_run._nudge_grocy_migrations("http://localhost:9383") is True
    assert built[0]["timeout"].read == first_run._MIGRATION_NUDGE_TIMEOUT


def test_the_sign_in_limits_stay_in_the_tens_of_seconds():
    # The lock waits on the whole nudge, so the deadline is what any other
    # setup attempt can be kept waiting for. It still has to leave room for a
    # server-disk first migration, and the heal stays the patient one.
    timeout = first_run._SIGNIN_NUDGE_TIMEOUT
    assert 10 <= timeout.read <= first_run._SIGNIN_NUDGE_DEADLINE <= 60
    assert timeout.connect <= 10
    assert first_run._MIGRATION_NUDGE_TIMEOUT > first_run._SIGNIN_NUDGE_DEADLINE


# -- The whole locked run shares one deadline ---------------------------------
#
# The nudge's deadline covers the nudge alone. Every other request in a run had
# only its read timeout, which measures silence: a server that sends a byte
# every few seconds never trips it, so an address that answered the sign-in
# that way held the provisioning lock, and every other setup attempt with it,
# for as long as it kept going.


def _grocy_answer(request: httpx.Request) -> httpx.Response:
    """A healthy, already-migrated Grocy's answers, for a MockTransport."""
    route = (request.method, request.url.path)
    if route == ("POST", "/login"):
        return httpx.Response(302, headers={
            "location": "/", "set-cookie": "grocy_session_access_token=abc; path=/"})
    if route == ("GET", "/manageapikeys/new"):
        return httpx.Response(302, headers={"location": "/manageapikeys?key=2"})
    if route == ("GET", "/manageapikeys"):
        return httpx.Response(200, text=GROCY_PAGE)
    if route == ("GET", "/api/system/info"):
        return httpx.Response(200, json={"grocy_version": {"Version": "4.7.1"}})
    if route == ("GET", "/api/users"):
        return httpx.Response(200, json=[{"id": 1, "username": "admin"}])
    if route == ("PUT", "/api/users/1"):
        return httpx.Response(204)
    raise AssertionError(f"unexpected request {route}")


def _dripping_server(state: dict):
    """A connection handler for a real local server that answers every request
    with a body sent one byte every 20 ms, far inside any read timeout, that
    never ends. Counts the bytes sent and notes when the client hangs up."""
    async def handle(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
            while not reader.at_eof():
                writer.write(b"<")
                await writer.drain()
                state["bytes"] += 1
                await asyncio.sleep(0.02)
            state["hung_up"] = True
        except ConnectionError:
            state["hung_up"] = True
        finally:
            writer.close()
    return handle


@pytest.mark.anyio
async def test_a_dripping_sign_in_frees_the_lock_within_the_deadline(fresh, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)  # straight to the local server
    monkeypatch.setattr(first_run, "_PROVISION_DEADLINE", 0.5)
    state = {"bytes": 0, "hung_up": False}
    server = await asyncio.start_server(_dripping_server(state), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        started = time.monotonic()
        report = await first_run.provision_grocy(f"http://127.0.0.1:{port}")
        assert time.monotonic() - started < 3
        # It was answering the whole time, just never finishing.
        assert state["bytes"] > 5
        assert report["retryable"] and not report["configured"]
        assert report["steps"] == [{"step": "sign-in", "done": False, "skipped": False,
                                    "reason": "Grocy answered too slowly."}]
        assert "too slowly" in report["message"]
        assert settings.grocy_api_key == ""
        # The lock is free, so the next "Set up for me" runs instead of waiting.
        assert not first_run.provisioning_in_progress()
        # And the connection was dropped, not left dripping in the background.
        for _ in range(100):
            if state["hung_up"]:
                break
            await asyncio.sleep(0.02)
        assert state["hung_up"]
    finally:
        server.close()
        await asyncio.wait_for(server.wait_closed(), 5)


@pytest.mark.anyio
async def test_one_deadline_covers_every_request_in_the_run(fresh, monkeypatch):
    """Answers that each come in good time still cannot add up past the
    deadline: it is one budget for the run, not one per request."""
    monkeypatch.setattr(first_run, "_PROVISION_DEADLINE", 0.7)
    seen = []

    async def unhurried(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        await asyncio.sleep(0.2)
        return _grocy_answer(request)

    wire_transport(monkeypatch, unhurried)
    started = time.monotonic()
    report = await first_run.provision_grocy("http://grocy.test")
    assert time.monotonic() - started < 3
    # A full run is six requests, 1.2 s at this pace: it was cut off partway.
    assert 2 <= len(seen) < 6
    assert report["retryable"] and not report["configured"]
    assert "try again" in report["message"]
    assert settings.grocy_api_key == ""
    assert not first_run.provisioning_in_progress()


@pytest.mark.anyio
async def test_running_out_once_the_key_is_saved_still_reports_connected(
        fresh, monkeypatch):
    """Only the best-effort password step is left by then: the install is
    connected, and the factory sign-in, never changed, is reported as such."""
    monkeypatch.setattr(first_run, "_PROVISION_DEADLINE", 0.5)
    chunks, methods = [], []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == "/api/users":
            return httpx.Response(200, content=_drip(chunks))
        return _grocy_answer(request)

    wire_transport(monkeypatch, handler)
    report = await first_run.provision_grocy("http://grocy.test")
    assert report["ok"] and report["configured"] and not report["retryable"]
    assert settings.grocy_api_key == (
        "newkeynewkeynewkeynewkeynewkeynewkeynewkeynewkey02")
    assert settings.grocy_admin_password == ""
    assert "PUT" not in methods
    assert chunks
    step = next(s for s in report["steps"] if s["step"] == "secure admin sign-in")
    assert step["skipped"] and "admin / admin" in step["reason"]
    assert "still admin / admin" in report["message"]
    assert not first_run.provisioning_in_progress()


@pytest.mark.anyio
async def test_the_password_change_is_never_cut_off_by_the_run_deadline(
        fresh, monkeypatch):
    """Cut off after Grocy applied it, the new password would be stored
    nowhere and the factory one would no longer work. So the change gets its
    own floor and is saved, even with the run's deadline already spent."""
    # Margins a slow CI runner cannot eat: the steps before the change must
    # still fit in the run's deadline, or the run stops before it starts.
    monkeypatch.setattr(first_run, "_PROVISION_DEADLINE", 1.0)
    monkeypatch.setattr(first_run, "_PASSWORD_CHANGE_FLOOR", 5.0)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            await asyncio.sleep(1.5)  # past the run's deadline, inside the floor
        return _grocy_answer(request)

    wire_transport(monkeypatch, handler)
    report = await first_run.provision_grocy("http://grocy.test")
    assert report["ok"] and report["configured"]
    assert settings.grocy_admin_password  # the password Grocy now has is kept
    step = next(s for s in report["steps"] if s["step"] == "secure admin sign-in")
    assert step["done"]
    assert not first_run.provisioning_in_progress()


@pytest.mark.anyio
async def test_the_nudge_never_outlasts_the_run(fresh, monkeypatch):
    """The nudge keeps its own 45 s, but never more than the run has left."""
    monkeypatch.setattr(first_run, "_PROVISION_DEADLINE", 0.3)
    root_hits = []
    wire_transport(monkeypatch, _hostile("silent", root_hits, []))
    started = time.monotonic()
    report = await first_run.provision_grocy("http://stall.test")
    assert time.monotonic() - started < 3
    assert root_hits
    # The sign-in answer it already had, so the appliance heal still sees it.
    assert report["retryable"] and first_run.grocy_signin_answered_5xx(report)
    assert not first_run.provisioning_in_progress()


@pytest.mark.anyio
async def test_a_dripping_mealie_frees_the_lock_too(fresh, monkeypatch):
    monkeypatch.setattr(first_run, "_PROVISION_DEADLINE", 0.3)
    chunks = []
    wire_transport(monkeypatch,
                   lambda request: httpx.Response(200, content=_drip(chunks)))
    started = time.monotonic()
    report = await first_run.provision_mealie("http://mealie.test")
    assert time.monotonic() - started < 3
    assert chunks
    assert report["retryable"] and not report["configured"]
    assert report["steps"][0]["reason"] == "Mealie answered too slowly."
    assert "too slowly" in report["message"]
    assert settings.mealie_api_key == ""
    assert not first_run.provisioning_in_progress()


@pytest.mark.anyio
async def test_a_slow_first_start_still_fits_the_run_deadline(fresh, monkeypatch):
    """The real budget scaled down (45 s nudge, 60 s run): a migration that
    uses most of the nudge's time still leaves the run room to connect."""
    monkeypatch.setattr(first_run, "_SIGNIN_NUDGE_DEADLINE", 0.9)
    monkeypatch.setattr(first_run, "_PROVISION_DEADLINE", 1.2)
    migrated = []

    async def handler(request: httpx.Request) -> httpx.Response:
        route = (request.method, request.url.path)
        if route == ("POST", "/login") and not migrated:
            return httpx.Response(500)
        if route == ("GET", "/"):
            await asyncio.sleep(0.6)  # building the schema
            migrated.append(True)
            return httpx.Response(302, headers={"location": "/stockoverview"})
        return _grocy_answer(request)

    wire_transport(monkeypatch, handler)
    report = await first_run.provision_grocy("http://grocy.test")
    assert report["configured"] and not report["retryable"], report
    assert migrated == [True]
    assert settings.grocy_admin_password


@pytest.mark.anyio
async def test_the_appliance_heal_is_not_held_to_the_run_deadline(
        appliance, monkeypatch):
    """The heal runs outside the lock and keeps its patient nudge, however
    short a provisioning run's deadline is."""
    monkeypatch.setattr(first_run, "_PROVISION_DEADLINE", 0.05)
    hits = []

    async def migrating(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        await asyncio.sleep(0.2)  # well past the run deadline above
        return httpx.Response(302, headers={"location": "/stockoverview"})

    wire_transport(monkeypatch, migrating)
    repairs = record_bridge_repairs(monkeypatch)
    for _ in range(first_run._GROCY_5XX_STREAK_THRESHOLD):
        await first_run._maybe_heal_grocy("http://localhost:9383", _wedge_report())
    assert hits == ["/"]
    assert repairs == []  # the nudge took, so the bridge was never asked


def test_the_run_deadline_fits_a_full_nudge_and_stays_about_a_minute():
    # A fresh Grocy's run is a whole nudge plus a few quick requests, and the
    # deadline is also how long a stalled address can keep setup busy.
    assert first_run._PROVISION_DEADLINE >= first_run._SIGNIN_NUDGE_DEADLINE + 10
    assert first_run._PROVISION_DEADLINE <= 60
    assert first_run._PROVISION_DEADLINE < first_run._MIGRATION_NUDGE_TIMEOUT


# -- A brand-new Grocy's one-shot first-request redirect ----------------------
#
# A factory-fresh Grocy answers its very first request of any kind with a 302
# to its own root and signs nobody in. Taken as a sign-in, it ended the first
# run at "Grocy has not handed back the new API key yet" and the connect came
# about 14 s later, on the next attempt. Now the sign-in is sent again at once.


@pytest.mark.anyio
async def test_a_brand_new_grocy_connects_in_one_run(fresh, heal_state, monkeypatch):
    base = "http://grocy:80"
    monkeypatch.setattr(settings, "grocy_base_url", base, raising=False)
    calls = wire(monkeypatch, brand_new_grocy(base))
    report = await first_run.provision_grocy()
    assert report["configured"] and not report["retryable"], report
    assert settings.grocy_api_key == (
        "newkeynewkeynewkeynewkeynewkeynewkeynewkeynewkey02")
    # The one-shot redirect, the 500 over the empty database, then the real
    # sign-in once the nudge has built it.
    assert len(_requests(calls, "POST", f"{base}/login")) == 3
    assert len(_requests(calls, "GET", f"{base}/")) == 1
    assert not [s for s in report["steps"] if s["reason"]]


@pytest.mark.anyio
async def test_the_one_shot_redirect_alone_costs_one_more_sign_in(fresh, monkeypatch):
    # A Grocy whose database already exists (the one-shot redirect also comes
    # after a Grocy update): one more sign-in, no nudge.
    base = "http://grocy.test"
    calls = wire(monkeypatch, brand_new_grocy(base, grocy_routes(base)))
    report = await first_run.provision_grocy()
    assert report["configured"]
    assert len(_requests(calls, "POST", f"{base}/login")) == 2
    assert not _requests(calls, "GET", f"{base}/")


@pytest.mark.anyio
async def test_a_root_redirect_that_never_signs_in_is_retried_only_once(fresh, monkeypatch):
    # Something that answers every sign-in with a cookie-less redirect to its
    # root (a Grocy that cannot write its view cache redirects every request).
    # One more sign-in, then the run goes on as before and stays retryable.
    base = "http://grocy.test"
    calls = wire(monkeypatch, {
        ("POST", f"{base}/login"): one_shot_redirect(base),
        ("GET", f"{base}/manageapikeys/new"): FakeResponse(
            302, headers={"location": "/login"}),
        ("GET", f"{base}/manageapikeys"): FakeResponse(200, text="<html>Sign in</html>"),
    })
    report = await first_run.provision_grocy()
    assert report["retryable"] and not report["configured"]
    assert len(_requests(calls, "POST", f"{base}/login")) == 2


@pytest.mark.anyio
async def test_the_startup_loop_connects_a_brand_new_grocy_on_its_first_attempt(
        fresh, heal_state, monkeypatch):
    from app.services import readiness
    released = []
    monkeypatch.setattr(readiness, "mark_provisioning_done",
                        lambda: released.append(True))
    base = "http://grocy.test"
    wire(monkeypatch, brand_new_grocy(base))

    async def no_mealie(base=""):
        return {"ok": False, "configured": False, "retryable": True}

    monkeypatch.setattr(first_run, "provision_mealie", no_mealie)
    await first_run.startup_first_run(attempts=1, delay=0, initial_delay=0)
    assert settings.grocy_api_key
    assert released  # the getting-ready page lets the user through at once


@pytest.mark.anyio
async def test_a_brand_new_grocy_signs_in_over_a_real_client(fresh, monkeypatch):
    """The same start over httpx itself, so the session cookie is read from a
    real Set-Cookie header and the key page is reached with it."""
    state = {"served": False, "migrated": False}
    key_page_cookies = []

    async def handler(request: httpx.Request) -> httpx.Response:
        route = (request.method, request.url.path)
        if not state["served"]:
            state["served"] = True
            return httpx.Response(302, headers={"location": "http://grocy.test/"})
        if route == ("POST", "/login"):
            if not state["migrated"]:
                return httpx.Response(500, text="no such table: migrations")
            return httpx.Response(302, headers={
                "location": "http://grocy.test/",
                "set-cookie": "grocy_session_access_token=f00d; path=/; httponly"})
        if route == ("GET", "/"):
            state["migrated"] = True
            return httpx.Response(302, headers={"location": "http://grocy.test/stockoverview"})
        if route == ("GET", "/manageapikeys/new"):
            key_page_cookies.append(request.headers.get("cookie", ""))
            return httpx.Response(302, headers={"location": "/manageapikeys?key=2"})
        if route == ("GET", "/manageapikeys"):
            return httpx.Response(200, text=GROCY_PAGE)
        if route == ("GET", "/api/system/info"):
            return httpx.Response(200, json={"grocy_version": {"Version": "4.7.1"}})
        if route == ("GET", "/api/users"):
            return httpx.Response(200, json=[{"id": 1, "username": "admin"}])
        if route == ("PUT", "/api/users/1"):
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {route}")

    wire_transport(monkeypatch, handler)
    report = await first_run.provision_grocy("http://grocy.test")
    assert report["configured"] and not report["retryable"], report
    assert key_page_cookies == ["grocy_session_access_token=f00d"]


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
