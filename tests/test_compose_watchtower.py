"""Server auto-update via Watchtower is on by default and scoped (FoodAssistant-k2kk).

These guard the docker-compose.prod.yml wiring. A plain `docker compose up -d`
starts the updater, so it has to work on current Docker and stay confined to
Pantry Raider's own app container: never the pinned backends, and never a
Watchtower the owner already runs or the containers that one looks after.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.prod.yml"
ENABLE_LABEL = "com.centurylinklabs.watchtower.enable"
SCOPE_LABEL = "com.centurylinklabs.watchtower.scope"


def _services():
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _pairs(entries) -> dict[str, str]:
    """Labels and environment, whether written as KEY=VALUE lines or a map."""
    if isinstance(entries, dict):
        return {str(k): str(v) for k, v in entries.items()}
    return dict(str(e).split("=", 1) for e in entries or [])


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
    # Nor may anything else in the stack (CUPS, Beszel, Watchtower itself).
    others = [name for name in svc if name != "service"]
    assert len(others) >= 6, f"only saw {others}"
    for name in others:
        assert ENABLE_LABEL not in _pairs(svc[name].get("labels")), name


def test_only_the_app_can_reach_the_update_api():
    svc = _services()
    # Watchtower holds the Docker socket, so its API sits on a network it shares
    # with the app alone, never on the stack's default network.
    assert set(svc["watchtower"]["networks"]) == {"updater"}
    assert "updater" in svc["service"]["networks"]
    for name in svc:
        if name not in ("service", "watchtower"):
            assert "updater" not in (svc[name].get("networks") or []), name


def test_watchtower_is_the_maintained_fork_on_a_pinned_tag():
    image = _services()["watchtower"]["image"]
    # containrrr/watchtower is archived and always asks for Docker API 1.25,
    # which Docker 29 refuses, so there it restarts in a loop and never
    # updates anything. The fork negotiates the API version instead.
    assert "containrrr" not in image
    repo, _, tag = image.rpartition(":")
    assert repo == "ghcr.io/nicholas-fedor/watchtower"
    assert tag != "latest" and re.fullmatch(r"\d+\.\d+\.\d+", tag), \
        f"pin a release tag, got {tag!r}"


def test_watchtower_and_the_app_share_one_scope():
    svc = _services()
    scope = _pairs(svc["watchtower"]["environment"]).get("WATCHTOWER_SCOPE")
    # Unscoped, Watchtower stops and deletes every other Watchtower on the host
    # when it starts (their images too, with CLEANUP on) and updates anything
    # that carries the common enable label.
    assert scope, "watchtower must run with a WATCHTOWER_SCOPE"
    assert _pairs(svc["watchtower"].get("labels")).get(SCOPE_LABEL) == scope
    assert _pairs(svc["service"].get("labels")).get(SCOPE_LABEL) == scope


def test_update_now_and_the_daily_check_are_both_on():
    svc = _services()
    env = _pairs(svc["watchtower"]["environment"])
    # The Updates page's Update now button posts to /v1/update.
    endpoints = env.get("WATCHTOWER_HTTP_API_ENDPOINTS", "").replace(",", " ").split()
    assert "update" in endpoints
    # Turning the HTTP API on by itself switches the scheduled check off.
    assert env.get("WATCHTOWER_HTTP_API_PERIODIC_POLLS") == "true"
    assert env.get("WATCHTOWER_LABEL_ENABLE") == "true"
    # The older switch is deprecated in the fork and gone from its next major.
    assert "WATCHTOWER_HTTP_API_UPDATE" not in env
    # The app authenticates the trigger with the same token.
    app_env = _pairs(svc["service"]["environment"])
    assert env["WATCHTOWER_HTTP_API_TOKEN"] == app_env["WATCHTOWER_HTTP_API_TOKEN"]
