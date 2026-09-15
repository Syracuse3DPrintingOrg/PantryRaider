"""Honest errors when Grocy answers with a web page instead of JSON.

The 2026-09-06 incident: the Grocy image updated to 4.7.0, which moved its
auth middlewares into a Grocy\\Middleware\\Auth sub-namespace. The persisted
config.php still named the old class, and Grocy answered EVERY API call with
HTTP 200 and a text/html "Invalid setting in config.php" page. The client
raised JSONDecodeError and the UI called it a passing outage. These tests pin
the replacement: Grocy's own words, the fix hint, a structured 502, honest
banners on both pages, the /health detail, and the setup proxy route.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import grocy as grocy_svc  # noqa: E402
from app.services.grocy import (  # noqa: E402
    AUTH_CLASS_HINT,
    AUTH_CLASS_NEW_VALUE,
    NOT_DATA_MESSAGE,
    GrocyClient,
    GrocyError,
    describe_grocy_html_error,
    error_payload,
    html_to_text,
    known_grocy_fix_hint,
)

# Captured at import (collection) time, before any other test file can swap
# the method on the class, so the /health test below runs the real check.
_REAL_HEALTH_CHECK = GrocyClient.health_check

GROCY_AUTH_PAGE = (
    'Invalid setting in config.php: Configured AUTH_CLASS '
    '"Grocy\\Middleware\\DefaultAuthMiddleware" does not exist<br><br>'
    '----------<br>Check your "config.php" file (which is in your data '
    'directory: "/config/data") and compare it with "config-dist.php" to '
    'see what is wrong'
)
GROCY_OTHER_PAGE = (
    'Invalid setting in config.php: Configured MODE "banana" is not valid'
    '<br><br>----------<br>Check your "config.php" file'
)
PHP_FATAL_PAGE = (
    '<br />\n<b>Fatal error</b>:  Uncaught Error: Class "Foo" not found in '
    '/app/www/app.php:12<br />\nStack trace:<br />#0 {main}'
)
LOGIN_PAGE = (
    '<html><head><title>Sign in to grocy.example.com</title></head><body>'
    '<form method="post"><input name="user"><input type="password" name="pw">'
    '<button>Sign in</button></form></body></html>'
)
NGINX_404 = (
    '<html><head><title>404 Not Found</title></head><body><center>'
    '<h1>404 Not Found</h1></center><hr><center>nginx</center></body></html>'
)


# --- describe_grocy_html_error: truth table ----------------------------------

def test_describe_turns_the_auth_class_page_into_plain_sentences():
    msg = describe_grocy_html_error(GROCY_AUTH_PAGE, "text/html; charset=utf-8")
    assert msg is not None
    assert 'Configured AUTH_CLASS "Grocy\\Middleware\\DefaultAuthMiddleware" does not exist' in msg
    assert "<" not in msg and "br>" not in msg
    assert "----------" not in msg
    assert "config.php" in msg


def test_describe_keeps_a_php_fatal_error_without_markup():
    msg = describe_grocy_html_error(PHP_FATAL_PAGE, "text/html")
    assert msg and "Fatal error" in msg and "Uncaught Error" in msg
    assert "<b>" not in msg and "<br" not in msg


def test_describe_returns_none_for_a_reverse_proxy_login_page():
    # A page with a password field is somebody's login screen, even when the
    # hostname in its title says "grocy".
    assert describe_grocy_html_error(LOGIN_PAGE, "text/html") is None


def test_describe_returns_none_for_a_plain_web_server_error_page():
    assert describe_grocy_html_error(NGINX_404, "text/html") is None


@pytest.mark.parametrize("body", ["", "   \n\t ", None])
def test_describe_returns_none_for_an_empty_body(body):
    assert describe_grocy_html_error(body, "text/html") is None


def test_describe_leaves_json_alone():
    assert describe_grocy_html_error('{"config.php": "grocy"}', "application/json") is None


def test_describe_trims_long_pages_at_a_word_boundary():
    long_page = GROCY_AUTH_PAGE + " more words about the problem" * 40
    msg = describe_grocy_html_error(long_page, "text/html")
    assert msg.endswith("...")
    assert len(msg) <= 304
    assert not msg[:-3].endswith(" ")


def test_html_to_text_decodes_entities_and_drops_separators():
    text = html_to_text("a &amp; b<br>----------<br><p>c &lt;d&gt;</p>")
    assert text == "a & b c <d>"


# --- known_grocy_fix_hint: truth table ---------------------------------------

def test_hint_recognises_the_auth_class_namespace_move():
    msg = describe_grocy_html_error(GROCY_AUTH_PAGE, "text/html")
    assert known_grocy_fix_hint(msg) == AUTH_CLASS_HINT
    assert AUTH_CLASS_NEW_VALUE in AUTH_CLASS_HINT
    assert "back up" in AUTH_CLASS_HINT.lower()
    assert "config.php" in AUTH_CLASS_HINT


def test_hint_recognises_json_escaped_backslashes():
    escaped = 'Configured AUTH_CLASS "Grocy\\\\Middleware\\\\DefaultAuthMiddleware" does not exist'
    assert known_grocy_fix_hint(escaped) == AUTH_CLASS_HINT


def test_hint_is_none_for_another_config_error():
    msg = describe_grocy_html_error(GROCY_OTHER_PAGE, "text/html")
    assert msg is not None
    assert known_grocy_fix_hint(msg) is None


def test_hint_is_none_when_the_new_class_is_already_named():
    msg = ('Invalid setting in config.php: Configured AUTH_CLASS '
           '"Grocy\\Middleware\\Auth\\DefaultAuthMiddleware" does not exist')
    assert known_grocy_fix_hint(msg) is None


@pytest.mark.parametrize("msg", ["", None, "AUTH_CLASS is fine", "DefaultAuthMiddleware"])
def test_hint_is_none_without_both_markers(msg):
    assert known_grocy_fix_hint(msg) is None


def test_user_facing_copy_has_no_em_dashes():
    for text in (AUTH_CLASS_HINT, NOT_DATA_MESSAGE, grocy_svc.config_error_message("x")):
        assert "\u2014" not in text


# --- GrocyClient._request on a non-JSON answer -------------------------------

class _CannedClient:
    """Stands in for the shared httpx.AsyncClient: one fixed answer."""

    def __init__(self, status=200, body="", content_type="text/html"):
        self.status = status
        self.body = body
        self.content_type = content_type

    async def request(self, method, url, headers=None, json=None):
        return httpx.Response(
            self.status, content=self.body.encode(),
            headers={"content-type": self.content_type},
            request=httpx.Request(method, url),
        )


@pytest.fixture
def canned(monkeypatch):
    def _install(status=200, body="", content_type="text/html"):
        monkeypatch.setattr(grocy_svc, "_client",
                            _CannedClient(status, body, content_type))
    return _install


@pytest.mark.anyio
async def test_request_raises_grocy_error_with_hint_on_html_200(canned):
    canned(200, GROCY_AUTH_PAGE, "text/html; charset=utf-8")
    with pytest.raises(GrocyError) as info:
        await GrocyClient().get_stock()
    err = info.value
    assert err.kind == "config"
    assert err.hint == AUTH_CLASS_HINT
    assert "reported a problem with its own setup" in str(err)
    assert "AUTH_CLASS" in str(err)
    assert "<br" not in str(err)


@pytest.mark.anyio
async def test_request_on_a_login_page_says_not_data_without_a_hint(canned):
    canned(200, LOGIN_PAGE, "text/html")
    with pytest.raises(GrocyError) as info:
        await GrocyClient().get_stock()
    assert info.value.kind == "bad_response"
    assert str(info.value) == NOT_DATA_MESSAGE
    assert info.value.hint is None


@pytest.mark.anyio
async def test_request_passes_json_through_untouched(canned):
    canned(200, '[{"product_id": 1, "amount": 2}]', "application/json")
    assert await GrocyClient().get_stock() == [{"product_id": 1, "amount": 2}]


@pytest.mark.anyio
async def test_request_empty_body_is_an_empty_dict(canned):
    canned(200, "", "application/json")
    assert await GrocyClient()._get("/x") == {}


@pytest.mark.anyio
async def test_request_error_status_with_html_never_leaks_markup(canned):
    canned(500, "<html><body><h1>Fatal error</h1><p>Uncaught Error: boom</p></body></html>")
    with pytest.raises(GrocyError) as info:
        await GrocyClient().get_stock()
    assert info.value.kind == "http"
    assert str(info.value).startswith("Grocy 500 on /stock:")
    assert "Uncaught Error: boom" in str(info.value)
    assert "<" not in str(info.value)


@pytest.mark.anyio
async def test_request_error_status_json_keeps_grocys_words(canned):
    canned(400, '{"error_message": "Entity does not exist or is not exposed"}', "application/json")
    with pytest.raises(GrocyError) as info:
        await GrocyClient().get_stock()
    assert "Entity does not exist or is not exposed" in str(info.value)
    assert info.value.kind == "http"


@pytest.mark.anyio
async def test_connect_error_is_kind_unreachable(monkeypatch):
    class _Dead:
        async def request(self, *a, **k):
            raise httpx.ConnectError("boom")
    monkeypatch.setattr(grocy_svc, "_client", _Dead())
    with pytest.raises(GrocyError) as info:
        await GrocyClient().get_stock()
    assert info.value.kind == "unreachable"
    assert "not reachable" in str(info.value)


# --- error_payload ------------------------------------------------------------

def _config_error():
    return GrocyError(grocy_svc.config_error_message("Invalid setting"),
                      hint=AUTH_CLASS_HINT, kind="config")


def test_error_payload_plain_outage_is_just_the_detail():
    payload = error_payload(GrocyError("Grocy is not reachable.", kind="unreachable"))
    assert payload == {"detail": "Grocy is not reachable."}


def test_error_payload_config_error_carries_hint_and_repairable(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    payload = error_payload(_config_error())
    assert payload["config_error"] is True
    assert payload["hint"] == AUTH_CLASS_HINT
    assert payload["repairable"] is False
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    assert error_payload(_config_error())["repairable"] is True
    # A satellite's Grocy lives on its main server: nothing to repair here.
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    assert error_payload(_config_error())["repairable"] is False


# --- Routes and pages ---------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(type(settings), "is_configured", lambda self: True,
                        raising=False)
    # A Pi appliance also has the first-boot readiness gate, which would
    # redirect every page to /ui/getting-ready; this install is long past it.
    from app.services import readiness
    monkeypatch.setattr(readiness, "gate_possible", lambda: False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


@pytest.fixture
def grocy_config_broken(canned):
    canned(200, GROCY_AUTH_PAGE, "text/html; charset=utf-8")


@pytest.fixture
def grocy_down(monkeypatch):
    class _Dead:
        async def request(self, *a, **k):
            raise httpx.ConnectError("All connection attempts failed")
    monkeypatch.setattr(grocy_svc, "_client", _Dead())


@pytest.mark.parametrize("path", ["/inventory/dashboard", "/inventory/stock",
                                  "/expiring/", "/expiring/summary"])
def test_grocy_json_routes_answer_a_structured_502(client, grocy_config_broken, path):
    r = client.get(path)
    assert r.status_code == 502
    body = r.json()
    assert "reported a problem with its own setup" in body["detail"]
    assert body["config_error"] is True
    assert body["hint"] == AUTH_CLASS_HINT
    assert body["repairable"] is False
    assert "<" not in body["detail"]


def test_dashboard_502_offers_repair_on_a_pi_hosted_appliance(client, grocy_config_broken, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    body = client.get("/inventory/dashboard").json()
    assert body["repairable"] is True


def test_dashboard_502_for_a_plain_outage_keeps_the_old_shape(client, grocy_down):
    r = client.get("/inventory/dashboard")
    assert r.status_code == 502
    body = r.json()
    assert "not reachable" in body["detail"]
    assert "hint" not in body and "config_error" not in body


def test_expiring_page_renders_grocys_words_and_the_hint(client, grocy_config_broken):
    r = client.get("/ui/expiring")
    assert r.status_code == 200
    html = r.text
    assert "reported a problem with its own setup" in html
    assert "AUTH_CLASS" in html
    assert "Grocy 4.7 moved its login handler" in html
    # Not the outage copy, and no repair button on a server install.
    assert "Inventory will return when it is" not in html
    assert "Repair Grocy config" not in html


def test_expiring_page_offers_repair_on_a_pi_hosted_appliance(client, grocy_config_broken, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    html = client.get("/ui/expiring").text
    assert "Repair Grocy config" in html
    assert "/setup/grocy/repair" in html or "grocy-repair.js" in html


def test_expiring_page_keeps_the_outage_copy_for_a_dead_grocy(client, grocy_down):
    html = client.get("/ui/expiring").text
    assert "Grocy is not reachable. Inventory will return when it is." in html
    assert "Repair Grocy config" not in html
    assert "own setup" not in html


def test_expiring_page_escapes_a_hostile_grocy_message(client, canned):
    canned(200, 'Invalid setting in config.php: <script>alert(1)</script> &lt;b&gt;x', "text/html")
    html = client.get("/ui/expiring").text
    assert "<script>alert(1)</script>" not in html
    assert "Invalid setting in config.php" in html


def test_inventory_page_carries_the_hint_slot_and_repair_button(client):
    html = client.get("/ui/inventory").text
    assert 'id="inventory-status-hint"' in html
    assert 'id="inventory-repair"' in html
    assert "grocy-repair.js" in html


# --- /health detail -----------------------------------------------------------

@pytest.fixture
def real_health_check(monkeypatch):
    monkeypatch.setattr(GrocyClient, "health_check", _REAL_HEALTH_CHECK)


def test_health_carries_the_grocy_detail_when_in_error(client, grocy_config_broken, real_health_check):
    body = client.get("/health").json()
    assert body["grocy"] == "error"
    assert "own setup" in body["grocy_detail"]
    assert "AUTH_CLASS" in body["grocy_detail"]
    assert body["grocy_hint"] == AUTH_CLASS_HINT
    assert len(body["grocy_detail"]) <= 200


def test_health_has_no_detail_when_grocy_is_fine(client, canned, real_health_check):
    canned(200, '{"grocy_version": {"Version": "4.6.0"}}', "application/json")
    body = client.get("/health").json()
    assert body["grocy"] == "ok"
    assert "grocy_detail" not in body and "grocy_hint" not in body


def test_health_detail_for_a_dead_grocy_is_the_outage_copy(client, grocy_down, real_health_check):
    body = client.get("/health").json()
    assert body["grocy"] == "error"
    assert "not reachable" in body["grocy_detail"]
    assert "grocy_hint" not in body


# --- POST /setup/grocy/repair -------------------------------------------------

class _FakeBridgeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.content = b"x"

    def json(self):
        return self._payload


class _FakeBridgeClient:
    """Stands in for services.bridge.bridge_client: records the URL posted."""
    posted: list = []
    reply: dict = {}
    raise_exc: Exception | None = None

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        _FakeBridgeClient.posted.append(url)
        if _FakeBridgeClient.raise_exc:
            raise _FakeBridgeClient.raise_exc
        return _FakeBridgeResponse(_FakeBridgeClient.reply)


@pytest.fixture
def fake_bridge(monkeypatch):
    import app.routers.setup as setup_router
    _FakeBridgeClient.posted = []
    _FakeBridgeClient.reply = {}
    _FakeBridgeClient.raise_exc = None
    monkeypatch.setattr(setup_router, "bridge_client", _FakeBridgeClient)
    monkeypatch.setattr(setup_router, "_GROCY_REPAIR_PROBE_DELAY", 0)
    return _FakeBridgeClient


def test_setup_repair_refuses_off_an_appliance_with_the_hand_edit(client, fake_bridge):
    r = client.post("/setup/grocy/repair")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["action"] == "refused"
    assert AUTH_CLASS_NEW_VALUE in body["reason"]
    assert fake_bridge.posted == []


def test_setup_repair_proxies_the_bridge_and_reprobes(client, fake_bridge, canned, real_health_check, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    fake_bridge.reply = {"ok": True, "action": "repaired",
                         "reason": "Changed AUTH_CLASS.", "backup": "/config/data/config.php.bak-1"}
    canned(200, '{"grocy_version": {"Version": "4.7.0"}}', "application/json")
    body = client.post("/setup/grocy/repair").json()
    assert fake_bridge.posted == ["http://127.0.0.1:9299/grocy/repair-config"]
    assert body["ok"] is True and body["action"] == "repaired"
    assert body["grocy_ok"] is True
    assert body["backup"].endswith(".bak-1")


def test_setup_repair_reports_grocy_still_broken(client, fake_bridge, grocy_config_broken, real_health_check, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    fake_bridge.reply = {"ok": True, "action": "skipped", "reason": "nothing to repair"}
    body = client.post("/setup/grocy/repair").json()
    assert body["ok"] is True and body["grocy_ok"] is False
    assert "AUTH_CLASS" in body["grocy_detail"]


def test_setup_repair_when_the_bridge_is_unreachable(client, fake_bridge, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    fake_bridge.raise_exc = httpx.ConnectError("refused")
    body = client.post("/setup/grocy/repair").json()
    assert body["ok"] is False and body["action"] == "failed"
    assert "device helper" in body["reason"]
    assert AUTH_CLASS_NEW_VALUE in body["reason"]
