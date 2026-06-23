"""Tests for services/lan_scan.py - no network, monkeypatched probing."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import lan_scan


def test_scan_rejects_large_cidr():
    results = lan_scan.scan_for_instances("10.0.0.0/20")  # 4096 hosts
    assert len(results) == 1
    assert "error" in results[0]
    assert "too large" in results[0]["error"]


def test_scan_rejects_invalid_cidr():
    results = lan_scan.scan_for_instances("not-a-cidr")
    assert len(results) == 1
    assert "error" in results[0]


def test_scan_returns_fingerprinted_hosts(monkeypatch):
    """Monkeypatch _probe_host so the scan never touches the network."""
    def fake_probe(ip, ports, timeout):
        if ip == "192.168.1.3":
            return {"ip": ip, "port": 9284, "version": "1.6.0",
                    "mode": "pi_remote", "status": "ok"}
        return None

    monkeypatch.setattr(lan_scan, "_probe_host", fake_probe)
    results = lan_scan.scan_for_instances("192.168.1.0/29")  # hosts .1-.6
    assert len(results) == 1
    assert results[0]["ip"] == "192.168.1.3"
    assert results[0]["version"] == "1.6.0"


def test_scan_collects_multiple_hosts(monkeypatch):
    hits = {"192.168.1.1", "192.168.1.3"}

    def fake_probe(ip, ports, timeout):
        if ip in hits:
            return {"ip": ip, "port": 9284, "version": "1.5.0", "mode": "server", "status": "ok"}
        return None

    monkeypatch.setattr(lan_scan, "_probe_host", fake_probe)
    results = lan_scan.scan_for_instances("192.168.1.0/29")  # hosts .1-.6
    assert len(results) == 2
    found_ips = {r["ip"] for r in results}
    assert found_ips == hits


def test_scan_swallows_probe_errors(monkeypatch):
    """A probe that raises must not abort the full scan."""
    def fake_probe(ip, ports, timeout):
        if ip == "192.168.1.2":
            raise RuntimeError("simulated failure")
        return None

    monkeypatch.setattr(lan_scan, "_probe_host", fake_probe)
    # Should return an empty list without raising.
    results = lan_scan.scan_for_instances("192.168.1.0/29")
    assert results == []


def test_scan_excludes_local_host(monkeypatch):
    """The scanning host fingerprints as itself and must be dropped by default."""
    def fake_probe(ip, ports, timeout):
        return {"ip": ip, "port": 9284, "version": "1.6.0", "mode": "pi_hosted", "status": "ok"}

    monkeypatch.setattr(lan_scan, "_probe_host", fake_probe)
    monkeypatch.setattr(lan_scan, "_local_ips", lambda: {"192.168.1.3"})
    results = lan_scan.scan_for_instances("192.168.1.0/29")  # hosts .1-.6
    found_ips = {r["ip"] for r in results}
    assert "192.168.1.3" not in found_ips
    assert "192.168.1.1" in found_ips


def test_scan_exclude_override_scans_everything(monkeypatch):
    """An explicit empty exclude set scans every host, self included."""
    def fake_probe(ip, ports, timeout):
        if ip == "192.168.1.3":
            return {"ip": ip, "port": 9284, "version": "1.6.0", "mode": "pi_hosted", "status": "ok"}
        return None

    monkeypatch.setattr(lan_scan, "_probe_host", fake_probe)
    monkeypatch.setattr(lan_scan, "_local_ips", lambda: {"192.168.1.3"})
    results = lan_scan.scan_for_instances("192.168.1.0/29", exclude=set())
    assert {r["ip"] for r in results} == {"192.168.1.3"}


def test_default_cidr_returns_slash24_or_none(monkeypatch):
    """default_cidr should return a /24 string or None; never raise."""
    import socket

    class FakeSock:
        def connect(self, _): pass
        def getsockname(self): return ("10.8.0.5", 0)
        def __enter__(self): return self
        def __exit__(self, *_): pass

    monkeypatch.setattr(socket, "socket", lambda *a, **kw: FakeSock())
    cidr = lan_scan.default_cidr()
    assert cidr == "10.8.0.0/24"


def test_default_cidr_returns_none_on_failure(monkeypatch):
    import socket

    def bad_socket(*a, **kw):
        raise OSError("no network")

    monkeypatch.setattr(socket, "socket", bad_socket)
    assert lan_scan.default_cidr() is None
