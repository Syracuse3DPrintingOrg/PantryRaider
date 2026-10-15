"""How a server updates itself (services/update_path.py) and the routes on it.

A Pi updates through its host bridge and never probes. A server with the
Watchtower token uses Watchtower and never probes. Anything else probes the
local bridge's GET /identity with a fresh nonce and no token: a passing proof
writes the latch and makes the server a 'server_bridge' install; an older
bridge (401 from its token gate, or 404 in its first day) that still answers
the token-exempt GET /hostname is 'legacy' until the latch exists. Every
server_bridge failure names sudo foodassistant-update, and the Compose and
Unraid answers stay exactly as they were.
"""
from __future__ import annotations

import hashlib
import hmac
import http.server
import importlib.machinery
import importlib.util
import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
SERVICE = REPO / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.routers import setup as setup_router  # noqa: E402
from app.services import bridge, update_path  # noqa: E402
from page_scripts import with_page_scripts

BRIDGE_SCRIPT = REPO / "scripts" / "image-build" / "foodassistant-host-bridge"
VECTORS = json.loads((REPO / "tests" / "fixtures" / "local_credentials_vectors.json").read_text())
TOKEN = VECTORS["token"]
LAN_PEER = ("192.168.1.20", 50000)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.delenv("WATCHTOWER_HTTP_API_TOKEN", raising=False)
    monkeypatch.delenv("FOODASSISTANT_FORCE_MODEL", raising=False)
    bridge.invalidate_bridge_token()
    update_path.reset_cache()
    yield tmp_path
    bridge.invalidate_bridge_token()
    update_path.reset_cache()


@pytest.fixture
def token_file(env):
    (env / "bridge-token").write_text(TOKEN + "\n")
    bridge.invalidate_bridge_token()
    return TOKEN


@pytest.fixture
def client(env, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.chdir(SERVICE)
    with patch.object(type(settings), "is_configured", lambda self: True):
        yield TestClient(app, client=LAN_PEER)


class _FakeBridge:
    """An httpx.MockTransport handler standing in for the local bridge."""

    def __init__(self, identity=None, hostname=200, proof_token=TOKEN, raise_connect=False):
        self.identity = identity        # status for /identity, None = a valid proof
        self.hostname = hostname
        self.proof_token = proof_token
        self.raise_connect = raise_connect
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_connect:
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/identity":
            if self.identity is None:
                nonce = request.url.params.get("nonce", "")
                return httpx.Response(200, json={
                    "ok": True, "service": "foodassistant-host-bridge",
                    "proof": update_path.identity_proof(self.proof_token, nonce)})
            if self.identity == 401:
                return httpx.Response(401, json={
                    "ok": False, "error": "missing or invalid X-Bridge-Token"})
            return httpx.Response(self.identity, json={"error": "x"})
        if request.url.path == "/hostname":
            if self.hostname == 200:
                return httpx.Response(200, json={"ok": True, "hostname": "kitchen"})
            return httpx.Response(self.hostname, json={})
        return httpx.Response(404, json={})


def _use(monkeypatch, fake):
    """Route every probe (including resolve()'s) through the fake bridge."""
    def _client(transport=None):
        return httpx.AsyncClient(transport=httpx.MockTransport(fake),
                                 timeout=httpx.Timeout(2.0, connect=1.0))
    monkeypatch.setattr(update_path, "_client", _client)
    return fake


def _no_probe(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("probed")
    monkeypatch.setattr(update_path, "probe_server_bridge", boom)


# -- the kind matrix ----------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["pi_hosted", "pi_remote"])
async def test_pi_never_probes(env, monkeypatch, mode):
    monkeypatch.setattr(settings, "deployment_mode", mode)
    _no_probe(monkeypatch)
    r = await update_path.resolve(force=True)
    assert r["kind"] == "pi" and r["bridge"] == "not_checked"
    assert update_path.usable(r) is True


@pytest.mark.anyio
async def test_watchtower_token_wins_before_any_probe(token_file, monkeypatch):
    monkeypatch.setenv("WATCHTOWER_HTTP_API_TOKEN", "secret")
    fake = _use(monkeypatch, _FakeBridge())
    r = await update_path.resolve(force=True)
    assert r == {"kind": "watchtower", "bridge": "not_checked", "bridge_hint": False}
    assert fake.requests == []


@pytest.mark.anyio
async def test_no_token_file_is_a_container_with_no_io(env, monkeypatch):
    fake = _use(monkeypatch, _FakeBridge())
    assert await update_path.probe_server_bridge() == "absent"
    r = await update_path.resolve(force=True)
    assert r["kind"] == "container" and r["bridge"] == "absent"
    assert fake.requests == []


@pytest.mark.anyio
async def test_valid_proof_is_verified_and_writes_the_latch(token_file, env, monkeypatch):
    fake = _use(monkeypatch, _FakeBridge())
    r = await update_path.resolve(force=True)
    assert r["kind"] == "server_bridge" and r["bridge"] == "verified"
    latch = env / "bridge-identity-verified"
    assert latch.exists()
    assert (latch.stat().st_mode & 0o777) == 0o600
    assert update_path.usable(r) is True
    assert await update_path.auto_update_eligible() is True
    for req in fake.requests:
        assert "x-bridge-token" not in {k.lower() for k in req.headers}


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 404])
async def test_old_bridge_is_legacy_without_the_latch(token_file, monkeypatch, status):
    fake = _use(monkeypatch, _FakeBridge(identity=status))
    assert await update_path.probe_server_bridge() == "legacy"
    paths = [req.url.path for req in fake.requests]
    assert paths == ["/identity", "/hostname"]


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 404])
async def test_old_answer_with_the_latch_is_a_mismatch(token_file, env, monkeypatch, status):
    (env / "bridge-identity-verified").write_text("x")
    fake = _use(monkeypatch, _FakeBridge(identity=status))
    assert await update_path.probe_server_bridge() == "mismatch"
    assert [req.url.path for req in fake.requests] == ["/identity"]


