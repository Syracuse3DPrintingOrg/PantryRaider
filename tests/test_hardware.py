"""Tests for host hardware detection (app.hardware).

Pure logic: the model string is forced via FOODASSISTANT_FORCE_MODEL so the
tests run anywhere, with no Raspberry Pi and no /proc/device-tree present.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from app import hardware  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_caches():
    # is_raspberry_pi / board_model are lru_cached; reset around each test so a
    # forced model from one test does not leak into the next.
    hardware.is_raspberry_pi.cache_clear()
    hardware.board_model.cache_clear()
    yield
    hardware.is_raspberry_pi.cache_clear()
    hardware.board_model.cache_clear()


def test_pi_model_detected(monkeypatch):
    monkeypatch.setenv("FOODASSISTANT_FORCE_MODEL", "Raspberry Pi 5 Model B Rev 1.0")
    assert hardware.is_raspberry_pi() is True
    assert "Raspberry Pi 5" in hardware.board_model()


def test_pi_zero_detected(monkeypatch):
    monkeypatch.setenv("FOODASSISTANT_FORCE_MODEL", "Raspberry Pi 3 Model B Plus Rev 1.3")
    assert hardware.is_raspberry_pi() is True


def test_non_pi_is_false(monkeypatch):
    monkeypatch.setenv("FOODASSISTANT_FORCE_MODEL", "Generic x86-64 PC")
    assert hardware.is_raspberry_pi() is False


def test_empty_model_is_false(monkeypatch):
    # Forced empty string: no device-tree, no Pi (the CI / server case).
    monkeypatch.setenv("FOODASSISTANT_FORCE_MODEL", "")
    assert hardware.is_raspberry_pi() is False
    assert hardware.board_model() == ""


def test_case_insensitive(monkeypatch):
    monkeypatch.setenv("FOODASSISTANT_FORCE_MODEL", "RASPBERRY PI 4 MODEL B")
    assert hardware.is_raspberry_pi() is True
