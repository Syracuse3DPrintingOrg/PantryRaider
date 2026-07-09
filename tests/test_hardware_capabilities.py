"""Tests for Raspberry Pi capability detection (app.hardware).

Pure logic: the board model is forced via FOODASSISTANT_FORCE_MODEL and the RAM
total via FOODASSISTANT_FORCE_RAM_MB, so the tests run anywhere with no Pi and
no /proc present. Covers the board tier classifier, the RAM reader, and the
supports_local_stack() decision including its conservative unknown fallback.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from app import hardware  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_caches():
    hardware.is_raspberry_pi.cache_clear()
    hardware.board_model.cache_clear()
    yield
    hardware.is_raspberry_pi.cache_clear()
    hardware.board_model.cache_clear()


# board_tier -----------------------------------------------------------------

@pytest.mark.parametrize("model", [
    "Raspberry Pi 3 Model B Plus Rev 1.3",
    "Raspberry Pi 2 Model B",
    "Raspberry Pi Zero 2 W",
])
def test_low_tier_boards(model):
    assert hardware.board_tier(model) == "low"


@pytest.mark.parametrize("model", [
    "Raspberry Pi 4 Model B Rev 1.4",
    "Raspberry Pi 5 Model B Rev 1.0",
])
def test_capable_tier_boards(model):
    assert hardware.board_tier(model) == "capable"


@pytest.mark.parametrize("model", ["Generic x86-64 PC", ""])
def test_non_pi_tier_unknown(model):
    assert hardware.board_tier(model) == "unknown"


def test_unnumbered_pi_is_unknown_not_overrestricted():
    # The original Pi 1 reports "Raspberry Pi Model B" with no family number.
    # We classify it unknown rather than low so the safe fallback (offer the
    # normal set) wins; RAM still gates it on the real board.
    assert hardware.board_tier("Raspberry Pi Model B Rev 2") == "unknown"


# total_ram_mb ---------------------------------------------------------------

def test_forced_ram_parsed(monkeypatch):
    monkeypatch.setenv("FOODASSISTANT_FORCE_RAM_MB", "3884")
    assert hardware.total_ram_mb() == 3884


def test_forced_ram_blank_is_unknown(monkeypatch):
    monkeypatch.setenv("FOODASSISTANT_FORCE_RAM_MB", "")
    assert hardware.total_ram_mb() is None


# supports_local_stack -------------------------------------------------------

def test_low_tier_blocks_local_stack():
    # A Pi 3 is refused regardless of any RAM reading.
    assert hardware.supports_local_stack("Raspberry Pi 3 Model B", ram_mb=8000) is False


def test_capable_board_with_enough_ram_supports():
    assert hardware.supports_local_stack("Raspberry Pi 4 Model B", ram_mb=4096) is True


def test_capable_board_low_ram_blocked():
    # Even a Pi 4 with only 2 GB is below the hosted threshold.
    assert hardware.supports_local_stack("Raspberry Pi 4 Model B", ram_mb=2048) is False


def test_unknown_detection_falls_back_to_supported():
    # No model and no RAM reading: do not over-restrict.
    assert hardware.supports_local_stack("", ram_mb=None) is True


def test_capable_board_unknown_ram_supported():
    # Capable tier with an unreadable RAM total still passes.
    assert hardware.supports_local_stack("Raspberry Pi 5 Model B", ram_mb=None) is True