@pytest.mark.anyio
async def test_old_answer_without_hostname_is_unreachable(token_file, monkeypatch):
    _use(monkeypatch, _FakeBridge(identity=401, hostname=500))
    assert await update_path.probe_server_bridge() == "unreachable"


@pytest.mark.anyio
async def test_wrong_proof_is_a_mismatch(token_file, monkeypatch):
    _use(monkeypatch, _FakeBridge(proof_token="some-other-token"))
    r = await update_path.resolve(force=True)
    assert r["kind"] == "server_bridge" and r["bridge"] == "mismatch"
    assert update_path.usable(r) is False


@pytest.mark.anyio
async def test_a_token_replaced_on_disk_still_verifies(token_file, env, monkeypatch):
    # The app cached the old token; a reinstall wrote a new one that the
    # bridge now proves with. One fresh read of the file settles it.
    bridge.invalidate_bridge_token()
    assert bridge.bridge_token() == TOKEN
    (env / "bridge-token").write_text("a-new-token-after-reinstall\n")
    _use(monkeypatch, _FakeBridge(proof_token="a-new-token-after-reinstall"))
    assert await update_path.probe_server_bridge() == "verified"
    assert bridge.bridge_token() == "a-new-token-after-reinstall"


@pytest.mark.anyio
async def test_a_non_ascii_proof_is_a_mismatch(token_file, monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"ok": True, "proof": "\u00e9" * 64})
    _use(monkeypatch, handler)
    assert await update_path.probe_server_bridge() == "mismatch"


@pytest.mark.anyio
async def test_503_is_unreachable(token_file, monkeypatch):
    _use(monkeypatch, _FakeBridge(identity=503))
    assert await update_path.probe_server_bridge() == "unreachable"


@pytest.mark.anyio
async def test_refused_without_evidence_is_a_container(token_file, monkeypatch):
    _use(monkeypatch, _FakeBridge(raise_connect=True))
    r = await update_path.resolve(force=True)
    assert r["kind"] == "container" and r["bridge"] == "absent"


