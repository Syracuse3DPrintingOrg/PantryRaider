"""Settings-page render path stays fast (FoodAssistant-0hwez).

GET /setup on a pi_hosted appliance used to stack slow device probes inside
the render: host-bridge calls (the Mealie docker compose ps, repeated
hostname asks), a sequential three-address Grocy sweep, and a blocking lpstat
on the event loop, all before a byte reached the browser. These tests pin the
fixes: the render makes NO bridge calls, the Mealie state is fetched by the
page after paint, the lpstat probe runs off the event loop with a short
timeout, the Grocy sweep probes its candidates concurrently, the bridge
hostname is memoized, and the page's scripts are deferred (or lazy-loaded)
instead of blocking the first paint.

Run: python -m pytest tests/test_setup_render_path.py -q
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402

TEMPLATES = _SERVICE / "app" / "templates"
STATIC_JS = _SERVICE / "app" / "static" / "js"


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _render(client, monkeypatch, *, mode: str, is_pi: bool) -> str:
    monkeypatch.setattr(settings, "deployment_mode", mode)
    with patch.object(type(settings), "is_configured", lambda self: True), \
         patch("app.services.readiness.gate_possible", return_value=False), \
         patch("app.routers.setup.is_raspberry_pi", return_value=is_pi), \
         patch("app.templating.is_raspberry_pi", return_value=is_pi):
        r = client.get("/setup")
    assert r.status_code == 200
    return r.text


# --- The render itself makes no slow device calls ---------------------------


def test_render_makes_no_bridge_calls_and_runs_lpstat_off_loop(client, monkeypatch):
    """On the pi_hosted shape (the worst case), GET /setup must not open a
    single bridge connection (no Mealie probe, nothing), and the print-stack
    probe must run in a worker thread, never on the event loop."""
    from app.routers import setup as setup_router

    bridge_calls: list = []

    def _counting_bridge_client(**kw):
        bridge_calls.append(kw)
        raise AssertionError(
            "bridge_client must not be used during the settings render")

    monkeypatch.setattr(setup_router, "bridge_client", _counting_bridge_client)

    lpstat_on_loop: list = []

    def _fake_printing_available() -> bool:
        try:
            asyncio.get_running_loop()
            lpstat_on_loop.append(True)
        except RuntimeError:
            lpstat_on_loop.append(False)
        return False

    monkeypatch.setattr(setup_router, "_printing_available",
                        _fake_printing_available)

    grocy_probes: list = []

    async def _fake_detect() -> str:
        grocy_probes.append(1)
        return ""

    monkeypatch.setattr(setup_router, "_detect_local_grocy", _fake_detect)

    html = _render(client, monkeypatch, mode="pi_hosted", is_pi=True)
    assert bridge_calls == []
    # The probe ran exactly once, and NOT on the event loop (run_in_threadpool).
    assert lpstat_on_loop == [False]
    # The Mealie running affordance ships hidden, and the page is told to ask
    # for the live state after paint.
    assert "data-mealie-running" in html
    assert "const MEALIE_STATUS_PROBE = true" in html


def test_satellite_render_does_not_ask_for_mealie(client, monkeypatch):
    monkeypatch.setattr(settings, "remote_server_url", "http://srv:9284",
                        raising=False)
    html = _render(client, monkeypatch, mode="pi_remote", is_pi=True)
    assert "const MEALIE_STATUS_PROBE = false" in html


# --- Grocy detection probes its candidates concurrently ----------------------


def test_detect_local_grocy_probes_candidates_concurrently(monkeypatch):
    """Every candidate's probe must be in flight at once: each fake response
    waits until all three probes have started, which deadlocks (and times
    out) under the old sequential sweep."""
    from app.routers import setup as setup_router

    started: set = set()
    all_started = asyncio.Event()

    class _FakeResponse:
        status_code = 404

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            started.add(url)
            if len(started) == len(setup_router._LOCAL_GROCY_CANDIDATES):
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=1.5)
            return _FakeResponse()

    monkeypatch.setattr(setup_router.httpx, "AsyncClient", _FakeClient)

    async def _run():
        return await asyncio.wait_for(setup_router._detect_local_grocy(),
                                      timeout=3.0)

    assert asyncio.run(_run()) == ""
    assert len(started) == len(setup_router._LOCAL_GROCY_CANDIDATES) == 3


def test_detect_local_grocy_keeps_candidate_priority(monkeypatch):
    """When more than one candidate answers, the list's order still decides."""
    from app.routers import setup as setup_router

    class _FakeResponse:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _FakeResponse()

    monkeypatch.setattr(setup_router.httpx, "AsyncClient", _FakeClient)
    got = asyncio.run(setup_router._detect_local_grocy())
    assert got == setup_router._LOCAL_GROCY_CANDIDATES[0]


