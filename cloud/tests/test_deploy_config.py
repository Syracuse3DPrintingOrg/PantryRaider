"""The deploy files carry the hardening the service relies on: HSTS on the
apex in the Caddyfile and an app healthcheck in the compose file."""
from pathlib import Path

CLOUD = Path(__file__).parent.parent


def _apex_block() -> str:
    text = (CLOUD / "Caddyfile").read_text()
    start = text.index("{$CLOUD_DOMAIN} {")
    return text[start:text.index("\n}\n", start)]


def test_apex_sends_hsts():
    assert 'header Strict-Transport-Security "max-age=31536000"' in _apex_block()


def test_app_has_a_healthcheck_on_health():
    # Read as text: the cloud suite runs without a YAML parser installed.
    text = (CLOUD / "docker-compose.yml").read_text()
    app = text[text.index("\n  app:\n"):text.index("\n  db:\n")]
    assert "    healthcheck:\n" in app
    assert "http://127.0.0.1:8000/health" in app
