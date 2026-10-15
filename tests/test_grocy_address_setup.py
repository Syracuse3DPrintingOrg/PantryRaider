"""The inventory address the setup flow saves is one the app can reach.

A Compose stack reaches its own Grocy at http://grocy:80, which is also the
shipped default. These tests pin the pieces that keep a fresh install on that
working address: it counts as configured once saved, the setup page pre-fills
the address the app's own probe found (never the browser's host), and the
connection test and Status only call the saved address good when that exact
address answers.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

_cwd = os.getcwd()
os.chdir(SERVICE)
try:
    import app.routers.setup as srouter  # noqa: E402
    from app.config import Settings, settings  # noqa: E402
    from app.services import status_summary  # noqa: E402
finally:
    os.chdir(_cwd)


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    """A Settings() free of the host's environment and any .env file."""
    monkeypatch.chdir(tmp_path)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)
    data = tmp_path / "data"
    data.mkdir()

    def build(saved: dict | None = None):
        if saved is not None:
            (data / "settings.json").write_text(json.dumps(saved))
        s = Settings()
        object.__setattr__(s, "data_dir", str(data))
        return s
    return build


# -- is_configured and the compose address -----------------------------------

def test_unsaved_default_address_is_not_configured(fresh):
    s = fresh()
    object.__setattr__(s, "auth_password", "hash")
    assert s.grocy_base_url == "http://grocy:80"
    assert s.grocy_address_chosen() is False
    assert s.is_configured() is False


def test_saved_compose_address_is_configured(fresh):
    # First-run provisioning and the wizard both save the address they used.
    s = fresh({"grocy_base_url": "http://grocy:80"})
    object.__setattr__(s, "auth_password", "hash")
    assert s.grocy_address_chosen() is True
    assert s.is_configured() is True


def test_compose_address_with_a_key_is_chosen(fresh):
    s = fresh()
    object.__setattr__(s, "grocy_api_key", "k")
    assert s.grocy_address_chosen() is True


def test_compose_address_from_the_environment_is_chosen(fresh, monkeypatch):
    monkeypatch.setenv("GROCY_BASE_URL", "http://grocy:80")
    s = fresh()
    assert s.grocy_address_chosen() is True


def test_empty_address_is_never_chosen(fresh):
    s = fresh({"grocy_base_url": ""})
    object.__setattr__(s, "grocy_base_url", "")
    object.__setattr__(s, "grocy_api_key", "k")
    assert s.grocy_address_chosen() is False


def test_any_other_address_is_chosen(fresh):
    s = fresh()
    object.__setattr__(s, "grocy_base_url", "http://192.168.1.50:9383")
    assert s.grocy_address_chosen() is True


# -- The setup page pre-fill --------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setitem(settings.__dict__, "grocy_base_url", "http://grocy:80")
    monkeypatch.setitem(settings.__dict__, "grocy_api_key", "")
    monkeypatch.setattr(settings, "deployment_mode", "server")
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _grocy_field_value(html: str) -> str:
    m = re.search(r'id="grocy_base_url"[^>]*?value="([^"]*)"', html, re.S)
    assert m, "no Grocy address field on the page"
    return m.group(1)


def test_setup_page_prefills_the_address_the_app_reached(client, monkeypatch):
    # The browser is another machine (TestClient's host is not loopback). The
    # field used to read http://<browser host>:9383, which the container may
    # not reach; it must hold the address the app's own probe found.
    async def _found():
        return "http://grocy:80"
    monkeypatch.setattr(srouter.page, "_detect_local_grocy", _found)
    with patch.object(type(settings), "is_configured", lambda self: True):
        html = client.get("/setup", headers={"host": "127.0.0.1:9284"}).text
    assert _grocy_field_value(html) == "http://grocy:80"
    assert ":9383" not in _grocy_field_value(html)


def test_setup_page_shows_a_saved_compose_address(client, monkeypatch):
    monkeypatch.setitem(settings.__dict__, "grocy_api_key", "k")

    async def _boom():
        raise AssertionError("a chosen address needs no probe")
    monkeypatch.setattr(srouter.page, "_detect_local_grocy", _boom)
    with patch.object(type(settings), "is_configured", lambda self: True):
        html = client.get("/setup").text
    assert _grocy_field_value(html) == "http://grocy:80"


# -- Test Grocy and Status check the saved address ----------------------------

_RealAsyncClient = httpx.AsyncClient


def _mock_http(monkeypatch, answers: dict[str, httpx.Response]):
    def _handle(request: httpx.Request) -> httpx.Response:
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        base = f"{request.url.scheme}://{request.url.host}:{port}"
        if base in answers:
            return answers[base]
        raise httpx.ConnectError("refused", request=request)

    def _factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _RealAsyncClient(transport=httpx.MockTransport(_handle), **kwargs)

    monkeypatch.setattr(srouter.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(srouter, "_refuse_probe_target", lambda url: "")


def _body(result):
    if isinstance(result, dict):
        return result
    return json.loads(result.body)


_INFO = httpx.Response(200, json={"grocy_version": {"Version": "4.7.1"}})


def test_test_grocy_reports_a_fallback_instead_of_connected(monkeypatch):
    _mock_http(monkeypatch, {"http://grocy:80": _INFO})
    out = _body(asyncio.run(srouter.test_grocy(srouter.TestGrocyPayload(
        grocy_base_url="http://127.0.0.1:9383", grocy_api_key="k"))))
    assert out["ok"] is False
    assert out["fallback_url"] == "http://grocy:80"
    assert "http://127.0.0.1:9383" in out["error"]


def test_test_grocy_connected_at_the_saved_address(monkeypatch):
    _mock_http(monkeypatch, {"http://grocy:80": _INFO})
    out = _body(asyncio.run(srouter.test_grocy(srouter.TestGrocyPayload(
        grocy_base_url="http://grocy:80", grocy_api_key="k"))))
    assert out == {"ok": True, "message": "Connected: Grocy 4.7.1"}


def test_status_is_bad_when_only_a_fallback_answers(monkeypatch):
    from app.routers.setup import system
    monkeypatch.setitem(settings.__dict__, "grocy_base_url", "http://127.0.0.1:9383")
    monkeypatch.setitem(settings.__dict__, "grocy_api_key", "k")
    _mock_http(monkeypatch, {"http://grocy:80": _INFO})
    probe = asyncio.run(system._grocy_status_probe())
    assert probe["ok"] is False
    assert "http://grocy:80" in probe["detail"]
    item = status_summary.build_summary({"grocy": probe})["items"]["grocy"]
    assert item["state"] == "bad"
    assert "http://grocy:80" in item["detail"]


def test_status_detail_defaults_when_nothing_answers():
    item = status_summary.map_service("grocy", "Grocy", True, False)
    assert item["detail"] == "Could not connect."


def test_first_run_route_returns_the_connected_address(monkeypatch):
    from app.services import first_run

    async def _provision(base):
        return {"ok": True, "configured": True, "message": "", "steps": []}
    monkeypatch.setattr(first_run, "provision_grocy", _provision)
    monkeypatch.setattr(first_run, "provisioning_in_progress", lambda: False)
    monkeypatch.setitem(settings.__dict__, "grocy_base_url", "http://grocy:80")
    monkeypatch.setattr(srouter, "_refuse_probe_target", lambda url: "")
    with patch.object(type(settings), "is_configured", lambda self: True):
        out = _body(asyncio.run(srouter.first_run_provision(None, "grocy", None)))
    assert out["base_url"] == "http://grocy:80"
