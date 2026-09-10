"""Time-to-target ready estimate and the demo sample thermometer.

Covers the pure predictor (FoodAssistant-1d1g): a rising probe gets a sane
estimate, a flat or cooling probe gets none, an already-past probe gets none,
and noisy readings do not blow the estimate up. Also the deterministic demo
sample grill (FoodAssistant-qqcq): same clock, same device, with a target so a
ready-in estimate is produced. Pure logic, no hardware or running app.
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import gadgets  # noqa: E402


# -- estimate_ready_seconds ---------------------------------------------------

def _rising(start_temp, rate_c_per_s, n=6, step=10.0, t0=1000.0):
    """A clean rising history: n samples, `step` seconds apart, climbing at
    rate_c_per_s Celsius/second from start_temp."""
    return [(t0 + i * step, start_temp + rate_c_per_s * (i * step))
            for i in range(n)]


def test_rising_probe_gets_a_sane_estimate():
    # Climbing 0.05 C/s from 40 C, target 63 C: latest sample is 40+0.05*50=42.5,
    # remaining 20.5 C at 0.05 C/s -> about 410 seconds.
    hist = _rising(40.0, 0.05)
    est = gadgets.estimate_ready_seconds(hist, 63.0, "above")
    assert est is not None
    assert 350 <= est <= 470


def test_flat_probe_returns_none():
    hist = [(1000.0 + i * 10, 50.0) for i in range(6)]
    assert gadgets.estimate_ready_seconds(hist, 63.0, "above") is None


def test_cooling_probe_toward_higher_target_returns_none():
    # Temperature falling while the target is above it: never arrives.
    hist = _rising(60.0, -0.05)
    assert gadgets.estimate_ready_seconds(hist, 74.0, "above") is None


def test_already_past_target_returns_none():
    hist = _rising(70.0, 0.05)
    assert gadgets.estimate_ready_seconds(hist, 63.0, "above") is None


def test_too_few_samples_returns_none():
    assert gadgets.estimate_ready_seconds([(1000.0, 50.0)], 63.0, "above") is None
    assert gadgets.estimate_ready_seconds([], 63.0, "above") is None


def test_noisy_data_does_not_explode():
    # Alternating up/down jumps with no net trend: the estimate must stay None
    # or, if it resolves at all, a finite sane number, never a runaway value.
    hist = [(1000.0, 50.0), (1010.0, 58.0), (1020.0, 51.0),
            (1030.0, 59.0), (1040.0, 50.5), (1050.0, 58.5)]
    est = gadgets.estimate_ready_seconds(hist, 63.0, "above")
    assert est is None or 0 < est <= gadgets._MAX_ESTIMATE_SECONDS


def test_barely_rising_beyond_horizon_returns_none():
    # A microscopic climb would project days out: not useful, so None.
    hist = _rising(20.0, 0.0002)
    assert gadgets.estimate_ready_seconds(hist, 63.0, "above") is None


def test_chilling_cook_below_direction():
    # Cooling from 20 C down toward a 4 C fridge target at 0.02 C/s:
    # latest 20-0.02*50=19, remaining 15 C -> about 750 s.
    hist = _rising(20.0, -0.02)
    est = gadgets.estimate_ready_seconds(hist, 4.0, "below")
    assert est is not None
    assert 600 <= est <= 900


def test_duplicate_timestamps_do_not_divide_by_zero():
    hist = [(1000.0, 40.0), (1000.0, 41.0), (1010.0, 45.0), (1020.0, 50.0)]
    est = gadgets.estimate_ready_seconds(hist, 63.0, "above")
    assert est is None or est > 0


def test_out_of_order_samples_are_sorted():
    hist = list(reversed(_rising(40.0, 0.05)))
    est = gadgets.estimate_ready_seconds(hist, 63.0, "above")
    assert est is not None and est > 0


# -- demo_sample_device -------------------------------------------------------

def test_demo_sample_is_deterministic_for_a_fixed_clock():
    a = gadgets.demo_sample_device(1_700_000_000.0)
    b = gadgets.demo_sample_device(1_700_000_000.0)
    assert a == b


def test_demo_sample_shape_and_target_yields_estimate():
    dev = gadgets.demo_sample_device(1_700_000_123.0)
    assert dev["id"] == gadgets.DEMO_DEVICE_ID
    assert dev["demo"] is True
    assert "demo" in dev["name"].lower()
    assert len(dev["probes"]) == 2
    food = dev["probes"][0]
    ambient = dev["probes"][1]
    # The food probe carries a target below its current reading direction, so a
    # ready-in estimate is produced.
    assert food["target_c"] == 63.0
    assert food["temp_c"] < food["target_c"]
    assert food["ready_in_seconds"] is not None
    assert food["ready_in_seconds"] > 0
    # The ambient probe has no target and thus no estimate.
    assert ambient["target_c"] is None
    assert ambient["ready_in_seconds"] is None
    assert ambient["role"] == "ambient"


def test_demo_sample_drifts_over_time():
    early = gadgets.demo_sample_device(1_700_000_000.0)
    later = gadgets.demo_sample_device(1_700_000_400.0)  # 400 s later
    # Something visibly changes so the card reads as live.
    assert early["probes"][0]["temp_c"] != later["probes"][0]["temp_c"] \
        or early["probes"][1]["temp_c"] != later["probes"][1]["temp_c"]
