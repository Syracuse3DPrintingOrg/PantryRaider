"""Fleet-wide auto-update flag and the Pi update decision (FoodAssistant-k2kk)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import auto_update as au  # noqa: E402
from app.config import settings, SATELLITE_PULL_FIELDS, _SAVEABLE  # noqa: E402


def test_pi_hosted_always_attempts():
    # Not a satellite: attempt regardless of any server version (OTA no-ops when
    # already current).
    assert au.should_run(False, "0.6.10", "") is True
    assert au.should_run(False, "0.6.10", "0.6.10") is True


def test_satellite_only_updates_when_behind_known_server():
    # Server version unknown yet: do nothing.
    assert au.should_run(True, "0.6.10", "") is False
    # Same version: nothing to do.
    assert au.should_run(True, "0.6.12", "0.6.12") is False
    # Different version: converge on the server.
    assert au.should_run(True, "0.6.10", "0.6.12") is True


def test_flag_defaults_on_and_is_global():
    assert settings.auto_update is True              # on by default
    assert "auto_update" in _SAVEABLE                 # persisted
    assert "auto_update" in SATELLITE_PULL_FIELDS     # inherited by remotes (global)


def test_server_reports_its_version_to_satellites():
    from app.config import APP_VERSION
    # The satellite config payload carries the server version so a remote can
    # match it. Build the response shape directly via the handler's helper path.
    import app.routers.satellite as srv
    # The version constant is what the endpoint embeds.
    assert srv.APP_VERSION == APP_VERSION


# -- UI ---------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    # Configured so the setup-redirect middleware lets /setup/* through.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _setup_html(client, monkeypatch, mode):
    monkeypatch.setattr(settings, "deployment_mode", mode)
    with patch.object(type(settings), "is_configured", lambda self: True):
        return client.get("/setup").text


def test_server_shows_editable_auto_update_toggle(client, monkeypatch):
    monkeypatch.setenv("WATCHTOWER_HTTP_API_TOKEN", "secret")
    html = _setup_html(client, monkeypatch, "server")
    assert 'id="auto_update"' in html
    assert 'onchange="saveAutoUpdate(this)"' in html       # editable
    # Server-specific notes: what the switch and channel really govern here,
    # and how to pin or stop the server's own updater.
    assert "PANTRYRAIDER_TAG" in html
    assert "docker compose stop watchtower" in html
    assert "connected Pi Remotes" in html
    # Releases only is not recommended where it does not govern the server.
    assert "recommended choice for an everyday kitchen" not in html


def test_server_with_updater_shows_update_now_and_hidden_warning(client, monkeypatch):
    monkeypatch.setenv("WATCHTOWER_HTTP_API_TOKEN", "secret")
    html = _setup_html(client, monkeypatch, "server")
    assert 'id="server-update-btn"' in html
    assert 'id="updater-warning"' in html
    assert 'id="updater-warning" class="d-none' in html
    assert "docker-compose.prod.yml" in html
    # The new compose file upgrades Mealie one way, so the warning says to
    # back up before replacing the file.
    warning = html[html.index('id="updater-warning"'):]
    warning = warning[:warning.index("</div>")]
    assert warning.index("back up") < warning.index("docker-compose.prod.yml")


def test_server_without_updater_hides_update_now(client, monkeypatch):
    # Unraid and other installs with no Watchtower: no dead button, no
    # Watchtower copy, and a pointer to the tool that does update them.
    monkeypatch.delenv("WATCHTOWER_HTTP_API_TOKEN", raising=False)
    html = _setup_html(client, monkeypatch, "server")
    assert 'id="server-update-btn"' not in html
    assert 'id="updater-warning"' not in html
    assert "Apply update on the Docker tab" in html
    assert "docker compose stop watchtower" not in html


def test_pi_hosted_keeps_its_own_update_copy(client, monkeypatch):
    html = _setup_html(client, monkeypatch, "pi_hosted")
    assert 'id="update-btn"' in html
    assert "recommended choice for an everyday kitchen" in html
    assert "PANTRYRAIDER_TAG" not in html


def test_satellite_auto_update_toggle_is_read_only(client, monkeypatch):
    html = _setup_html(client, monkeypatch, "pi_remote")
    assert 'id="auto_update"' in html
    assert 'onchange="saveAutoUpdate(this)"' not in html   # disabled on a remote
    assert "Managed on the main server" in html


def test_update_server_rejects_pi_appliance(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted")
    r = client.post("/setup/update-server").json()
    assert r["ok"] is False and "Pi appliance" in r["error"]


def test_update_server_without_an_updater_points_at_the_container_tool(client, monkeypatch):
    # Unraid and other installs with no Watchtower: the answer names what does
    # update them instead of telling them to add a service they do not run.
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.delenv("WATCHTOWER_HTTP_API_TOKEN", raising=False)
    r = client.post("/setup/update-server").json()
    assert r["ok"] is False
    assert r["error"] == (
        "This install is updated by the tool that runs its container. On "
        "Unraid, use Apply update on the Docker tab.")
    assert "watchtower" not in r["error"].lower()


def _fake_watchtower(monkeypatch, *, status=200, raises=None, posted=None, got=None):
    """Swap the router's httpx.AsyncClient for one that answers every request
    with the given status (or raises), recording what was sent."""
    import app.routers.setup as srouter

    class _Resp:
        status_code = status

    class _FakeClient:
        def __init__(self, *a, **k):
            if got is not None:
                got.setdefault("timeouts", []).append(k.get("timeout"))
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, **k):
            if posted is not None:
                posted["url"] = url
                posted["auth"] = (headers or {}).get("Authorization")
            if raises is not None:
                raise raises
            return _Resp()
        async def get(self, url, headers=None, **k):
            if got is not None:
                got["url"] = url
                got["auth"] = (headers or {}).get("Authorization")
            if raises is not None:
                raise raises
            return _Resp()

    monkeypatch.setattr(srouter.httpx, "AsyncClient", _FakeClient)


@pytest.fixture
def server_with_updater(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setenv("WATCHTOWER_HTTP_API_TOKEN", "secret")
    monkeypatch.setenv("WATCHTOWER_URL", "http://watchtower:8080")


@pytest.mark.parametrize("status", [200, 202])
def test_update_server_triggers_watchtower_without_waiting(client, monkeypatch,
                                                           server_with_updater, status):
    posted = {}
    _fake_watchtower(monkeypatch, status=status, posted=posted)
    r = client.post("/setup/update-server").json()
    assert r["ok"] is True
    # async=true: the maintained fork answers 202 at once instead of holding
    # the request for the whole pull; the old image ignores it.
    assert posted["url"] == "http://watchtower:8080/v1/update?async=true"
    assert posted["auth"] == "Bearer secret"


def test_update_server_busy_updater(client, monkeypatch, server_with_updater):
    _fake_watchtower(monkeypatch, status=429)
    r = client.post("/setup/update-server").json()
    assert r["ok"] is False
    assert r["error"] == ("An update is already running. Give it a minute, "
                          "then check the version again.")


def test_update_server_slow_pull_counts_as_started(client, monkeypatch, server_with_updater):
    # The old image holds the request until the pull finishes. A slow pull is
    # an update in progress, not a missing updater.
    import httpx
    _fake_watchtower(monkeypatch, raises=httpx.ReadTimeout("slow"))
    r = client.post("/setup/update-server").json()
    assert r["ok"] is True
    assert "started" in r["message"] and "restart" in r["message"]
    assert "not running" not in r["message"]


@pytest.mark.parametrize("exc_name", ["ConnectError", "ConnectTimeout"])
def test_update_server_unreachable_updater_says_how_to_fix_it(client, monkeypatch,
                                                              server_with_updater, exc_name):
    import httpx
    _fake_watchtower(monkeypatch, raises=getattr(httpx, exc_name)("down"))
    r = client.post("/setup/update-server").json()
    assert r["ok"] is False
    err = r["error"]
    assert err.startswith("The automatic updater is not answering.")
    assert "Docker 29" in err
    assert "docker-compose.prod.yml as docker-compose.yml" in err
    assert "docker compose up -d" in err
    # The new file also upgrades Mealie one way, so the backup comes first.
    assert err.index("back up") < err.index("docker-compose.prod.yml")
    # The old text claimed the service was missing, which is wrong for an
    # updater that is in the file but crash-looping.
    assert "probably not running" not in err


# -- updater status -----------------------------------------------------------

@pytest.mark.parametrize("status", [401, 405, 200])
def test_updater_status_any_http_answer_is_reachable(client, monkeypatch,
                                                     server_with_updater, status):
    # The fork answers 405 to GET, the old image 401; either one means a
    # process is listening.
    got = {}
    _fake_watchtower(monkeypatch, status=status, got=got)
    r = client.get("/setup/updater-status").json()
    assert r == {"configured": True, "reachable": True}
    assert got["url"] == "http://watchtower:8080/v1/update"
    # Never authenticated: the old image runs an update on any method.
    assert got["auth"] is None
    assert got["timeouts"] == [2.0]


@pytest.mark.parametrize("exc_name", ["ConnectError", "ConnectTimeout", "ReadTimeout"])
def test_updater_status_connection_failure_is_unreachable(client, monkeypatch,
                                                          server_with_updater, exc_name):
    import httpx
    _fake_watchtower(monkeypatch, raises=getattr(httpx, exc_name)("down"))
    r = client.get("/setup/updater-status").json()
    assert r == {"configured": True, "reachable": False}


def test_updater_status_without_token_skips_the_probe(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.delenv("WATCHTOWER_HTTP_API_TOKEN", raising=False)
    got = {}
    _fake_watchtower(monkeypatch, status=200, got=got)
    r = client.get("/setup/updater-status").json()
    assert r == {"configured": False, "reachable": False}
    assert "url" not in got


def test_updater_status_is_server_only(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted")
    monkeypatch.setenv("WATCHTOWER_HTTP_API_TOKEN", "secret")
    got = {}
    _fake_watchtower(monkeypatch, status=200, got=got)
    r = client.get("/setup/updater-status").json()
    assert r == {"configured": False, "reachable": False}
    assert "url" not in got


def test_setup_save_persists_auto_update(client, monkeypatch):
    # monkeypatch captures the original so the flag is restored after this test,
    # keeping the on-by-default invariant intact for the rest of the suite.
    monkeypatch.setattr(settings, "auto_update", True)
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted")
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = client.post("/setup/save", json={"auto_update": False})
    assert r.status_code == 200
    assert settings.auto_update is False


# -- the restart poll after Update now ------------------------------------------

_POLL_HARNESS = r"""
const src = require('fs').readFileSync(%(path)s, 'utf8');
const start = src.indexOf('async function _pollForRestart');
let depth = 0, end = -1;
for (let i = src.indexOf('{', start); i < src.length; i++) {
  if (src[i] === '{') depth++;
  else if (src[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
}
const poll = eval('(' + src.slice(start, end) + ')');
const plan = %(plan)s;
let now = 0;
Date.now = () => now;
global.setTimeout = (fn, ms) => { now += ms || 0; fn(); };
global.window = { __APP_VERSION: '0.19.6' };
global.location = { reload() {} };
global.fetch = async () => {
  const step = plan.find(s => now < s.until) || plan[plan.length - 1];
  if (step.v === 'down') throw new Error('down');
  return { ok: true, json: async () => ({ version: step.v }) };
};
const out = { className: '', textContent: '', innerHTML: '' };
poll(out, %(wait)s).then(() => console.log(JSON.stringify({ t: now, text: out.textContent })));
"""


def _run_poll(plan, wait):
    import json
    import shutil
    import subprocess
    if shutil.which("node") is None:
        pytest.skip("node not available")
    js = SERVICE / "app" / "static" / "js" / "setup" / "devices-updates.js"
    script = _POLL_HARNESS % {"path": json.dumps(str(js)), "plan": json.dumps(plan),
                              "wait": json.dumps(wait)}
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_update_now_keeps_waiting_while_the_new_version_downloads():
    # The updater answers at once and downloads in the background, so the old
    # app keeps answering with the old version for a while. That is not
    # "already current": keep polling until the new version comes back.
    r = _run_poll([{"until": 60000, "v": "0.19.6"}, {"until": 70000, "v": "down"},
                   {"until": 10 ** 9, "v": "0.19.7"}], 120000)
    assert r["text"].startswith("Updated to v0.19.7")


def test_update_now_with_nothing_newer_still_says_so():
    r = _run_poll([{"until": 10 ** 9, "v": "0.19.6"}], 120000)
    assert r["text"].startswith("You are on v0.19.6")
    assert r["t"] >= 120000
    # Once the app has restarted, the same version is the real answer.
    r = _run_poll([{"until": 10000, "v": "down"}, {"until": 10 ** 9, "v": "0.19.6"}], 120000)
    assert r["text"].startswith("You are on v0.19.6") and r["t"] < 20000


def test_update_now_passes_the_download_wait_to_the_poll():
    js = (SERVICE / "app" / "static" / "js" / "setup" / "devices-updates.js").read_text()
    body = js[js.index("async function updateServerNow"):js.index("async function checkUpdaterStatus")]
    assert "_pollForRestart(out, 120000)" in body