@pytest.mark.anyio
async def test_refused_with_force_model_is_an_unreachable_bridge(token_file, monkeypatch):
    monkeypatch.setenv("FOODASSISTANT_FORCE_MODEL", "server")
    _use(monkeypatch, _FakeBridge(raise_connect=True))
    r = await update_path.resolve(force=True)
    assert r == {"kind": "server_bridge", "bridge": "unreachable", "bridge_hint": True}


@pytest.mark.anyio
async def test_refused_with_the_latch_is_an_unreachable_bridge(token_file, env, monkeypatch):
    (env / "bridge-identity-verified").write_text("x")
    _use(monkeypatch, _FakeBridge(raise_connect=True))
    r = await update_path.resolve(force=True)
    assert r["kind"] == "server_bridge" and r["bridge"] == "unreachable"


@pytest.mark.anyio
async def test_resolve_caches_until_forced(token_file, monkeypatch):
    fake = _use(monkeypatch, _FakeBridge())
    await update_path.resolve()
    await update_path.resolve()
    assert len(fake.requests) == 1
    await update_path.resolve(force=True)
    assert len(fake.requests) == 2


def test_identity_proof_matches_hmac_and_the_shared_vectors():
    nonce = "00112233445566778899aabbccddeeff"
    want = hmac.new(TOKEN.encode(), b"pantryraider-bridge-identity-v1:" + nonce.encode(),
                    hashlib.sha256).hexdigest()
    assert update_path.identity_proof(TOKEN, nonce) == want
    for v in VECTORS["identity_proofs"]:
        assert update_path.identity_proof(TOKEN, v["nonce"]) == v["proof"]


# -- against the real bridge script -------------------------------------------

