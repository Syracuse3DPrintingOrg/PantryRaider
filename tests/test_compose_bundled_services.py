"""The bundled backends stay private, pinned together, and pairable.

Ollama has no sign-in, so its port answers on the machine itself only. The
Beszel agent runs on the host network, so the hub cannot reach it by service
name; the two share a socket instead, and the agent never needs the host's
whole filesystem. The same pins appear in every compose file that ships them.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "docker-compose.yml"
PROD = ROOT / "docker-compose.prod.yml"
APPLIANCE = ROOT / "scripts" / "image-build" / "docker-compose.appliance.yml"
UNRAID = ROOT / "unraid" / "docker-compose.yml"
STACKS = (DEV, PROD, APPLIANCE)
SOCKET_DIR = "./beszel_socket:/beszel_socket"


def _services(path: Path) -> dict:
    return yaml.safe_load(path.read_text())["services"]


def _tag(image: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", image.rpartition(":")[2]))


@pytest.mark.parametrize("path", STACKS, ids=lambda p: p.name)
def test_ollama_answers_on_this_machine_only(path: Path) -> None:
    assert _services(path)["ollama"]["ports"] == ["127.0.0.1:11434:11434"]


@pytest.mark.parametrize("path", STACKS, ids=lambda p: p.name)
def test_beszel_hub_and_agent_pair_over_a_shared_socket(path: Path) -> None:
    svc = _services(path)
    hub, agent = svc["beszel"], svc["beszel-agent"]
    assert SOCKET_DIR in hub["volumes"] and SOCKET_DIR in agent["volumes"]
    assert "LISTEN=/beszel_socket/beszel.sock" in agent["environment"]
    assert "KEY=${BESZEL_AGENT_KEY:-}" in agent["environment"]
    # The agent reads the Docker socket, never the whole host filesystem.
    assert not any(v.startswith("/:") for v in agent["volumes"])
    # Its state lives in a named volume, so a recreate does not orphan one.
    assert "beszel-agent-data:/var/lib/beszel-agent" in agent["volumes"]
    assert "beszel-agent-data" in yaml.safe_load(path.read_text())["volumes"]
    assert _tag(hub["image"]) == _tag(agent["image"]) >= (0, 20, 0)


def test_every_stack_pins_the_same_backend_versions() -> None:
    for name in ("mealie", "ollama", "beszel", "beszel-agent"):
        images = {p.name: _services(p)[name]["image"] for p in STACKS}
        assert len(set(images.values())) == 1, f"{name} pins differ: {images}"
    # The Unraid stack ships Mealie as a commented-out block.
    mealie = _services(PROD)["mealie"]["image"]
    assert f"#   image: {mealie}" in UNRAID.read_text()
    # Mealie before v3.28.0 misses a run of upstream security fixes.
    assert _tag(mealie) >= (3, 28, 0)
