"""First-boot readiness gate (FoodAssistant-0m61).

On a freshly flashed Pi appliance the app serves minutes before the co-hosted
Grocy does, so unconfigured navigation is steered to /ui/getting-ready until
the inventory is CONNECTED. These tests pin the whole matrix: the gate engages
only on an unconfigured pi_hosted install with no inventory key, holds while
provisioning is still working (including after Grocy's first HTTP answer, which
is the middle of the job, not the end), hands off when the key lands or when
provisioning reports it has stopped trying, honours the user's dismissal, obeys
the backstop deadline, and can never reappear once released.

Also covers the two default changes shipped with the same bead: the
barcode auto-check default flip and the tri-state AI shelf-life toggle.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import readiness  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_readiness():
    readiness.reset()
    yield
    readiness.reset()


def _fresh_pi(monkeypatch, tmp_path):
    """Settings of a just-flashed pi_hosted appliance mid first boot: the
    seeded mode and Grocy address, no password, no API key."""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "pi_hosted", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://localhost:9383", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)


async def _never_answers():
    return False


async def _answers():
    readiness.mark_answered()
    return True


# -- pure gate rules ---------------------------------------------------------

def test_gate_possible_only_on_fresh_pi_hosted(monkeypatch, tmp_path):
    _fresh_pi(monkeypatch, tmp_path)
    assert readiness.gate_possible() is True
    # A server install never gates, even fresh.
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    assert readiness.gate_possible() is False
    # A satellite never gates.
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    assert readiness.gate_possible() is False


def test_gate_impossible_once_configured_or_connected(monkeypatch, tmp_path):
    _fresh_pi(monkeypatch, tmp_path)
    # A saved Grocy key (first-run provisioning finished) ends the gate.
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    assert readiness.gate_possible() is False
    # A completed setup (password saved) ends it too.
    monkeypatch.setattr(settings, "grocy_api_key", "", raising=False)
    monkeypatch.setattr(settings, "auth_password", "hash", raising=False)
    assert settings.is_configured() is True
    assert readiness.gate_possible() is False


@pytest.mark.anyio
async def test_gate_active_while_grocy_silent(monkeypatch, tmp_path):
    _fresh_pi(monkeypatch, tmp_path)
    monkeypatch.setattr(readiness, "grocy_answering", _never_answers)
    assert await readiness.gate_active() is True


@pytest.mark.anyio
async def test_gate_holds_after_grocy_answers_until_provisioning_lands(monkeypatch, tmp_path):
    """Grocy answering is the start of provisioning, not the end (Dan,
    2026-07-16). Releasing here is what handed the user a wizard whose
    inventory pane was empty."""
    _fresh_pi(monkeypatch, tmp_path)
    monkeypatch.setattr(readiness, "grocy_answering", _answers)
    assert await readiness.gate_active() is True
    # The key landing is what ends it (gate_possible goes false).
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    assert await readiness.gate_active() is False


@pytest.mark.anyio
async def test_provisioning_done_lifts_the_gate_for_good(monkeypatch, tmp_path):
    """A Grocy that is already someone's own connects no key, so waiting on one
    would be a lie: first_run says it has stopped, and the gate opens."""
    _fresh_pi(monkeypatch, tmp_path)
    monkeypatch.setattr(readiness, "grocy_answering", _answers)
    readiness.mark_provisioning_done()
    assert await readiness.gate_active() is False
    # And the choice survives a process restart (state file).
    readiness.reset()
    assert await readiness.gate_active() is False


@pytest.mark.anyio
async def test_gate_releases_after_the_backstop_deadline(monkeypatch, tmp_path):
    """A provisioner that dies without reporting must not wall the user out."""
    _fresh_pi(monkeypatch, tmp_path)
    monkeypatch.setattr(readiness, "grocy_answering", _never_answers)
    assert await readiness.gate_active() is True
    # Pretend the gate has been holding since well before the deadline.
    readiness._state["first_seen"] = time.time() - readiness._GATE_MAX_WAIT - 1
    assert await readiness.gate_active() is False


@pytest.mark.anyio
async def test_backwards_clock_does_not_freeze_the_deadline(monkeypatch, tmp_path):
    """A Pi has no RTC, so NTP can shove the clock backwards mid first boot."""
    _fresh_pi(monkeypatch, tmp_path)
    monkeypatch.setattr(readiness, "grocy_answering", _never_answers)
    assert await readiness.gate_active() is True
    readiness._state["first_seen"] = time.time() + 60 * 60   # clock jumped back
    assert await readiness.gate_active() is True             # still gated
    assert readiness._state["first_seen"] <= time.time()     # and re-based


@pytest.mark.anyio
async def test_dismiss_is_sticky(monkeypatch, tmp_path):
    _fresh_pi(monkeypatch, tmp_path)
    monkeypatch.setattr(readiness, "grocy_answering", _never_answers)
    readiness.dismiss()
    assert await readiness.gate_active() is False
    readiness.reset()  # a new worker reads the same choice from the file
    assert await readiness.gate_active() is False


@pytest.mark.anyio
async def test_status_shape(monkeypatch, tmp_path):
    _fresh_pi(monkeypatch, tmp_path)
    monkeypatch.setattr(readiness, "grocy_answering", _never_answers)
    st = await readiness.status()
    assert st["ok"] is True and st["ready"] is False
    states = [s["state"] for s in st["steps"]]
    assert states[0] == "done" and "working" in states
    # Grocy answering advances the progress display but does not hand off:
    # the inventory is not connected yet.
    monkeypatch.setattr(readiness, "grocy_answering", _answers)
    st = await readiness.status()
    assert st["grocy_serving"] is True
    assert st["grocy_connected"] is False
    assert st["ready"] is False
    assert st["steps"][1]["state"] == "done"    # inventory service is up
    assert st["steps"][2]["state"] == "working"  # connecting it is the live step
    # The key landing is what makes it ready.
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    st = await readiness.status()
    assert st["ready"] is True and st["grocy_connected"] is True
    assert [s["state"] for s in st["steps"]] == ["done", "done", "done"]


# -- middleware / routes ------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    _fresh_pi(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def test_setup_redirects_to_getting_ready_while_gated(client, monkeypatch):
    monkeypatch.setattr(readiness, "grocy_answering", _never_answers)
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/ui/getting-ready" in r.headers["location"]
    # The kiosk latch rides along.
    r = client.get("/setup?kiosk=1", follow_redirects=False)
    assert "kiosk=1" in r.headers["location"]


def test_getting_ready_page_serves_while_gated(client, monkeypatch):
    monkeypatch.setattr(readiness, "grocy_answering", _never_answers)
    r = client.get("/ui/getting-ready")
    assert r.status_code == 200
    assert "getting ready" in r.text.lower()
    assert "Continue to setup without waiting" in r.text
    s = client.get("/ui/getting-ready/status").json()
    assert s["ok"] is True and s["ready"] is False


def test_getting_ready_holds_while_grocy_only_answers(client, monkeypatch):
    """The wizard must not open on the first HTTP answer: provisioning is still
    creating the key that the wizard's inventory pane shows."""
    monkeypatch.setattr(readiness, "grocy_answering", _answers)
    s = client.get("/ui/getting-ready/status").json()
    assert s["ready"] is False
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/ui/getting-ready" in r.headers["location"]


