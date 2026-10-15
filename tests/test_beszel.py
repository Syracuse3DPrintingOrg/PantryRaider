"""Tests for the Beszel dashboard link (service/app/services/beszel.py).

Beszel (FoodAssistant-4kz2) is an optional monitoring hub offered above the
built-in Resources snapshot. These cover the URL normalization and the
enabled+url gate that decides whether the pane should show the link. No
network, no settings object: both functions are pure.
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import beszel  # noqa: E402


def test_normalize_url_defaults_scheme_and_trims():
    assert beszel.normalize_url("beszel.local:8090") == "http://beszel.local:8090"
    assert beszel.normalize_url("http://beszel.local:8090/") == "http://beszel.local:8090"
    assert beszel.normalize_url("https://hub.example.com/") == "https://hub.example.com"
    assert beszel.normalize_url("  ") == ""
    assert beszel.normalize_url(None) == ""


def test_dashboard_link_requires_enabled_and_url():
    assert beszel.dashboard_link(True, "beszel.local:8090") == "http://beszel.local:8090"
    assert beszel.dashboard_link(False, "beszel.local:8090") == ""
    assert beszel.dashboard_link(True, "") == ""
    assert beszel.dashboard_link(True, "   ") == ""
    assert beszel.dashboard_link(False, "") == ""


def test_dashboard_link_preserves_https():
    assert beszel.dashboard_link(True, "https://hub.example.com") == "https://hub.example.com"
