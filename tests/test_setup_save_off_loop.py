"""A settings save writes settings.json off the event loop.

The write is durable (fsyncs of the file, its folder and the last good copy),
which on an SD card can take a good part of a second. /setup/save is an async
route, so it hands the save to a thread and other requests keep moving.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    with patch.object(type(settings), "is_configured", lambda self: True):
        yield TestClient(app, client=("192.168.1.20", 50000))


def test_the_save_runs_off_the_event_loop(client, monkeypatch):
    seen = []
    real = type(settings).save

    def spy(self, data):
        try:
            asyncio.get_running_loop()
            seen.append("loop")
        except RuntimeError:
            seen.append("thread")
        return real(self, data)

    monkeypatch.setattr(type(settings), "save", spy)
    r = client.post("/setup/save", json={"expiring_soon_days": 6})
    assert r.json()["ok"] is True
    assert settings.expiring_soon_days == 6
    assert seen and seen[0] == "thread"