def test_getting_ready_hands_off_when_the_inventory_connects(client, monkeypatch):
    monkeypatch.setattr(readiness, "grocy_answering", _answers)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    # The poll reports ready, and the page itself bounces to the wizard.
    s = client.get("/ui/getting-ready/status").json()
    assert s["ready"] is True
    r = client.get("/ui/getting-ready", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"].endswith("/setup")
    # The wizard now serves instead of bouncing back.
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code == 200


def test_getting_ready_hands_off_when_provisioning_gives_up(client, monkeypatch):
    """Provisioning that cannot connect this Grocy releases the user to the
    wizard rather than leaving them on a progress page forever."""
    monkeypatch.setattr(readiness, "grocy_answering", _never_answers)
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code in (302, 303, 307)     # gated while it tries
    readiness.mark_provisioning_done()
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code == 200                 # and released when it stops


def test_skip_ready_dismisses_the_gate(client, monkeypatch):
    monkeypatch.setattr(readiness, "grocy_answering", _never_answers)
    r = client.get("/setup?skip_ready=1", follow_redirects=False)
    assert r.status_code == 200          # the wizard serves immediately
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code == 200          # and the dismissal sticks


def test_never_gates_on_server_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "", raising=False)
    monkeypatch.setattr(readiness, "grocy_answering", _never_answers)
    r = client.get("/setup", follow_redirects=False)
    assert r.status_code == 200
    r = client.get("/ui/getting-ready", follow_redirects=False)
    assert r.status_code in (302, 303, 307)


def test_never_gates_once_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(readiness, "grocy_answering", _never_answers)
    assert settings.is_configured() is True
    # A configured install never sees the readiness page.
    r = client.get("/ui/getting-ready", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/setup" in r.headers["location"]


def test_unconfigured_ui_still_lands_on_getting_ready(client, monkeypatch):
    monkeypatch.setattr(readiness, "grocy_answering", _never_answers)
    # /ui redirects to /setup (existing behaviour), which then gates.
    r = client.get("/ui/", follow_redirects=True)
    assert r.status_code == 200
    assert "getting ready" in r.text.lower()


# -- default changes shipped with the same bead --------------------------------

def test_barcode_autocheck_default_is_on():
    from app.config import Settings
    assert Settings.model_fields["barcode_autocheck_shopping"].default is True


def test_llm_expiry_effective_matrix(monkeypatch):
    # Never chosen: follows whether AI is configured.
    monkeypatch.setattr(settings, "llm_expiry_enabled", None, raising=False)
    monkeypatch.setattr(settings, "vision_provider", "gemini", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "", raising=False)
    monkeypatch.setattr(settings, "enrich_provider", "", raising=False)
    assert settings.llm_expiry_effective() is False
    monkeypatch.setattr(settings, "gemini_api_key", "k", raising=False)
    assert settings.llm_expiry_effective() is True
    # Forager counts as configured AI.
    monkeypatch.setattr(settings, "gemini_api_key", "", raising=False)
    monkeypatch.setattr(settings, "vision_provider", "cloud", raising=False)
    monkeypatch.setattr(settings, "cloud_instance_token", "tok", raising=False)
    assert settings.llm_expiry_effective() is True
    # An explicit user choice always wins.
    monkeypatch.setattr(settings, "llm_expiry_enabled", False, raising=False)
    assert settings.llm_expiry_effective() is False
    monkeypatch.setattr(settings, "llm_expiry_enabled", True, raising=False)
    assert settings.llm_expiry_effective() is True
