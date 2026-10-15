"""The sensor reader names itself on every call to the app.

The reader's daemon module imports bleak at the top, which this suite does
not install, so a stand-in module is put in its place for the import. Only
the header helper is exercised; nothing touches a radio or the network.
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest


@pytest.fixture
def daemon_mod(monkeypatch):
    if "bleak" not in sys.modules:
        fake = types.ModuleType("bleak")
        fake.BleakClient = object
        fake.BleakScanner = object
        monkeypatch.setitem(sys.modules, "bleak", fake)
    import foodassistant_gadgets as pkg
    had = sys.modules.pop("foodassistant_gadgets.daemon", None)
    had_attr = getattr(pkg, "daemon", None)
    try:
        yield importlib.import_module("foodassistant_gadgets.daemon")
    finally:
        # Leave no daemon module built against the stand-in behind, neither in
        # sys.modules nor as the package attribute a later
        # 'from foodassistant_gadgets import daemon' would pick up.
        sys.modules.pop("foodassistant_gadgets.daemon", None)
        if had is not None:
            sys.modules["foodassistant_gadgets.daemon"] = had
        if had_attr is not None:
            pkg.daemon = had_attr
        elif hasattr(pkg, "daemon"):
            delattr(pkg, "daemon")


def _headers(daemon_mod, api_key):
    holder = types.SimpleNamespace(cfg=types.SimpleNamespace(api_key=api_key))
    return daemon_mod.Daemon._headers(holder)


def test_headers_name_the_reader_without_a_key(daemon_mod):
    from foodassistant_gadgets import __version__
    assert _headers(daemon_mod, "") == {"X-PR-Client": f"gadgets/{__version__}"}


def test_headers_carry_the_key_when_set(daemon_mod):
    from foodassistant_gadgets import __version__
    assert _headers(daemon_mod, "reader-key") == {
        "X-PR-Client": f"gadgets/{__version__}",
        "X-API-Key": "reader-key",
    }


def test_client_header_is_printable_and_short(daemon_mod):
    value = _headers(daemon_mod, "")["X-PR-Client"]
    assert len(value) <= 64
    assert all(0x20 < ord(c) < 0x7f for c in value)