# --- Bridge hostname memoization ---------------------------------------------


def test_bridge_hostname_memoized_and_invalidated(monkeypatch):
    from app import config, hardware
    import httpx

    monkeypatch.setattr(hardware, "is_raspberry_pi", lambda: True)
    calls: list = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"hostname": "pantry"}

    def _fake_get(url, timeout=None):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(httpx, "get", _fake_get)
    config.invalidate_hostname_cache()
    try:
        assert config._bridge_hostname() == "pantry"
        assert config._bridge_hostname() == "pantry"
        assert len(calls) == 1, "the second ask must come from the memo"
        config.invalidate_hostname_cache()
        assert config._bridge_hostname() == "pantry"
        assert len(calls) == 2, "invalidation must force a fresh ask"
    finally:
        config.invalidate_hostname_cache()


def test_bridge_hostname_memoizes_a_failure_too(monkeypatch):
    """A dead bridge must not cost its full timeout on every render call."""
    from app import config, hardware
    import httpx

    monkeypatch.setattr(hardware, "is_raspberry_pi", lambda: True)
    calls: list = []

    def _fake_get(url, timeout=None):
        calls.append(url)
        raise OSError("connection refused")

    monkeypatch.setattr(httpx, "get", _fake_get)
    config.invalidate_hostname_cache()
    try:
        assert config._bridge_hostname() == ""
        assert config._bridge_hostname() == ""
        assert len(calls) == 1
    finally:
        config.invalidate_hostname_cache()


# --- Script loading: defer on both templates, lazy panes, post-paint fetch ---


def _script_src_tags(source: str) -> list[str]:
    return [t for t in re.findall(r"<script\b[^>]*>", source) if 'src="' in t]


def test_setup_page_scripts_are_deferred_except_kiosk_display():
    """Every external script on the standalone settings page carries defer,
    except kiosk-display.js, which must run before the inline intro guard
    (it latches the kioskMode flag the guard reads, FoodAssistant-9dhh)."""
    src = (TEMPLATES / "setup.html").read_text()
    tags = _script_src_tags(src)
    assert tags, "setup.html lost its script tags?"
    for tag in tags:
        if "kiosk-display.js" in tag:
            assert "defer" not in tag, tag
        else:
            assert "defer" in tag, tag


def test_base_head_scripts_are_deferred_except_kiosk_display():
    """The 13 head scripts every base-derived page loads: kiosk-display.js
    stays synchronous (the intro guard depends on it), everything else is
    deferred so it no longer blocks first paint."""
    src = (TEMPLATES / "base.html").read_text()
    head = src[:src.index("</head>")]
    tags = _script_src_tags(head)
    names = [re.search(r'src="([^"?]+)', t).group(1) for t in tags]
    assert "static/js/kiosk-display.js" in names
    for tag in tags:
        if "kiosk-display.js" in tag:
            assert "defer" not in tag, tag
        else:
            assert "defer" in tag, tag
    # The inline consumers of the deferred shared poll wait for
    # DOMContentLoaded instead of checking at parse time.
    body = src[src.index("</head>"):]
    for block in re.findall(r"<script>(.*?)</script>", body, re.S):
        if "PRKioskStatus" in block:
            assert "DOMContentLoaded" in block, block[:200]


def test_heavy_pane_scripts_load_lazily():
    """The deck editor, label designer, and camera tools are not in the page's
    script list; they load on the first open of their pane."""
    setup_html = (TEMPLATES / "setup.html").read_text()
    for name in ("deck-editor.js", "label-designer.js", "cameras-ha.js"):
        assert f"static/js/setup/{name}" not in setup_html, name
    helpers = (STATIC_JS / "setup" / "helpers.js").read_text()
    assert "function loadPaneScript" in helpers
    menu = (STATIC_JS / "setup" / "menu.js").read_text()
    for pane, name in (("pane-start-page", "deck-editor.js"),
                       ("pane-printing", "label-designer.js"),
                       ("pane-connections", "cameras-ha.js"),
                       ("pane-home-assistant", "cameras-ha.js")):
        assert f"'{pane}': '{name}'" in menu, (pane, name)


def test_mealie_status_is_fetched_after_paint():
    """panes.js asks setup/mealie/status once the page is on screen and
    reveals the data-mealie-running affordances; the render never asks."""
    panes = (STATIC_JS / "setup" / "panes.js").read_text()
    assert "setup/mealie/status" in panes
    assert "document.addEventListener('DOMContentLoaded', loadMealieStatus)" in panes
    assert "data-mealie-running" in panes
    for name in ("_macros.html", "_wizard.html"):
        tpl = (TEMPLATES / "setup" / name).read_text()
        assert "data-mealie-running" in tpl, name
