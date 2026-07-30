"""Satellite forwarding for the shared timer registry (FoodAssistant-vh3r).

Timers live on the MAIN server (services/timers.py), so on a pi_remote every
/timers call must forward there and pass the server's answer through verbatim.
If any endpoint used the satellite's local in-memory registry instead, a timer
started on the satellite would be invisible on the server and every other
device (and vice versa).

Follows the satellite forwarding tests in test_scanner_mode.py: the forwarding
httpx client is replaced by a recorder that answers like the main server.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import current_recipe, timers  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    timers.clear_all()
    current_recipe.clear_active()
    yield
    timers.clear_all()
    current_recipe.clear_active()


class _FwdRecorder:
    """Stands in for current_recipe._fwd_client: records every forwarded
    request and answers like a main server that owns the timer registry."""

    def __init__(self, response: httpx.Response | None = None):
        self.calls: list[dict] = []
        self.response = response or httpx.Response(
            200, json={"timer": {"id": 7, "label": "Pasta"}, "from": "main-server"})
        self.raise_exc: Exception | None = None

    async def request(self, method, url, headers=None, params=None,
                      content=None, json=None):
        self.calls.append({
            "method": method, "url": url,
            "api_key": (headers or {}).get("X-API-Key", ""),
            "content": content, "json": json,
        })
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


@pytest.fixture
def sat_client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "http://main.server:9284", raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "sat-key", raising=False)
    from app.routers import current_recipe as cr_router
    recorder = _FwdRecorder()
    monkeypatch.setattr(cr_router, "_fwd_client", recorder)
    # The GET /timers micro-cache is module-level state; start each test cold
    # so one test's cached response never answers another test's GET.
    cr_router._timers_cache.invalidate()
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app), recorder
    finally:
        os.chdir(cwd)


def test_satellite_forwards_every_timer_call(sat_client):
    client, recorder = sat_client
    responses = [
        client.get("/timers"),
        client.post("/timers", json={"label": "Pasta", "seconds": 600}),
        client.get("/timers/7"),
        client.post("/timers/7/extend", json={"seconds": 60}),
        client.delete("/timers/7"),
        client.delete("/timers"),  # the Timers page Clear all
    ]
    assert [(c["method"], c["url"]) for c in recorder.calls] == [
        ("GET", "http://main.server:9284/timers"),
        ("POST", "http://main.server:9284/timers"),
        ("GET", "http://main.server:9284/timers/7"),
        ("POST", "http://main.server:9284/timers/7/extend"),
        ("DELETE", "http://main.server:9284/timers/7"),
        ("DELETE", "http://main.server:9284/timers"),
    ]
    # Every call authenticates with the satellite's upstream key and returns
    # the server's answer verbatim.
    assert all(c["api_key"] == "sat-key" for c in recorder.calls)
    for r in responses:
        assert r.status_code == 200
        assert r.json()["from"] == "main-server"


def test_satellite_post_body_reaches_the_server_verbatim(sat_client):
    client, recorder = sat_client
    client.post("/timers", json={"label": "Rice", "seconds": 900})
    import json as _json
    assert _json.loads(recorder.calls[0]["content"]) == {"label": "Rice", "seconds": 900}


def test_satellite_never_touches_local_timer_registry(sat_client):
    client, recorder = sat_client
    client.post("/timers", json={"label": "Pasta", "seconds": 600})
    client.post("/timers/7/extend", json={"seconds": 60})
    client.delete("/timers/7")
    # The single source of truth is the main server: the in-process registry
    # on the satellite stays empty.
    assert timers.list_timers() == []


def test_satellite_passes_upstream_error_statuses_through(sat_client):
    client, recorder = sat_client
    recorder.response = httpx.Response(404, json={"detail": "Timer not found"})
    r = client.delete("/timers/99")
    assert r.status_code == 404
    assert r.json()["detail"] == "Timer not found"


def test_satellite_clear_all_forwards_and_never_clears_locally(sat_client):
    # Clear all (DELETE on the /timers collection) is timer traffic like any
    # other: forwarded verbatim, answered by the server, local registry
    # untouched.
    client, recorder = sat_client
    recorder.response = httpx.Response(200, json={"ok": True, "cleared": 3})
    timers.create_timer("Local-only artifact", 60)  # must survive the forward
    r = client.delete("/timers")
    assert (recorder.calls[0]["method"], recorder.calls[0]["url"]) == (
        "DELETE", "http://main.server:9284/timers")
    assert recorder.calls[0]["api_key"] == "sat-key"
    assert r.status_code == 200
    assert r.json() == {"ok": True, "cleared": 3}
    assert len(timers.list_timers()) == 1


def test_satellite_reports_unreachable_server_as_502(sat_client):
    client, recorder = sat_client
    recorder.raise_exc = httpx.ConnectError("boom")
    r = client.get("/timers")
    assert r.status_code == 502
    assert "main server is not reachable" in r.json()["detail"]


# Recipe-suggestion start (POST /current-recipe/timers/start) -----------------
#
# The active recipe is per-device (there is no satellite forwarding for
# /current-recipe), so the suggestion must be resolved against the SATELLITE's
# own recipe, but the resulting timer must land in the server registry.


def test_suggestion_start_resolves_locally_and_creates_upstream(sat_client):
    client, recorder = sat_client
    current_recipe.set_active({"title": "x", "steps": ["Simmer 20 minutes"]})
    r = client.post("/current-recipe/timers/start", json={"step_index": 0})
    assert r.status_code == 200
    assert r.json()["from"] == "main-server"
    # The resolved label and duration were posted to the server's /timers.
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert (call["method"], call["url"]) == ("POST", "http://main.server:9284/timers")
    assert call["api_key"] == "sat-key"
    assert call["json"] == {"label": "Simmer", "seconds": 1200.0}
    assert timers.list_timers() == []


def test_suggestion_start_no_match_stays_local_404(sat_client):
    client, recorder = sat_client
    # No active recipe and no explicit seconds: nothing to start, and nothing
    # is forwarded (the 404 comes from the local suggestion resolution).
    r = client.post("/current-recipe/timers/start", json={"step_index": 3})
    assert r.status_code == 404
    assert recorder.calls == []


def test_suggestion_start_upstream_down_is_502(sat_client):
    client, recorder = sat_client
    recorder.raise_exc = httpx.ConnectError("boom")
    current_recipe.set_active({"title": "x", "steps": ["Simmer 20 minutes"]})
    r = client.post("/current-recipe/timers/start", json={"step_index": 0})
    assert r.status_code == 502
    assert "main server is not reachable" in r.json()["detail"]


# Server-mode regression -------------------------------------------------------


@pytest.fixture
def server_client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    # Enough config to count as set up, so the setup-redirect middleware does
    # not intercept the timer calls when this file runs on its own.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test:9383", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "test-key", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_server_mode_still_uses_the_local_registry(server_client):
    created = server_client.post(
        "/timers", json={"label": "Pasta", "seconds": 600}).json()["timer"]
    assert any(t["id"] == created["id"] for t in timers.list_timers())
    listed = server_client.get("/timers").json()["timers"]
    assert [t["id"] for t in listed] == [created["id"]]
    assert server_client.post(
        f"/timers/{created['id']}/extend", json={"seconds": 60}
    ).json()["timer"]["total_seconds"] == 660
    assert server_client.delete(f"/timers/{created['id']}").json() == {"ok": True}
    assert timers.list_timers() == []


def test_server_mode_clear_all_empties_the_registry(server_client):
    server_client.post("/timers", json={"label": "Pasta", "seconds": 600})
    server_client.post("/timers", json={"label": "Rice", "seconds": 900})
    r = server_client.delete("/timers")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "cleared": 2}
    assert timers.list_timers() == []
    # Clearing an already-empty registry succeeds and reports zero.
    assert server_client.delete("/timers").json() == {"ok": True, "cleared": 0}
    # The collection route never shadows the per-timer delete.
    assert server_client.delete("/timers/999").status_code == 404


# Deck reconciliation over a forwarded GET /timers -----------------------------
#
# The Stream Deck on a satellite polls its LOCAL app base; with forwarding the
# list it gets back is the server's. deadline_epoch is the shared wall clock,
# so sync_timer_bindings reconciles exactly as it would against a local server.


def test_deck_sync_reconciles_against_forwarded_timer_list(sat_client, monkeypatch):
    client, recorder = sat_client
    now = time.time()
    recorder.response = httpx.Response(200, json={"timers": [
        {"id": 3, "label": "Pasta", "total_seconds": 600,
         "remaining_seconds": 500, "running": True, "expired": False,
         "deadline_epoch": now + 500, "created_epoch": now - 100},
    ]})
    server_timers = client.get("/timers").json()["timers"]

    from foodassistant_streamdeck import actions
    binding = actions.TimerState()
    changed = actions.sync_timer_bindings(
        {"timer1": binding}, {"timer1": "Pasta"}, server_timers, now)
    # The idle deck key adopts the server timer started elsewhere, keyed by
    # the shared deadline_epoch.
    assert changed is True
    assert binding.timer_id == 3
    assert binding.deadline_epoch == pytest.approx(now + 500)

    # And when the server (reached through the same forwarding) no longer
    # lists it, the key clears back to idle. The deck's next poll lands after
    # the satellite's short GET micro-cache has expired; model that here by
    # dropping the cached entry instead of sleeping out the TTL.
    from app.routers import current_recipe as cr_router
    cr_router._timers_cache.invalidate()
    recorder.response = httpx.Response(200, json={"timers": []})
    server_timers = client.get("/timers").json()["timers"]
    changed = actions.sync_timer_bindings(
        {"timer1": binding}, {"timer1": "Pasta"}, server_timers, now)
    assert changed is True
    assert binding.timer_id is None
    assert binding.deadline_epoch == 0.0


# GET /timers micro-cache (FoodAssistant-3mq) ----------------------------------
#
# On a Pi Remote several surfaces poll GET /timers within the same second (the
# Timers page, Start Page faces, screensaver pills, the deck). A short TTL
# cache turns that burst into ONE upstream round trip; any timer mutation
# forwarded through this satellite drops it so changes show immediately.


def test_burst_of_timer_gets_forwards_upstream_once(sat_client):
    client, recorder = sat_client
    recorder.response = httpx.Response(200, json={"timers": [], "from": "main-server"})
    first = client.get("/timers")
    second = client.get("/timers")
    third = client.get("/timers")
    # One round trip served all three pollers, each with the identical body.
    assert len(recorder.calls) == 1
    assert first.content == second.content == third.content
    assert second.status_code == 200


def test_timer_mutation_invalidates_the_get_cache(sat_client):
    client, recorder = sat_client
    recorder.response = httpx.Response(200, json={"timers": []})
    client.get("/timers")
    client.post("/timers", json={"label": "Pasta", "seconds": 600})
    recorder.response = httpx.Response(
        200, json={"timers": [{"id": 1, "label": "Pasta"}]})
    listed = client.get("/timers").json()["timers"]
    # GET, POST, GET: the mutation dropped the cached empty list, so the new
    # timer is visible on the very next poll, not after the TTL.
    assert [c["method"] for c in recorder.calls] == ["GET", "POST", "GET"]
    assert listed == [{"id": 1, "label": "Pasta"}]


def test_suggestion_start_also_invalidates_the_get_cache(sat_client):
    client, recorder = sat_client
    recorder.response = httpx.Response(200, json={"timers": []})
    client.get("/timers")
    current_recipe.set_active({"title": "x", "steps": ["Simmer 20 minutes"]})
    client.post("/current-recipe/timers/start", json={"step_index": 0})
    recorder.response = httpx.Response(
        200, json={"timers": [{"id": 2, "label": "Simmer"}]})
    listed = client.get("/timers").json()["timers"]
    assert listed == [{"id": 2, "label": "Simmer"}]


def test_unreachable_server_response_is_never_cached(sat_client):
    client, recorder = sat_client
    recorder.raise_exc = httpx.ConnectError("boom")
    assert client.get("/timers").status_code == 502
    # The server comes back: the next poll must reach it, not replay the 502.
    recorder.raise_exc = None
    recorder.response = httpx.Response(200, json={"timers": []})
    assert client.get("/timers").status_code == 200
    assert len(recorder.calls) == 2


def test_upstream_error_status_is_never_cached(sat_client):
    client, recorder = sat_client
    recorder.response = httpx.Response(503, json={"detail": "busy"})
    assert client.get("/timers").status_code == 503
    recorder.response = httpx.Response(200, json={"timers": []})
    assert client.get("/timers").status_code == 200
    assert len(recorder.calls) == 2


def test_server_mode_never_uses_the_forward_cache(server_client, monkeypatch):
    # On the main server GET /timers reads the local registry directly; the
    # micro-cache is satellite plumbing and must not delay local reads.
    from app.routers import current_recipe as cr_router
    cr_router._timers_cache.invalidate()
    server_client.post("/timers", json={"label": "Pasta", "seconds": 600})
    assert len(server_client.get("/timers").json()["timers"]) == 1
    server_client.delete("/timers")
    assert server_client.get("/timers").json()["timers"] == []