def _load_bridge():
    spec = importlib.util.spec_from_loader(
        "foodassistant_host_bridge_z10",
        importlib.machinery.SourceFileLoader("foodassistant_host_bridge_z10", str(BRIDGE_SCRIPT)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def real_bridge(monkeypatch):
    mod = _load_bridge()
    monkeypatch.setattr(mod, "_EXPECTED_TOKEN", TOKEN)
    monkeypatch.setattr(mod, "_grace_active", lambda *a, **k: False)
    monkeypatch.setattr(mod, "_mark_graduated", lambda *a, **k: None)
    monkeypatch.setattr(mod._Handler, "_get_hostname",
                        lambda self: self._send(200, {"ok": True, "hostname": "kitchen"}))
    monkeypatch.setattr(mod._Handler, "log_message", lambda self, *a, **k: None)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), mod._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(update_path, "BRIDGE_BASE", base)
    try:
        yield mod, base
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.anyio
async def test_fielded_bridge_without_identity_is_legacy_then_mismatch(
        real_bridge, token_file, env, client, monkeypatch):
    mod, base = real_bridge
    monkeypatch.setattr(mod, "TOKEN_EXEMPT_GET", frozenset(mod.TOKEN_EXEMPT_GET - {"/identity"}))
    async with httpx.AsyncClient() as c:
        raw = await c.get(f"{base}/identity", params={"nonce": "0" * 32})
    assert raw.status_code == 401
    assert await update_path.probe_server_bridge() == "legacy"
    assert not (env / "bridge-identity-verified").exists()
    r = await update_path.resolve(force=True)
    assert r["kind"] == "server_bridge" and r["bridge"] == "legacy"

    async def fake_update():
        return {"ok": True, "before": "a", "after": "a"}
    monkeypatch.setattr(setup_router, "run_host_bridge_update", fake_update)
    body = client.post("/setup/update").json()
    assert body == {"ok": True, "before": "a", "after": "a"}

    (env / "bridge-identity-verified").write_text("x")
    assert await update_path.probe_server_bridge() == "mismatch"


@pytest.mark.anyio
async def test_r2_bridge_proves_itself(real_bridge, token_file, env):
    assert await update_path.probe_server_bridge() == "verified"
    assert (env / "bridge-identity-verified").exists()


# -- update routes ------------------------------------------------------------

def _resolved(monkeypatch, kind, bridge_state, hint=False):
    async def fake(force=False):
        return {"kind": kind, "bridge": bridge_state, "bridge_hint": hint}
    monkeypatch.setattr(update_path, "resolve", fake)


def _fake_bridge_update(monkeypatch):
    calls = []

    async def fake():
        calls.append(1)
        return {"ok": True, "before": "0.20.2", "after": "0.21.0"}
    monkeypatch.setattr(setup_router, "run_host_bridge_update", fake)
    return calls


@pytest.mark.parametrize("state", ["verified", "legacy"])
def test_update_runs_the_bridge_update_on_a_usable_server(client, monkeypatch, state):
    _resolved(monkeypatch, "server_bridge", state)
    calls = _fake_bridge_update(monkeypatch)
    assert client.post("/setup/update").json()["ok"] is True
    assert calls == [1]


@pytest.mark.parametrize("state,msg", [("mismatch", update_path.BRIDGE_MISMATCH_MSG),
                                       ("unreachable", update_path.BRIDGE_DOWN_MSG)])
def test_update_failures_name_the_hand_command(client, monkeypatch, state, msg):
    _resolved(monkeypatch, "server_bridge", state)
    calls = _fake_bridge_update(monkeypatch)
    body = client.post("/setup/update").json()
    assert body == {"ok": False, "error": msg}
    assert "sudo foodassistant-update" in body["error"]
    assert calls == []


def test_update_on_a_container(client, monkeypatch):
    _resolved(monkeypatch, "container", "absent")
    body = client.post("/setup/update").json()
    assert body == {"ok": False, "no_updater": True, "error": setup_router._NO_UPDATER_MSG}
    _resolved(monkeypatch, "container", "absent", hint=True)
    body = client.post("/setup/update").json()
    assert body == {"ok": False, "no_updater": True, "error": update_path.CONTAINER_HINT_MSG}
    assert "sudo foodassistant-update" in update_path.CONTAINER_HINT_MSG


def test_update_on_watchtower_delegates(client, monkeypatch):
    _resolved(monkeypatch, "watchtower", "not_checked")

    async def fake_server():
        return {"ok": True, "message": "delegated"}
    monkeypatch.setattr(setup_router, "update_server", fake_server)
    assert client.post("/setup/update").json() == {"ok": True, "message": "delegated"}


def test_update_server_uses_a_usable_bridge(client, monkeypatch):
    _resolved(monkeypatch, "server_bridge", "verified")
    calls = _fake_bridge_update(monkeypatch)
    assert client.post("/setup/update-server").json()["ok"] is True
    assert calls == [1]


def test_update_server_unraid_answer_is_unchanged(client, monkeypatch):
    _resolved(monkeypatch, "container", "absent")
    body = client.post("/setup/update-server").json()
    assert body == {"ok": False, "no_updater": True, "error": (
        "This install is updated by the tool that runs its container. "
        "On Unraid, use Apply update on the Docker tab.")}


def test_update_server_container_with_hint(client, monkeypatch):
    _resolved(monkeypatch, "container", "absent", hint=True)
    body = client.post("/setup/update-server").json()
    assert body == {"ok": False, "no_updater": True, "error": update_path.CONTAINER_HINT_MSG}


def test_updater_status_for_a_server_bridge(client, monkeypatch):
    _resolved(monkeypatch, "server_bridge", "unreachable")
    body = client.get("/setup/updater-status").json()
    assert body == {"configured": True, "reachable": False, "kind": "server_bridge",
                    "bridge": "unreachable", "message": update_path.BRIDGE_DOWN_MSG}
    _resolved(monkeypatch, "server_bridge", "mismatch")
    assert client.get("/setup/updater-status").json()["message"] == update_path.BRIDGE_MISMATCH_MSG
    _resolved(monkeypatch, "server_bridge", "verified")
    body = client.get("/setup/updater-status").json()
    assert body["reachable"] is True and body["message"] == ""


def test_updater_status_keeps_its_old_keys(client, monkeypatch):
    _resolved(monkeypatch, "container", "absent")
    body = client.get("/setup/updater-status").json()
    assert body["configured"] is False and body["reachable"] is False
    assert body["kind"] == "container" and body["bridge"] == "absent"


def test_update_state_relays_on_a_usable_install(client, monkeypatch):
    class _B:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **k):
            assert url.endswith("/update/state")
            return httpx.Response(200, json={"ok": True, "highest_version": "0.21.0",
                                             "trust_fp": "SHA256:abc"})
    monkeypatch.setattr(setup_router, "bridge_client", _B)
    _resolved(monkeypatch, "server_bridge", "verified")
    assert client.get("/setup/update/state").json()["highest_version"] == "0.21.0"
    _resolved(monkeypatch, "container", "absent")
    assert client.get("/setup/update/state").json() == {}
    _resolved(monkeypatch, "server_bridge", "mismatch")
    assert client.get("/setup/update/state").json() == {}


