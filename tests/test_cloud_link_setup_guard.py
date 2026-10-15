"""A Forager account connected before setup finished belongs to the owner.

The wizard's AI step signs in to Forager before this device has any login, which
is legitimate: the person doing the setup is right there. But anyone else on the
network could reach that same step while a new device sat on the wizard, connect
their own account, and afterwards sign in here with it. So the connection is
stamped with the browser that made it and only survives the save that finishes
setup when it comes from that same browser (security audit, Sep 2026).
"""
import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.routers.setup import (  # noqa: E402
    cloud_link_survives_setup, _settle_pre_setup_cloud_link,
)


@pytest.fixture(autouse=True)
def data_dir(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    try:
        yield
    finally:
        os.chdir(cwd)


def test_a_connection_made_while_signed_in_is_always_kept():
    assert cloud_link_survives_setup("", "") is True
    assert cloud_link_survives_setup("", "anything") is True


def test_the_same_browser_keeps_its_connection():
    assert cloud_link_survives_setup("abc123", "abc123") is True


def test_another_browser_does_not():
    assert cloud_link_survives_setup("abc123", "") is False
    assert cloud_link_survives_setup("abc123", "def456") is False


def _linked(monkeypatch, stamp="abc123"):
    monkeypatch.setattr(settings, "cloud_instance_token", "instance-token", raising=False)
    monkeypatch.setattr(settings, "cloud_link_session", stamp, raising=False)
    monkeypatch.setattr(settings, "vision_provider", "cloud", raising=False)
    monkeypatch.setattr(settings, "enrich_provider", "cloud", raising=False)
    monkeypatch.setattr(settings, "tunnel_mode", "forager", raising=False)
    monkeypatch.setattr(settings, "tunnel_enabled", True, raising=False)


def test_finishing_setup_elsewhere_drops_a_stranger_s_connection(monkeypatch):
    _linked(monkeypatch)
    dropped = _settle_pre_setup_cloud_link({})
    assert dropped is True
    assert settings.cloud_instance_token == ""
    assert settings.cloud_link_session == ""
    # Remote access and the scanning provider that leaned on it go too, so the
    # kitchen is not left pointing at a service it no longer has.
    assert settings.tunnel_enabled is False
    assert settings.tunnel_mode == ""
    assert settings.vision_provider != "cloud"


def test_finishing_setup_in_the_same_browser_keeps_it(monkeypatch):
    _linked(monkeypatch)
    session = {"cloud_link_nonce": "abc123"}
    dropped = _settle_pre_setup_cloud_link(session)
    assert dropped is False
    assert settings.cloud_instance_token == "instance-token"
    assert settings.tunnel_enabled is True
    # The stamp is spent: from here on the settings surface needs a login.
    assert settings.cloud_link_session == ""
    assert "cloud_link_nonce" not in session


def test_a_connection_made_after_setup_is_never_touched(monkeypatch):
    _linked(monkeypatch, stamp="")
    assert _settle_pre_setup_cloud_link({}) is False
    assert settings.cloud_instance_token == "instance-token"
