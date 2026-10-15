"""Request logs must not keep secret tokens or client addresses
(FoodAssistant-fa3).

The verify, reset and share pages carry a secret token in their path, so a
uvicorn access line would write it, with the client address, into the
container log. The image turns access logging off and compose rotates the
logs that remain."""
from __future__ import annotations

from pathlib import Path

CLOUD = Path(__file__).resolve().parents[1]


def test_uvicorn_runs_without_an_access_log():
    cmd = [line for line in (CLOUD / "Dockerfile").read_text().splitlines()
           if not line.lstrip().startswith("#")]
    text = "\n".join(cmd)
    assert '"uvicorn"' in text
    assert '"--no-access-log"' in text


def test_compose_rotates_container_logs():
    compose = (CLOUD / "docker-compose.yml").read_text()
    assert compose.count("driver: json-file") >= 2  # app and db
    assert compose.count('max-size: "10m"') >= 2
    assert compose.count('max-file: "3"') >= 2
