"""Server auto-update via Watchtower is opt-in and scoped (FoodAssistant-k2kk).

These guard the docker-compose.prod.yml wiring so a future edit cannot silently
turn auto-updates on by default or let Watchtower update the pinned backends.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.prod.yml"


def _services():
    return yaml.safe_load(COMPOSE.read_text())["services"]


def test_watchtower_runs_by_default_and_is_label_scoped():
    wt = _services()["watchtower"]
    # On by default: no profile gate, so a plain `docker compose up -d` starts it.
    assert "profiles" not in wt
    assert "/var/run/docker.sock:/var/run/docker.sock" in wt["volumes"]
    env = wt["environment"]
    assert "WATCHTOWER_LABEL_ENABLE=true" in env        # label-scoped, not all containers


def test_only_the_service_container_is_labeled():
    svc = _services()
    assert "com.centurylinklabs.watchtower.enable=true" in svc["service"]["labels"]
    # The pinned backends must NOT carry the enable label, so Watchtower leaves
    # them alone.
    for name in ("grocy", "mealie", "ollama"):
        assert "com.centurylinklabs.watchtower.enable=true" not in svc[name].get("labels", [])
