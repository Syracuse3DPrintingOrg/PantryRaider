"""Things that cost a Raspberry Pi a write to its memory card, or a syscall
per render, for no benefit in production.

A kitchen screen polls every few seconds. Every request that leaves a log
line, and every render that stats a template, is repeated tens of thousands
of times a day on hardware where the disk is an SD card. None of it is
visible on a laptop, which is how it ships.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "service"
sys.path.insert(0, str(SERVICE))

from app.services import diagnostics  # noqa: E402


def test_per_request_access_lines_are_off_unless_asked_for(monkeypatch):
    """uvicorn logs one line per request. On a Pi that is an SD write several
    times a second, forever, into a log Docker never trims. The satellite's
    systemd unit already passed --no-access-log; the image must match."""
    log = logging.getLogger("uvicorn.access")
    saved = log.level
    try:
        monkeypatch.delenv("PR_ACCESS_LOG", raising=False)
        log.setLevel(logging.INFO)             # what uvicorn sets at startup
        diagnostics.configure_console_logging()
        assert log.level >= logging.WARNING, "access lines still on by default"

        monkeypatch.setenv("PR_ACCESS_LOG", "1")
        log.setLevel(logging.INFO)
        diagnostics.configure_console_logging()
        assert log.level == logging.INFO, "PR_ACCESS_LOG=1 must restore them"
    finally:
        log.setLevel(saved)


def test_templates_are_compiled_once_in_production():
    """Jinja's default stats every template on every render. base.html pulls
    in a couple of dozen partials, so that was a couple of dozen stat calls
    per page for a change that in production never comes."""
    assert os.environ.get("PR_DEV", "") != "1", "this suite runs as production"
    from app.templating import templates
    assert templates.env.auto_reload is False


def test_the_dev_compose_opts_back_in():
    """Developers keep live template edits and request lines in the console."""
    dev = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    env = dev["services"]["service"]["environment"]
    assert "PR_DEV=1" in env and "PR_ACCESS_LOG=1" in env


def test_every_deployed_compose_bounds_the_container_log():
    """Docker's json-file driver grows without limit by default. On an SD card
    that is a slow write per line and, in the end, a full card."""
    for path, key in [("docker-compose.prod.yml", "service"),
                      ("scripts/image-build/docker-compose.appliance.yml", "service"),
                      ("unraid/docker-compose.yml", "pantryraider")]:
        doc = yaml.safe_load((ROOT / path).read_text())
        logging_cfg = doc["services"][key].get("logging") or {}
        opts = logging_cfg.get("options") or {}
        assert logging_cfg.get("driver") == "json-file", f"{path}: no json-file driver"
        assert "max-size" in opts and "max-file" in opts, f"{path}: log not bounded"
