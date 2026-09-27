"""Home Assistant can add this repository as an add-on store.

The Supervisor accepts a git repository as an add-on repository only when a
repository.yaml (or .yml or .json) sits at the repository ROOT. It then finds
the add-ons themselves anywhere below it, by looking for their config files.
The file used to live under homeassistant/addon/, so adding the repository in
Home Assistant failed with "not a valid app repository" and no add-on showed.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO_FILE = ROOT / "repository.yaml"
OLD_REPO_FILE = ROOT / "homeassistant" / "addon" / "repository.yaml"
ADDON_CONFIG = ROOT / "homeassistant" / "addon" / "foodassistant" / "config.yaml"
STRIP_LIST = ROOT / "scripts" / "public-strip.txt"


def test_repository_file_is_at_the_root_and_valid():
    assert REPO_FILE.is_file(), "the Supervisor only reads repository.yaml at the repo root"
    data = yaml.safe_load(REPO_FILE.read_text())
    assert isinstance(data, dict)
    assert str(data.get("name") or "").strip()
    assert str(data.get("url") or "").strip()


def test_the_old_nested_copy_is_gone():
    """Two copies would drift; the nested one is never read."""
    assert not OLD_REPO_FILE.exists()


def test_the_addon_is_found_by_a_config_glob_from_the_root():
    """The Supervisor discovers add-ons with a **/config.* glob under the
    repository root, so the add-on folder can stay where it is."""
    found = {p.resolve() for p in ROOT.glob("**/config.yaml")
             if ".git" not in p.parts}
    assert ADDON_CONFIG.resolve() in found


def test_the_repository_files_ship_in_the_public_tree():
    """The strip list only exists in the dev tree; the public tree has nothing
    to check here."""
    if not STRIP_LIST.is_file():
        pytest.skip("scripts/public-strip.txt is not part of this tree")
    listed = {line.strip() for line in STRIP_LIST.read_text().splitlines()
              if line.strip() and not line.lstrip().startswith("#")}
    for path in ("repository.yaml", "homeassistant/addon/foodassistant/config.yaml",
                 "homeassistant/addon/foodassistant", "homeassistant/addon",
                 "homeassistant"):
        assert path not in listed, f"{path} is stripped from the public tree"
