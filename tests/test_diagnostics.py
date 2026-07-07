"""Debug logging service + admin endpoints (FoodAssistant-asra)."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import diagnostics  # noqa: E402


@pytest.fixture(autouse=True)
def _detach_handlers():
    """Remove any Pantry Raider file handler the test installs so cases do not
    leak handlers (and open files) into each other."""
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, diagnostics._HANDLER_TAG, False):
            root.removeHandler(h)
            h.close()


def test_configure_writes_to_data_dir(tmp_path):
    path = diagnostics.configure_file_logging(str(tmp_path), debug=True)
    assert path == diagnostics.log_path(str(tmp_path))
    logging.getLogger("app.test").debug("hello-debug-line")
    for h in logging.getLogger().handlers:
        h.flush()
    assert path.exists()
    assert "hello-debug-line" in path.read_text()


def test_only_one_handler_added_across_calls(tmp_path):
    diagnostics.configure_file_logging(str(tmp_path), debug=False)
    diagnostics.configure_file_logging(str(tmp_path), debug=True)
    diagnostics.configure_file_logging(str(tmp_path), debug=False)
    tagged = [h for h in logging.getLogger().handlers
              if getattr(h, diagnostics._HANDLER_TAG, False)]
    assert len(tagged) == 1


def test_read_log_redacts_secrets(tmp_path):
    diagnostics.configure_file_logging(str(tmp_path), debug=True)
    logging.getLogger("app.test").info("calling api with key sk-supersecretvalue now")
    for h in logging.getLogger().handlers:
        h.flush()
    text = diagnostics.read_log_text(str(tmp_path), ["sk-supersecretvalue"])
    assert "sk-supersecretvalue" not in text
    assert "[redacted]" in text


def test_read_log_empty_when_nothing_written(tmp_path):
    assert diagnostics.read_log_text(str(tmp_path), []) == ""


def test_configure_tolerates_unwritable_dir(tmp_path):
    # A path whose parent is a file cannot be created; configure returns None and
    # does not raise, leaving console logging in place.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    assert diagnostics.configure_file_logging(str(blocker), debug=True) is None


# -- API --------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    # Configured so the setup-redirect middleware lets /admin through.
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, diagnostics._HANDLER_TAG, False):
                root.removeHandler(h)
                h.close()


def test_logging_toggle_and_download(client):
    assert client.post("/admin/logging", json={"enabled": True}).json()["enabled"] is True
    assert client.get("/admin/logging").json()["enabled"] is True
    logging.getLogger("app.test").info("downloadable-marker")
    for h in logging.getLogger().handlers:
        h.flush()
    r = client.get("/admin/logs/download")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert "downloadable-marker" in r.text
    # Turning it back off persists.
    assert client.post("/admin/logging", json={"enabled": False}).json()["enabled"] is False
    assert client.get("/admin/logging").json()["enabled"] is False


def test_download_with_no_logs_gives_friendly_message(client):
    r = client.get("/admin/logs/download")
    assert r.status_code == 200
    assert "No log has been captured" in r.text