# -- the Settings page --------------------------------------------------------

def _page(client):
    r = client.get("/setup")
    assert r.status_code == 200
    return r.text


_UNRAID = "On Unraid, use Apply update on the Docker tab."
_SELF_UPDATING = "Installs the newest tested release."


def test_settings_render_for_a_server_bridge(client, monkeypatch):
    _resolved(monkeypatch, "server_bridge", "verified")
    html = _page(client)
    assert 'id="update-btn"' in html
    assert 'id="bridge-warning"' in html
    assert _SELF_UPDATING in html
    assert _UNRAID not in html
    assert 'id="updater-warning"' not in html
    assert "sudo foodassistant-update" in html


def test_settings_render_for_watchtower(client, monkeypatch):
    monkeypatch.setenv("WATCHTOWER_HTTP_API_TOKEN", "secret")
    _resolved(monkeypatch, "watchtower", "not_checked")
    html = _page(client)
    assert 'id="server-update-btn"' in html
    assert 'id="bridge-warning"' not in html


def test_settings_render_for_a_container(client, monkeypatch):
    _resolved(monkeypatch, "container", "absent")
    html = _page(client)
    assert _UNRAID in html and 'id="update-btn"' not in html
    assert 'id="container-update-hint"' not in html
    _resolved(monkeypatch, "container", "absent", hint=True)
    html = _page(client)
    assert 'id="container-update-hint"' in html
    assert "sudo foodassistant-update</code> to update Pantry Raider" in html


def test_channel_copy_and_the_note_for_updated_installs(client, monkeypatch):
    from app.services import auth_state
    _resolved(monkeypatch, "container", "absent")
    monkeypatch.setattr(type(settings), "update_channel_is_explicit", lambda self: False)
    html = _page(client)
    assert ">Every change</option>" in html
    assert "Every change, or tested releases only." in html
    # An install updated to 0.21 without a saved channel is told what changed,
    # with a way to dismiss it.
    assert auth_state.origin() == "upgraded"
    assert "This kitchen now installs tested releases only." in html
    assert "prChannelNoteSeen" in with_page_scripts(html) and 'aria-label="Dismiss"' in html
    # A saved channel, or a new install that never followed anything else,
    # gets no "now".
    monkeypatch.setattr(type(settings), "update_channel_is_explicit", lambda self: True)
    assert "This kitchen now installs tested releases only." not in _page(client)
    monkeypatch.setattr(type(settings), "update_channel_is_explicit", lambda self: False)
    monkeypatch.setattr(auth_state, "origin", lambda: "fresh")
    assert "This kitchen now installs tested releases only." not in _page(client)


def test_new_update_copy_has_no_em_dash():
    for text in (update_path.BRIDGE_DOWN_MSG, update_path.BRIDGE_MISMATCH_MSG,
                 update_path.CONTAINER_HINT_MSG):
        assert "\u2014" not in text
    for path in (SERVICE / "app" / "services" / "update_path.py",
                 SERVICE / "app" / "templates" / "setup" / "_pane_backups.html",
                 SERVICE / "app" / "static" / "js" / "setup" / "devices-updates.js"):
        assert "\u2014" not in path.read_text(), path
