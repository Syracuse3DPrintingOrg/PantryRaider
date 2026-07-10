"""Tests for the Pi device-health toast decision (FoodAssistant-h28s).

The interesting logic is the pure edge-trigger/de-dup in
services/pi_health.warnings_to_toast plus the user-forward copy mapping. These
run with no Pi, no host bridge, and no network.

Run: python -m pytest tests/test_pi_health.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import pi_health  # noqa: E402
from app.services import ha_events  # noqa: E402


def _live(key):
    return {"key": key, "message": key, "live": True}


def _sticky(key):
    return {"key": key, "message": key, "live": False}


# -- pure edge-trigger / de-dup ---------------------------------------------


def test_new_live_key_toasts():
    toasts, active = pi_health.warnings_to_toast(set(), [_live("undervoltage")])
    assert [w["key"] for w in toasts] == ["undervoltage"]
    assert active == {"undervoltage"}


def test_same_active_key_next_poll_does_not_retoast():
    # It was live last poll (in prev_active) and is still live: no new toast,
    # but it stays in the active set so it is remembered.
    toasts, active = pi_health.warnings_to_toast(
        {"undervoltage"}, [_live("undervoltage")])
    assert toasts == []
    assert active == {"undervoltage"}


def test_cleared_then_recurs_toasts_again():
    # Poll 1: onset.
    _, active = pi_health.warnings_to_toast(set(), [_live("undervoltage")])
    # Poll 2: cleared (nothing live) -> drops out of the active set.
    toasts, active = pi_health.warnings_to_toast(active, [])
    assert toasts == [] and active == set()
    # Poll 3: recurs -> toasts again because it is no longer remembered as live.
    toasts, active = pi_health.warnings_to_toast(active, [_live("undervoltage")])
    assert [w["key"] for w in toasts] == ["undervoltage"]


def test_since_boot_only_never_toasts():
    # A condition that happened earlier but is not live now: informational only,
    # it stays on the nav icon / status page and is never toasted, and never
    # enters the active set (so it cannot suppress a later live onset either).
    toasts, active = pi_health.warnings_to_toast(set(), [_sticky("undervoltage")])
    assert toasts == [] and active == set()


def test_multiple_new_live_keys_each_toast_once():
    warnings = [_live("undervoltage"), _live("temperature"), _live("disk")]
    toasts, active = pi_health.warnings_to_toast(set(), warnings)
    assert [w["key"] for w in toasts] == ["undervoltage", "temperature", "disk"]
    assert active == {"undervoltage", "temperature", "disk"}


def test_only_the_new_key_toasts_when_one_was_already_active():
    toasts, active = pi_health.warnings_to_toast(
        {"undervoltage"}, [_live("undervoltage"), _live("temperature")])
    assert [w["key"] for w in toasts] == ["temperature"]
    assert active == {"undervoltage", "temperature"}


def test_bad_entries_are_ignored():
    warnings = ["nope", {"live": True}, {"key": "", "live": True}, _live("disk")]
    toasts, active = pi_health.warnings_to_toast(set(), warnings)
    assert [w["key"] for w in toasts] == ["disk"]
    assert active == {"disk"}


# -- level + copy ------------------------------------------------------------


def test_warning_level_error_for_faults_else_warning():
    assert pi_health.warning_level("undervoltage") == "error"
    assert pi_health.warning_level("temperature") == "error"
    assert pi_health.warning_level("temp_limit") == "error"
    assert pi_health.warning_level("throttled") == "warning"
    assert pi_health.warning_level("disk") == "warning"
    assert pi_health.warning_level("unknown") == "warning"


def test_warning_pane_targets_network_for_known_keys():
    # Every device-health key surfaces on the Network pane's warning banner
    # (templates/setup/_pane_network.html), so a toast can deep-link straight
    # there instead of the generic /setup landing page (FoodAssistant-44f6).
    for key in ("undervoltage", "throttled", "freq_capped", "temp_limit",
                "temperature", "disk"):
        assert pi_health.warning_pane(key) == "pane-network"


def test_warning_pane_falls_back_to_network_for_unknown_key():
    assert pi_health.warning_pane("future_thing") == "pane-network"


def test_toast_copy_is_user_forward_for_known_keys():
    title, message = pi_health.warning_toast_copy({"key": "undervoltage"})
    assert title == "Power warning"
    assert "power supply" in message.lower()
    assert "—" not in message  # no em-dash (AGENTS.md style)


def test_toast_copy_falls_back_to_bridge_message_for_unknown_key():
    title, message = pi_health.warning_toast_copy(
        {"key": "future_thing", "message": "Something new happened"})
    assert title == "Device warning"
    assert message == "Something new happened"


# -- poll_and_toast wiring ---------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    pi_health.reset()
    ha_events.reset()
    yield
    pi_health.reset()
    ha_events.reset()


def test_poll_and_toast_queues_on_onset_only():
    import asyncio

    async def fetch():
        return [_live("undervoltage")]

    # First poll: onset, one toast queued on the shared ring.
    assert asyncio.run(pi_health.poll_and_toast(fetch)) == 1
    ev = ha_events.poll(0)["events"][-1]
    assert ev["type"] == "warning" and ev["key"] == "undervoltage"
    # The toast carries the pane the fix lives on, so the client can deep-link.
    assert ev["pane"] == "pane-network"
    # Second poll, condition still live: no new toast.
    assert asyncio.run(pi_health.poll_and_toast(fetch)) == 0


def test_poll_and_toast_fail_soft_on_bad_feed():
    import asyncio

    async def none_fetch():
        return None

    async def boom_fetch():
        raise RuntimeError("bridge down")

    assert asyncio.run(pi_health.poll_and_toast(none_fetch)) == 0
    assert asyncio.run(pi_health.poll_and_toast(boom_fetch)) == 0
    assert ha_events.poll(0)["events"] == []
