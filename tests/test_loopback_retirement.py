"""The hourly rule that ends the old trust for programs on this device (I-4)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import settings
from app.services import access, auth_state, local_agents

T0 = 1_800_000_000.0
DAY = 86400.0
SERVICE = Path(__file__).resolve().parents[1] / "service"


@pytest.fixture
def items(monkeypatch):
    seen = []
    monkeypatch.setattr(access, "raise_item",
                        lambda kind, title, body, dedupe, level="info": seen.append(kind))
    return seen


@pytest.fixture
def legacy():
    auth_state.reset(auth_state.test_state(now=T0 - 30 * DAY), persist=False)


@pytest.fixture
def with_bridge(bridge_token):
    return bridge_token


def _hw(kiosk=True, deck=True, gadgets=True):
    hw = {"kiosk": {"installed": kiosk}, "streamdeck": {"installed": deck}}
    if gadgets is not None:
        hw["gadgets"] = {"installed": gadgets}
    return hw


def _prove_all(now):
    auth_state.record_ticket_redeemed(now=now)
    auth_state.record_agent("streamdeck", caps={"navorigin1"}, now=now)
    auth_state.record_agent("gadgets", now=now)


def test_no_bridge_retires_once_nothing_relied_on_trust(legacy, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    auth_state.record_legacy_use("browser", now=T0 - 3 * DAY)
    assert local_agents.evaluate_retirement(T0, None) == "kept"
    assert auth_state.legacy_trust_active(now=T0)
    assert local_agents.evaluate_retirement(T0 + 5 * DAY, None) == "retired"
    assert auth_state.loopback_trust()["by"] == "auto"


def test_a_silent_bridge_keeps_trust(legacy, with_bridge):
    _prove_all(T0)
    assert local_agents.evaluate_retirement(T0, None) == "kept"
    assert auth_state.legacy_trust_active(now=T0)


def test_every_agent_proven_and_nothing_else_retires(legacy, with_bridge):
    _prove_all(T0)
    assert local_agents.evaluate_retirement(T0 + 60, _hw()) == "retired"
    assert not auth_state.legacy_trust_active(now=T0 + 120)


@pytest.mark.parametrize("missing", ["screen", "deck", "gadgets"])
def test_an_unproven_agent_keeps_trust(legacy, with_bridge, missing):
    if missing != "screen":
        auth_state.record_ticket_redeemed(now=T0)
    if missing != "deck":
        auth_state.record_agent("streamdeck", caps={"navorigin1"}, now=T0)
    if missing != "gadgets":
        auth_state.record_agent("gadgets", now=T0)
    assert local_agents.evaluate_retirement(T0 + 60, _hw()) == "kept"


def test_proofs_expire(legacy, with_bridge):
    _prove_all(T0)
    # The reader must have used its key in the last day.
    assert local_agents.evaluate_retirement(T0 + 2 * DAY, _hw()) == "kept"
    assert local_agents.evaluate_retirement(T0 + 2 * DAY, _hw(gadgets=False)) == "retired"


def test_a_deck_without_navorigin_is_not_proven(legacy, with_bridge):
    auth_state.record_ticket_redeemed(now=T0)
    auth_state.record_agent("streamdeck", caps=set(), now=T0)
    auth_state.record_agent("gadgets", now=T0)
    assert local_agents.evaluate_retirement(T0 + 60, _hw()) == "kept"


def test_a_bridge_answer_without_gadgets_fails_rule_a(legacy, with_bridge):
    _prove_all(T0)
    assert local_agents.evaluate_retirement(T0 + 60, _hw(gadgets=None)) == "kept"


def test_uninstalled_agents_need_no_proof(legacy, with_bridge):
    assert local_agents.evaluate_retirement(
        T0, _hw(kiosk=False, deck=False, gadgets=False)) == "retired"


def test_recent_legacy_use_keeps_trust(legacy, with_bridge):
    _prove_all(T0)
    auth_state.record_legacy_use("deck", now=T0 - 2 * DAY)
    assert local_agents.evaluate_retirement(T0, _hw()) == "kept"
    assert local_agents.evaluate_retirement(T0 + 6 * DAY, _hw(gadgets=False)) == "retired"


def test_same_host_proxy_use_never_holds_trust_open(legacy, with_bridge):
    _prove_all(T0)
    auth_state.record_legacy_use("same_host_proxy", now=T0)
    assert local_agents.evaluate_retirement(T0 + 60, _hw()) == "retired"


def test_other_local_callers_ask_for_confirmation(legacy, with_bridge, items):
    _prove_all(T0)
    auth_state.record_legacy_use("other", now=T0 - 10 * DAY)
    assert local_agents.evaluate_retirement(T0, _hw()) == "confirm"
    assert auth_state.loopback_trust()["confirm_needed"] is True
    assert items == ["local_trust_confirm"]
    assert local_agents.evaluate_retirement(T0 + 60, _hw()) == "confirm"
    assert items == ["local_trust_confirm"]                      # one item
    assert auth_state.legacy_trust_active(now=T0 + 60)


def test_retired_trust_never_reopens_on_its_own(legacy, with_bridge):
    auth_state.retire("admin", now=T0)
    auth_state.record_legacy_use("browser", now=T0 + 1)
    assert local_agents.evaluate_retirement(T0 + 2, _hw()) == "retired"
    assert not auth_state.legacy_trust_active(now=T0 + 3)


def test_only_an_owner_can_trust_again():
    """Nothing but the Security pane's retrust action calls retrust(), and no
    code path writes the legacy state outside the upgrade default."""
    root = SERVICE / "app"
    callers = []
    for f in root.rglob("*.py"):
        text = f.read_text()
        if "auth_state.retrust(" in text:
            callers.append(f.relative_to(root).as_posix())
    assert callers == ["routers/security.py"]
    src = (root / "services" / "auth_state.py").read_text()
    writes = re.findall(r'"state":\s*"legacy"', src)
    assert len(writes) == 1, writes
    assert '"state": "legacy", "since": start, "by": "auto"' in src


def test_a_new_install_is_watched_for_a_week_first(tmp_path, monkeypatch):
    """Uses are only recorded from 0.21 on, so on the day of the update an
    empty record proves nothing: a program that calls once a day keeps trust
    until it has been watched for a week."""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    auth_state.reset(auth_state.test_state(now=T0), persist=False)
    assert local_agents.evaluate_retirement(T0 + 300, None) == "kept"
    assert local_agents.evaluate_retirement(T0 + 6 * DAY, None) == "kept"
    assert auth_state.legacy_trust_active(now=T0 + 6 * DAY)
    assert local_agents.evaluate_retirement(T0 + 7 * DAY + 60, None) == "retired"
