"""Beszel monitoring hub link (FoodAssistant-4kz2).

Beszel (https://github.com/henrygd/beszel) is an optional self-hosted
monitoring hub with history and graphs, richer than the built-in live
snapshot in the Resources pane (services/resources.py). This module holds
the two small pure functions the pane needs: normalizing whatever URL the
user typed into a clickable link, and deciding whether the pane should offer
that link at all. The built-in snapshot always stays available underneath,
so nothing here ever hides it.
"""
from __future__ import annotations


def normalize_url(url: str) -> str:
    """Trim a Beszel hub URL and default the scheme to http.

    Users type ``beszel.local:8090`` as often as a full URL; assume http so
    the link still resolves on a LAN with no certificate. Returns "" for an
    empty input.
    """
    base = (url or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    return base


def dashboard_link(enabled: bool, url: str) -> str:
    """The Beszel dashboard URL to offer, or "" when it should not be shown.

    Requires both the enable toggle and a non-empty URL; either missing
    means the pane shows only the built-in live snapshot.
    """
    if not enabled:
        return ""
    return normalize_url(url)
