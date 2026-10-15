"""Forager reports the release it was built from (FoodAssistant-a96pv): the
app's APP_VERSION, optionally with the commit, and "unknown" rather than a
crash when neither is available."""
import re
from pathlib import Path

from app.config import CLOUD_VERSION, resolve_version

REPO = Path(__file__).resolve().parents[2]
CLOUD = REPO / "cloud"


def _app_version() -> str:
    text = (REPO / "service" / "app" / "config.py").read_text()
    return re.search(r'^APP_VERSION = "([^"]+)"', text, re.MULTILINE).group(1)


def test_checkout_reports_the_apps_release():
    assert CLOUD_VERSION == _app_version()


def test_health_reports_the_release_in_the_version_field(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "app": "pantryraider-cloud",
                           "version": CLOUD_VERSION}


def test_reads_the_baked_in_copy_first(tmp_path):
    baked = tmp_path / "app_config.py"
    baked.write_text('APP_NAME = "Pantry Raider"\nAPP_VERSION = "1.2.3"\n')
    assert resolve_version([baked, tmp_path / "missing.py"], environ={}) == "1.2.3"


def test_appends_the_commit_when_given(tmp_path):
    baked = tmp_path / "app_config.py"
    baked.write_text('APP_VERSION = "1.2.3"\n')
    assert resolve_version([baked], environ={"FORAGER_GIT_SHA": "ABC1234"}) == "1.2.3+abc1234"
    # Anything that is not a hex sha is ignored rather than echoed on /health.
    assert resolve_version([baked], environ={"FORAGER_GIT_SHA": "<b>"}) == "1.2.3"


def test_missing_or_empty_source_falls_back_to_unknown(tmp_path):
    empty = tmp_path / "empty.py"
    empty.write_text("")
    assert resolve_version([empty, tmp_path / "missing.py"], environ={}) == "unknown"
    assert resolve_version([tmp_path], environ={}) == "unknown"  # a directory
    assert resolve_version([], environ={"FORAGER_GIT_SHA": "abc1234"}) == "unknown+abc1234"


def test_explicit_override_wins(tmp_path):
    assert resolve_version([], environ={"FORAGER_VERSION": "9.9.9"}) == "9.9.9"


def test_image_build_carries_the_release_file():
    dockerfile = (CLOUD / "Dockerfile").read_text()
    assert "COPY --from=repo /service/app/config.py ./release/app_config.py" in dockerfile
    # The fallback stage keeps a plain `docker build cloud/` working.
    assert " AS repo\n" in dockerfile
    compose = (CLOUD / "docker-compose.yml").read_text()
    assert "additional_contexts:\n        repo: ..\n" in compose
