"""Automated validation of the shipped Home Assistant configs (FoodAssistant-f63n).

The files under homeassistant/ are pasted into a user's HA install by hand, so
nothing at runtime ever exercises them; a typo only surfaces during a fresh-HA
manual test pass. These tests give that pass a clean baseline by checking the
documented invariants without a live HA:

- every YAML file (and every fenced yaml block in the markdown guides) parses
- REST sensors poll a LAN URL, never a public reverse-proxy URL (a headless
  request through an auth proxy gets an HTML login page, not JSON)
- key_code comparisons are integer comparisons, never string casts (HA coerces
  template variables back to native types, so a |string cast silently never
  matches)
- rest_command.foodassistant_scan posts to /pending/scan
- entity ids referenced by the automations and Lovelace dashboard match the
  sensors the configuration actually defines
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml is required to lint the HA configs")

HA_DIR = Path(__file__).resolve().parents[1] / "homeassistant"

YAML_FILES = sorted(HA_DIR.rglob("*.yaml"))
MD_FILES = sorted(HA_DIR.rglob("*.md"))


class _HaLoader(yaml.SafeLoader):
    """SafeLoader that tolerates HA's custom tags (!secret, !include, ...)."""


def _unknown_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_HaLoader.add_multi_constructor("!", _unknown_tag)


def _load(text: str):
    return yaml.load(text, Loader=_HaLoader)


def _fenced_yaml_blocks(md_path: Path) -> list[str]:
    return re.findall(r"```yaml\n(.*?)```", md_path.read_text(), flags=re.S)


def _is_lan_url(url: str) -> bool:
    """True for a private/LAN address or a documented placeholder host."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "http":
        return False
    if host in ("localhost",) or host.endswith(".local") or "your_host" in host:
        return True
    if re.match(r"^(192\.168\.|10\.|127\.|172\.(1[6-9]|2\d|3[01])\.)", host):
        return True
    return False


# YAML parseability -----------------------------------------------------------

@pytest.mark.parametrize("path", YAML_FILES, ids=lambda p: str(p.relative_to(HA_DIR)))
def test_yaml_file_parses(path):
    doc = _load(path.read_text())
    assert doc is not None, f"{path.name} parsed to nothing"


@pytest.mark.parametrize("path", MD_FILES, ids=lambda p: str(p.relative_to(HA_DIR)))
def test_markdown_yaml_examples_parse(path):
    for i, block in enumerate(_fenced_yaml_blocks(path)):
        try:
            _load(block)
        except yaml.YAMLError as exc:  # pragma: no cover - failure message only
            pytest.fail(f"{path.name} fenced yaml block {i + 1} does not parse: {exc}")


# REST sensors use the LAN URL -------------------------------------------------

def _configuration() -> dict:
    return _load((HA_DIR / "configuration.yaml").read_text())


def test_rest_sensor_resources_are_lan_urls():
    conf = _configuration()
    resources = [entry["resource"] for entry in conf["rest"]]
    assert resources, "no REST sensors found in configuration.yaml"
    for url in resources:
        assert _is_lan_url(url), (
            f"REST sensor resource {url} must be a LAN URL, not a public one: "
            "headless polls through an auth proxy get an HTML login page"
        )


def test_rest_command_scan_posts_to_pending_scan():
    conf = _configuration()
    cmd = conf["rest_command"]["foodassistant_scan"]
    assert urlparse(cmd["url"]).path == "/pending/scan"
    assert _is_lan_url(cmd["url"])
    assert cmd["method"].upper() == "POST"
    assert "barcode" in cmd["payload"]


# key_code comparisons are integer comparisons ---------------------------------

def _all_ha_texts() -> list[tuple[Path, str]]:
    return [(p, p.read_text()) for p in YAML_FILES + MD_FILES]


def test_key_code_is_never_cast_to_string():
    for path, text in _all_ha_texts():
        assert not re.search(r"key_code\s*\|\s*string", text), (
            f"{path.name}: key_code must be compared as an integer; a |string "
            "cast gets undone by HA's type coercion and never matches"
        )


def test_key_comparisons_use_integers_not_quoted_strings():
    for path, text in _all_ha_texts():
        bad = re.findall(r"key(?:_code)?\s*(?:==|!=)\s*['\"]\d+['\"]", text)
        assert not bad, f"{path.name}: quoted key comparison(s) {bad}; compare as integers"


def test_scanner_guide_casts_key_code_to_int():
    text = (HA_DIR / "barcode-scanner.md").read_text()
    assert re.search(r"trigger\.event\.data\.key_code\s*\|\s*int", text)
    assert "{{ key == 28 }}" in text  # Enter, compared as an integer


# Entity id consistency ---------------------------------------------------------

def _slugify(name: str) -> str:
    """HA derives an entity id slug from the sensor name."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", name.lower())).strip("_")


def _defined_sensor_ids() -> set[str]:
    conf = _configuration()
    ids: set[str] = set()
    for entry in conf["rest"]:
        for sensor in entry.get("sensor", []):
            ids.add(f"sensor.{_slugify(sensor['name'])}")
    return ids


def _referenced_sensor_ids(text: str) -> set[str]:
    # A trailing underscore is a wildcard mention in prose (`sensor.food_*`),
    # not an entity id.
    return {m for m in re.findall(r"sensor\.[a-z0-9_]+", text) if not m.endswith("_")}


def test_automations_reference_defined_sensors():
    defined = _defined_sensor_ids()
    referenced = _referenced_sensor_ids((HA_DIR / "automations.yaml").read_text())
    assert referenced, "automations.yaml references no sensors"
    missing = referenced - defined
    assert not missing, f"automations.yaml references undefined sensors: {sorted(missing)}"


def test_lovelace_dashboard_references_defined_sensors():
    defined = _defined_sensor_ids()
    dashboard = HA_DIR / "lovelace" / "food-dashboard.yaml"
    referenced = _referenced_sensor_ids(dashboard.read_text())
    assert referenced, "the Lovelace dashboard references no sensors"
    missing = referenced - defined
    assert not missing, f"food-dashboard.yaml references undefined sensors: {sorted(missing)}"


def test_readme_entity_table_matches_defined_sensors():
    defined = _defined_sensor_ids()
    referenced = _referenced_sensor_ids((HA_DIR / "README.md").read_text())
    missing = referenced - defined
    assert not missing, f"README.md documents undefined sensors: {sorted(missing)}"
