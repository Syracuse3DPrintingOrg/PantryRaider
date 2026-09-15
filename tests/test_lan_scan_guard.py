"""The LAN scan / camera probe must never target a public network
(FoodAssistant-tfrm).

resolve_lan_cidr and the probe endpoints only accept RFC 1918 ranges now, so an
admin (or a misled admin session) cannot turn the camera/instance scanner into
an arbitrary internal or internet port scanner. Also confirms the camera probes
no longer follow redirects.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import lan_scan  # noqa: E402


@pytest.mark.parametrize("cidr", [
    "192.168.1.0/24", "10.0.0.0/24", "172.16.5.0/24", "172.31.0.0/16",
    "192.168.1.50",  # a bare host reads as a /32
])
def test_private_ranges_accepted(cidr):
    assert lan_scan.is_private_cidr(cidr) is True


@pytest.mark.parametrize("cidr", [
    "8.8.8.0/24", "1.1.1.1", "203.0.113.0/24",  # public
    "127.0.0.0/8",                               # loopback
    "169.254.0.0/16",                            # link-local
    "100.64.0.0/10",                             # CGNAT
    "0.0.0.0/0",                                 # everything
    "172.32.0.0/16",                             # just outside 172.16/12
    "not-a-cidr",
])
def test_non_private_ranges_refused(cidr):
    assert lan_scan.is_private_cidr(cidr) is False


def test_resolve_lan_cidr_rejects_explicit_public(monkeypatch):
    # An explicit public CIDR is refused (returns None) rather than swept.
    assert lan_scan.resolve_lan_cidr("8.8.8.0/24") is None


def test_resolve_lan_cidr_accepts_explicit_private():
    assert lan_scan.resolve_lan_cidr("192.168.4.0/24") == "192.168.4.0/24"


def test_resolve_lan_cidr_skips_non_private_candidates(monkeypatch):
    # Auto-detection candidates that are public are skipped too.
    from app.config import settings
    monkeypatch.setattr(settings, "lan_scan_cidr", "8.8.8.0/24", raising=False)
    monkeypatch.setattr(lan_scan, "default_cidr", lambda: "1.1.1.0/24")
    monkeypatch.setattr(lan_scan, "lan_cidr_from_config_urls", lambda: None)
    assert lan_scan.resolve_lan_cidr("", candidates=["9.9.9.0/24"]) is None
    # A private candidate is picked.
    assert lan_scan.resolve_lan_cidr("", candidates=["192.168.9.0/24"]) == "192.168.9.0/24"


def test_camera_probe_does_not_follow_redirects(monkeypatch):
    # _probe_http's default fetch must call httpx.get with follow_redirects=False
    # so a LAN responder cannot bounce the probe elsewhere.
    from app.services import camera_scan
    seen = {}

    class _Resp:
        status_code = 404
        headers = {"content-type": "text/html"}
        content = b""

    def _fake_get(url, **kw):
        seen.update(kw)
        return _Resp()

    monkeypatch.setattr(camera_scan.httpx, "get", _fake_get)
    camera_scan._probe_http("192.168.1.5", 80, "http", 0.4)
    assert seen.get("follow_redirects") is False
