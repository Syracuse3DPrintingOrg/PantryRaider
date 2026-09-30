"""The Settings connection tests answer honestly about what they reached.

Each test calls the route function directly with a mocked HTTP transport, so
nothing touches the network. The probe guard that refuses caller-supplied
addresses before setup is patched out: these tests are about what happens
after a request is allowed.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

_cwd = os.getcwd()
os.chdir(SERVICE)
try:
    import app.routers.setup as srouter  # noqa: E402
    from app.config import settings  # noqa: E402
finally:
    os.chdir(_cwd)

_RealAsyncClient = httpx.AsyncClient


def _mock_http(monkeypatch, handler):
    """Route every httpx.AsyncClient the router opens through handler, and
    record each request so a test can check where it went."""
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def _factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(transport=httpx.MockTransport(_record), **kwargs)

    monkeypatch.setattr(srouter.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(srouter, "_refuse_probe_target", lambda url: "")
    return seen


def _run(coro):
    return asyncio.run(coro)


def _body(result):
    """Route functions return either a dict or a JSONResponse."""
    if isinstance(result, dict):
        return result
    import json
    return json.loads(result.body)


_LOGIN_PAGE = ("<!doctype html><html><head><title>Sign in</title></head>"
               "<body><h1>Welcome to my router</h1></body></html>")


# -- Grocy -------------------------------------------------------------------

def _grocy(payload_url="http://grocy.test:9383"):
    return srouter.TestGrocyPayload(grocy_base_url=payload_url,
                                    grocy_api_key="k")


def test_grocy_web_page_is_a_clear_error_not_a_crash(monkeypatch):
    # An HTTP 200 web page used to raise JSONDecodeError out of the handler.
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, text=_LOGIN_PAGE, headers={"content-type": "text/html"}))
    out = _body(_run(srouter.test_grocy(_grocy())))
    assert out["ok"] is False
    assert "web page instead of data" in out["error"]
    # Never the upstream body.
    assert "router" not in out["error"]


def test_grocy_web_page_with_old_auth_class_gets_the_fix_hint(monkeypatch):
    page = ("<html><body>Error: AUTH_CLASS "
            "Grocy\\Middleware\\DefaultAuthMiddleware not found</body></html>")
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, text=page, headers={"content-type": "text/html"}))
    from app.services.grocy import known_grocy_fix_hint
    hint = known_grocy_fix_hint(page)
    assert hint
    out = _body(_run(srouter.test_grocy(_grocy())))
    assert out["ok"] is False
    assert "web page instead of data" in out["error"]
    assert hint in out["error"]


def test_grocy_version_dict_shows_the_version_number(monkeypatch):
    _mock_http(monkeypatch, lambda req: httpx.Response(200, json={
        "grocy_version": {"Version": "4.5.0", "ReleaseDate": "2025-03-01"}}))
    out = _body(_run(srouter.test_grocy(_grocy())))
    assert out["ok"] is True
    assert out["message"] == "Connected: Grocy 4.5.0"


def test_grocy_version_string_still_works(monkeypatch):
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, json={"grocy_version": "3.3.2"}))
    out = _body(_run(srouter.test_grocy(_grocy())))
    assert out == {"ok": True, "message": "Connected: Grocy 3.3.2"}


# -- Mealie ------------------------------------------------------------------

def test_mealie_web_page_names_the_address_problem(monkeypatch):
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, text=_LOGIN_PAGE, headers={"content-type": "text/html"}))
    payload = srouter.TestMealiePayload(mealie_base_url="http://mealie.test/api",
                                        mealie_api_key="tok")
    out = _body(_run(srouter.test_mealie(payload)))
    assert out["ok"] is False
    assert out["error"].startswith("Mealie answered with a web page instead of data.")
    assert "no /api or other path" in out["error"]
    assert "router" not in out["error"]


def test_mealie_json_still_connects(monkeypatch):
    _mock_http(monkeypatch, lambda req: httpx.Response(
        200, json={"username": "chef"}))
    payload = srouter.TestMealiePayload(mealie_base_url="http://mealie.test",
                                        mealie_api_key="tok")
    out = _body(_run(srouter.test_mealie(payload)))
    assert out == {"ok": True, "message": "Connected: authenticated as chef"}


# -- Ollama ------------------------------------------------------------------

def _tags(*names):
    return lambda req: httpx.Response(
        200, json={"models": [{"name": n} for n in names]})


def test_ollama_missing_model_is_not_a_success(monkeypatch):
    _mock_http(monkeypatch, _tags("llama3.2:3b"))
    monkeypatch.setattr(settings, "deployment_mode", "server")
    payload = srouter.TestProviderPayload(provider="ollama", model="llava:7b",
                                          base_url="http://ollama.test:11434")
    out = _body(_run(srouter.test_provider(payload)))
    assert out["ok"] is False
    assert out["error"] == (
        "Connected, but the model llava:7b is not installed. Pull it with: "
        "docker exec foodassistant-ollama ollama pull llava:7b")


def test_ollama_no_models_at_all_is_not_a_success(monkeypatch):
    _mock_http(monkeypatch, _tags())
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "ollama_model", "llava:7b")
    payload = srouter.TestProviderPayload(provider="ollama",
                                          base_url="http://ollama.test:11434")
    out = _body(_run(srouter.test_provider(payload)))
    assert out["ok"] is False and "llava:7b is not installed" in out["error"]


def test_ollama_present_model_connects(monkeypatch):
    _mock_http(monkeypatch, _tags("llava:7b", "llama3.2:3b"))
    monkeypatch.setattr(settings, "deployment_mode", "server")
    payload = srouter.TestProviderPayload(provider="ollama", model="llava:7b",
                                          base_url="http://ollama.test:11434")
    out = _body(_run(srouter.test_provider(payload)))
    assert out["ok"] is True
    assert "llava:7b" in out["message"]


def test_ollama_model_without_a_tag_matches_latest(monkeypatch):
    _mock_http(monkeypatch, _tags("moondream:latest"))
    monkeypatch.setattr(settings, "deployment_mode", "server")
    payload = srouter.TestProviderPayload(provider="ollama", model="moondream",
                                          base_url="http://ollama.test:11434")
    out = _body(_run(srouter.test_provider(payload)))
    assert out["ok"] is True


@pytest.mark.parametrize("entered", ["", "http://ollama:11434", "http://ollama:11434/"])
def test_ollama_on_pi_hosted_tests_localhost(monkeypatch, entered):
    # The appliance app is host-networked, so the compose service name does
    # not resolve there; the test must look where the app will really look.
    seen = _mock_http(monkeypatch, _tags("llava:7b"))
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted")
    monkeypatch.setattr(settings, "ollama_base_url", "http://ollama:11434")
    payload = srouter.TestProviderPayload(provider="ollama", model="llava:7b",
                                          base_url=entered)
    out = _body(_run(srouter.test_provider(payload)))
    assert out["ok"] is True
    assert str(seen[0].url) == "http://localhost:11434/api/tags"


def test_ollama_on_a_server_keeps_the_service_name(monkeypatch):
    seen = _mock_http(monkeypatch, _tags("llava:7b"))
    monkeypatch.setattr(settings, "deployment_mode", "server")
    payload = srouter.TestProviderPayload(provider="ollama", model="llava:7b",
                                          base_url="http://ollama:11434")
    _run(srouter.test_provider(payload))
    assert str(seen[0].url) == "http://ollama:11434/api/tags"


@pytest.mark.parametrize("mode", ["server", "pi_hosted", "pi_remote"])
@pytest.mark.parametrize("saved", ["", "http://ollama:11434", "http://ollama:11434/",
                                   "http://ollama.test:11434"])
def test_ollama_test_looks_where_scans_look(monkeypatch, mode, saved):
    # The Test button must probe the address scans will use, or it can pass
    # while every scan fails (or the other way round).
    from app import dependencies
    seen = _mock_http(monkeypatch, _tags("llava:7b"))
    monkeypatch.setattr(settings, "deployment_mode", mode)
    monkeypatch.setattr(settings, "ollama_base_url", saved)
    payload = srouter.TestProviderPayload(provider="ollama", model="llava:7b",
                                          base_url="")
    _run(srouter.test_provider(payload))
    expected = dependencies.ollama_base_url().rstrip("/") + "/api/tags"
    assert str(seen[0].url) == expected


# -- Forager: a disabled account is not a Premium upsell ---------------------

_DISABLED = {"detail": {"error": "account_disabled",
                        "message": "This account is paused. Email help@example.test."}}


@pytest.fixture
def linked(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "cloud_base_url", "https://forager.test")
    monkeypatch.setattr(settings, "cloud_instance_token", "inst-token")
    monkeypatch.setattr(type(settings), "cloud_linked", lambda self: True)


def test_backup_status_shows_the_disabled_message(monkeypatch, linked):
    _mock_http(monkeypatch, lambda req: httpx.Response(403, json=_DISABLED))
    out = _body(_run(srouter.cloud_backup_status()))
    assert out["premium"] is False
    assert out["disabled"] is True
    assert out["error"] == "This account is paused. Email help@example.test."


def test_backup_upload_shows_the_disabled_message(monkeypatch, linked):
    _mock_http(monkeypatch, lambda req: httpx.Response(403, json=_DISABLED))
    out = _body(_run(srouter.cloud_backup_upload()))
    assert out["ok"] is False
    assert out["error"] == "This account is paused. Email help@example.test."
    assert "Premium" not in out["error"]


def test_backup_restore_shows_the_disabled_message(monkeypatch, linked):
    monkeypatch.setattr(settings, "auth_password", "")
    _mock_http(monkeypatch, lambda req: httpx.Response(403, json=_DISABLED))
    out = _body(_run(srouter.cloud_backup_restore(srouter.CloudRestorePayload())))
    assert out["ok"] is False
    assert out["error"] == "This account is paused. Email help@example.test."


def test_disabled_message_is_plain_text(monkeypatch, linked):
    body = {"detail": {"error": "account_disabled",
                       "message": "<img src=x onerror=alert(1)>Paused."}}
    _mock_http(monkeypatch, lambda req: httpx.Response(403, json=body))
    out = _body(_run(srouter.cloud_backup_upload()))
    assert "<" not in out["error"] and ">" not in out["error"]
    assert "Paused." in out["error"]


def test_disabled_without_a_message_still_explains(monkeypatch, linked):
    _mock_http(monkeypatch, lambda req: httpx.Response(
        403, json={"detail": {"error": "account_disabled"}}))
    out = _body(_run(srouter.cloud_backup_upload()))
    assert "disabled" in out["error"].lower()
    assert "Premium" not in out["error"]


def test_plain_402_is_still_the_premium_message(monkeypatch, linked):
    _mock_http(monkeypatch, lambda req: httpx.Response(
        402, json={"detail": "Premium required"}))
    out = _body(_run(srouter.cloud_backup_upload()))
    assert "Premium" in out["error"]


def test_tunnel_enable_shows_the_disabled_message(monkeypatch, linked):
    monkeypatch.setattr(settings, "auth_password", "set")
    monkeypatch.setattr(srouter, "_tunnel_backend", lambda: "local")
    monkeypatch.setattr(type(settings), "local_2fa_active", lambda self: True)
    monkeypatch.setattr(srouter.tunnel_local, "keygen", lambda: "PUBKEY")
    _mock_http(monkeypatch, lambda req: httpx.Response(403, json=_DISABLED))
    out = _body(_run(srouter.tunnel_enable(srouter.TunnelEnablePayload())))
    assert out["ok"] is False
    assert out["error"] == "This account is paused. Email help@example.test."
    assert not out.get("needs_plan")


def test_subdomain_check_shows_the_disabled_message(monkeypatch, linked):
    _mock_http(monkeypatch, lambda req: httpx.Response(403, json=_DISABLED))
    out = _body(_run(srouter.tunnel_subdomain_available("kitchen")))
    assert out["ok"] is False
    assert out["error"] == "This account is paused. Email help@example.test."
    assert "status 403" not in out["error"]


_PANEL_HARNESS = r"""
const els = {};
function el(id) {
  if (!els[id]) {
    const cls = new Set(['d-none']);
    els[id] = {id, textContent: '', innerHTML: '', disabled: false,
               classList: {add: c => cls.add(c), remove: c => cls.delete(c),
                           contains: c => cls.has(c)}};
  }
  return els[id];
}
const document = {getElementById: el, addEventListener() {}};
const fetch = async () => ({json: async () => (STATUS)});
__FN__
loadForagerBackup().then(() => {
  const out = {};
  for (const id of Object.keys(els)) {
    out[id] = {hidden: els[id].classList.contains('d-none'),
               text: els[id].textContent, html: els[id].innerHTML,
               disabled: els[id].disabled};
  }
  console.log(JSON.stringify(out));
});
"""


def _run_backup_panel(status: dict) -> dict:
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    from test_recipe_output_escaping import extract_function
    src = (SERVICE / "app" / "static" / "js" / "setup" / "panes.js").read_text()
    script = (_PANEL_HARNESS.replace("__FN__", extract_function(src, "loadForagerBackup"))
              .replace("STATUS", json.dumps(status)))
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_backup_panel_shows_a_disabled_account_as_text():
    # The status route now says when the account is disabled; the panel must
    # show that message (as text, never markup) instead of hiding itself.
    msg = "This account is paused. <b>Email</b> help@example.test."
    els = _run_backup_panel({"ok": True, "linked": True, "premium": False,
                             "disabled": True, "backups": [], "error": msg})
    assert els["forager-backup-panel"]["hidden"] is False
    assert els["forager-backup-status"]["text"] == msg
    assert els["forager-backup-status"]["html"] == ""
    assert els["forager-backup-btn"]["disabled"] is True


def test_backup_panel_still_hides_for_a_plain_non_premium_account():
    els = _run_backup_panel({"ok": True, "linked": True, "premium": False,
                             "backups": []})
    assert els["forager-backup-panel"]["hidden"] is True


def test_settings_results_show_server_messages_as_text():
    # setResult carries server and upstream error text onto the page, so it
    # must build its line from text nodes, never from markup.
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    from test_recipe_output_escaping import extract_function
    src = (SERVICE / "app" / "static" / "js" / "setup" / "helpers.js").read_text()
    fn = extract_function(src, "setResult")
    assert "innerHTML" not in fn
    script = r"""
function mk(tag) { return {tag, className: '', children: [], textContent: '',
  appendChild(c) { this.children.push(c); return c; }}; }
const target = mk('div');
const document = {getElementById: () => target, createElement: mk,
                  createTextNode: t => ({text: t})};
""" + fn + r"""
setResult('x', false, '<img src=x onerror=alert(1)>');
const span = target.children[0];
console.log(JSON.stringify({cls: span.className, text: span.children[1].text}));
"""
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got == {"cls": "text-danger", "text": "<img src=x onerror=alert(1)>"}
