"""save() must never destroy existing settings on a bad read (FoodAssistant).

Regression for the data-loss bug where a truncated or corrupt settings.json was
silently treated as empty and then overwritten with only the newly saved field,
wiping every other setting (for example the Mealie URL/token and the theme).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402


def test_save_merges_and_preserves_other_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    sf = tmp_path / "settings.json"
    sf.write_text(json.dumps({"mealie_base_url": "http://m:9285", "ui_theme": "forest"}))
    settings.save({"grocy_base_url": "http://g:9383"})
    on_disk = json.loads(sf.read_text())
    # The unrelated keys survive a save of a different field.
    assert on_disk["mealie_base_url"] == "http://m:9285"
    assert on_disk["ui_theme"] == "forest"
    assert on_disk["grocy_base_url"] == "http://g:9383"


def test_corrupt_file_is_preserved_not_clobbered(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    sf = tmp_path / "settings.json"
    # A truncated/corrupt file that json.loads cannot parse but that holds the
    # user's (now unreadable) data.
    sf.write_text('{"mealie_api_key": "secret-token", "ui_theme": "for')
    settings.save({"secret_key": "abc123"})
    # The corrupt original is preserved aside for recovery.
    backups = list(tmp_path.glob("settings.json.corrupt.*"))
    assert backups, "corrupt settings.json should be backed up, not destroyed"
    assert "secret-token" in backups[0].read_text()
    # The fresh file holds the new value and is valid JSON.
    fresh = json.loads(sf.read_text())
    assert fresh["secret_key"] == "abc123"


def test_save_keeps_a_rollback_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    sf = tmp_path / "settings.json"
    sf.write_text(json.dumps({"mealie_base_url": "http://m:9285"}))
    settings.save({"ui_theme": "forest"})
    bak = tmp_path / "settings.json.bak"
    assert bak.exists()
    # The .bak holds the previous good content (before this save).
    assert json.loads(bak.read_text()) == {"mealie_base_url": "http://m:9285"}


def test_empty_file_is_treated_as_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    sf = tmp_path / "settings.json"
    sf.write_text("")  # genuinely empty, nothing to preserve
    settings.save({"grocy_api_key": "k"})
    assert json.loads(sf.read_text())["grocy_api_key"] == "k"
    assert not list(tmp_path.glob("settings.json.corrupt.*"))
