"""The continuous recovery-hotspot watchdog (foodassistant-ap-watchdog).

The watchdog became a persistent loop: it re-checks connectivity every minute
for the whole uptime, raises the hotspot only after three consecutive failed
checks, holds off while a NetworkManager connection attempt is in flight, and
stands the hotspot down when connectivity returns. The debounce decision is a
pure bash function (ap_watchdog_decide), exercised here as a truth table by
sourcing the script with AP_WATCHDOG_NO_MAIN=1 (which skips the loop).

Run: python -m pytest tests/test_ap_watchdog.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WATCHDOG = REPO / "scripts" / "image-build" / "foodassistant-ap-watchdog"
UNIT = REPO / "scripts" / "image-build" / "foodassistant-ap-watchdog.service"


def decide(connected, connecting, ap_active, fails, fails_needed=3):
    """Run ap_watchdog_decide with the given inputs; return (fails, action)."""
    out = subprocess.run(
        ["bash", "-c",
         f'source "$WATCHDOG"; ap_watchdog_decide {connected} {connecting} '
         f"{ap_active} {fails}"],
        env={**os.environ, "WATCHDOG": str(WATCHDOG),
             "AP_WATCHDOG_NO_MAIN": "1", "FAILS_NEEDED": str(fails_needed)},
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    new_fails, action = out.stdout.split()
    return int(new_fails), action


def test_debounce_truth_table():
    # (connected, connecting, ap_active, fails) -> (new_fails, action)
    cases = [
        # Three consecutive failures raise; the first two only count up.
        ((0, 0, 0, 0), (1, "none")),
        ((0, 0, 0, 1), (2, "none")),
        ((0, 0, 0, 2), (0, "raise")),
        # Recovery mid-count resets the counter (a rebooting router that came
        # back after two bad checks starts the debounce over, never carries it).
        ((1, 0, 0, 1), (0, "none")),
        ((1, 0, 0, 2), (0, "none")),
        # A connection attempt in flight holds the count and never raises,
        # even on what would otherwise be the raising check.
        ((0, 1, 0, 0), (0, "none")),
        ((0, 1, 0, 2), (2, "none")),
        # Connectivity returning while the AP is up stands it down.
        ((1, 0, 1, 0), (0, "standdown")),
        ((1, 0, 1, 2), (0, "standdown")),
        # AP already up and still no network: nothing to do, no re-raise.
        ((0, 0, 1, 0), (0, "none")),
        ((0, 0, 1, 2), (2, "none")),
        # Connected with the AP down: steady state, count stays zero.
        ((1, 0, 0, 0), (0, "none")),
    ]
    for args, want in cases:
        assert decide(*args) == want, args


def test_debounce_threshold_is_configurable():
    # FAILS_NEEDED=1 raises on the very first failed check (used to shorten
    # the window in bench testing); the default of 3 is asserted separately.
    assert decide(0, 0, 0, 0, fails_needed=1) == (0, "raise")
    assert decide(0, 0, 0, 0, fails_needed=5) == (1, "none")


def test_full_cycle_walks_up_raises_and_stands_down():
    """Drive the pure function through a realistic outage: three bad checks
    raise, the broadcasting state holds, then the network returning stands
    the hotspot down and resets the count."""
    fails, ap = 0, 0
    for _ in range(2):
        fails, action = decide(0, 0, ap, fails)
        assert action == "none"
    fails, action = decide(0, 0, ap, fails)
    assert action == "raise"
    ap = 1
    fails, action = decide(0, 0, ap, fails)
    assert action == "none"
    fails, action = decide(1, 0, ap, fails)
    assert action == "standdown"
    assert fails == 0


def test_watchdog_is_a_continuous_loop_with_the_same_criteria():
    """Source-guard: the loop shape and the connectivity criteria the one-shot
    used (wlan association, default route, wired IPv4) must all be present,
    plus the nmcli hold-off and the flag-guarded hostapd start."""
    text = WATCHDOG.read_text()
    assert "while :; do" in text
    assert 'sleep "$CHECK_INTERVAL"' in text
    assert "iw dev wlan0 link" in text
    assert "ip route show default" in text
    assert "/sys/class/net" in text
    assert 'grep -q ":connecting"' in text
    assert "systemctl is-active --quiet hostapd" in text
    # The stand-down mirrors the bridge's _ap_stand_down.
    assert "systemctl stop hostapd" in text
    assert "ip addr del 192.168.99.1/24" in text
    # Defaults: a one-minute check interval, a three-check debounce.
    assert 'CHECK_INTERVAL="${CHECK_INTERVAL:-60}"' in text
    assert 'FAILS_NEEDED="${FAILS_NEEDED:-3}"' in text


def test_watchdog_unit_is_a_restarting_service():
    """The repo unit file must run the loop as a plain always-restarted
    service; a oneshot unit would run the loop once and block the boot
    target. Name and install path are internal identifiers and stay."""
    text = UNIT.read_text()
    assert "Type=simple" in text
    assert "Restart=always" in text
    assert "ExecStart=/usr/local/sbin/foodassistant-ap-watchdog" in text
    assert "Type=oneshot" not in text
    assert "RemainAfterExit" not in text


def test_firstboot_installs_the_repo_unit():
    """firstboot must install the shipped unit file (asset dir or checkout),
    not write its own oneshot heredoc anymore."""
    text = (REPO / "scripts" / "image-build" / "firstboot.sh").read_text()
    assert "foodassistant-ap-watchdog.service" in text
    body = text[text.index("configure_wifi_ap_fallback()"):]
    body = body[:body.index("\n}\n") + 3]
    assert "Type=oneshot" not in body
    assert '"$ASSET_DIR/foodassistant-ap-watchdog.service"' in body
