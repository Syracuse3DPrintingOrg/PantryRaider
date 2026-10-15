"""A blank environment variable must not erase a saved setting.

Every copy of .env.example and the Unraid stack ships variables that are present
but empty (GROCY_BASE_URL=, GEMINI_API_KEY=). Those used to count as "set", so
the startup overlay skipped the address and keys the setup page had saved and
the install came back from every restart looking unconfigured, bouncing every
browser to the setup page again (reliability audit, Sep 2026).
"""
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import Settings, _SAVEABLE  # noqa: E402


def _overlay(s, saved):
    """The startup overlay's rule, applied to a fresh Settings instance."""
    for k, v in saved.items():
        if k in _SAVEABLE and k not in s.model_fields_set:
            object.__setattr__(s, k, v)
    return s


def test_blank_env_var_does_not_count_as_set(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env of the repo's own in the way
    monkeypatch.setenv("GROCY_BASE_URL", "")
    monkeypatch.setenv("GROCY_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    s = Settings()
    assert "grocy_base_url" not in s.model_fields_set
    assert "grocy_api_key" not in s.model_fields_set
    assert "gemini_api_key" not in s.model_fields_set


def test_saved_settings_survive_blank_env_vars(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROCY_BASE_URL", "")
    monkeypatch.setenv("GROCY_API_KEY", "")
    monkeypatch.setenv("VISION_PROVIDER", "")
    s = _overlay(Settings(), {
        "grocy_base_url": "http://192.168.1.50:9383",
        "grocy_api_key": "saved-key",
        "vision_provider": "openai",
    })
    assert s.grocy_base_url == "http://192.168.1.50:9383"
    assert s.grocy_api_key == "saved-key"
    assert s.vision_provider == "openai"


def test_a_real_env_value_still_wins(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROCY_BASE_URL", "http://pinned:9383")
    s = _overlay(Settings(), {"grocy_base_url": "http://saved:9383"})
    assert s.grocy_base_url == "http://pinned:9383"


def test_no_shipped_defaults_file_pins_a_vision_provider():
    # The self-host path is `cp .env.example .env` plus docker-compose.yml's
    # `env_file: .env`, so a non-empty value here overrides the provider the
    # setup page saved, on every restart. env_ignore_empty only rescues the
    # blank case, so these files have to ship the value blank.
    root = Path(__file__).resolve().parents[1]
    for rel in (".env.example", "unraid/docker-compose.yml"):
        for line in (root / rel).read_text().splitlines():
            stripped = line.strip().lstrip("- ").strip()
            if stripped.startswith("VISION_PROVIDER"):
                value = stripped.split("=", 1)[1] if "=" in stripped else ""
                # `${VISION_PROVIDER:-}` passes an operator-set value through
                # without pinning one of its own.
                value = value.replace("${VISION_PROVIDER:-}", "")
                assert value == "", f"{rel} pins VISION_PROVIDER: {line!r}"
    template = (root / "unraid" / "pantryraider.xml").read_text()
    assert 'Target="VISION_PROVIDER" Default=""' in template


def test_unraid_stack_ships_no_value_that_would_pin_a_setting():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "unraid" / "docker-compose.yml").read_text()
    assert "VISION_PROVIDER=${VISION_PROVIDER:-gemini}" not in compose
    template = (root / "unraid" / "pantryraider.xml").read_text()
    assert 'Target="VISION_PROVIDER" Default=""' in template
    # A generated signing key is written back to the saved settings on first
    # run, so the template must not offer a blank one to overwrite it with.
    assert 'Target="SECRET_KEY"' not in template
