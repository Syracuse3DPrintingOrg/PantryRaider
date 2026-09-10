"""The label-printing CUPS sidecar stays private and version-pinned.

Admin operations on that cupsd deliberately need no password, because the app's
sibling container runs lpadmin over the compose network (FoodAssistant-32dd).
Two things keep that from becoming a network-wide "add or delete any printer"
endpoint, and a future compose edit must not undo either: the published port is
loopback only, and the config the container refetches on every restart is pinned
to a release tag instead of a branch that can outrun the installed app.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = (ROOT / "docker-compose.yml", ROOT / "docker-compose.prod.yml")
CUPSD_CONF = ROOT / "docker" / "cups" / "cupsd.conf"

RAW_URL = re.compile(
    r"raw\.githubusercontent\.com/Syracuse3DPrintingOrg/PantryRaider/"
    r"\$\{CUPS_CONFIG_REF:-(?P<ref>[^}/]+)\}/docker/cups"
)


def _cups(path: Path) -> dict:
    return yaml.safe_load(path.read_text())["services"]["cups"]


def _pinned_ref(path: Path) -> str:
    match = RAW_URL.search("\n".join(_cups(path)["command"]))
    assert match, f"{path.name}: the cups config fetch must use ${{CUPS_CONFIG_REF:-<tag>}}"
    return match.group("ref")


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_cups_admin_port_is_published_on_loopback_only(path: Path) -> None:
    assert _cups(path)["ports"] == ["127.0.0.1:6631:631"]


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_cups_config_fetch_is_pinned_to_a_release_tag(path: Path) -> None:
    ref = _pinned_ref(path)
    assert re.fullmatch(r"v\d+\.\d+\.\d+", ref), f"{path.name}: pin a release tag, got {ref!r}"


def test_both_compose_files_pin_the_same_config_ref() -> None:
    assert len({_pinned_ref(p) for p in COMPOSE_FILES}) == 1


def test_cupsd_conf_keeps_passwordless_local_admin() -> None:
    conf = CUPSD_CONF.read_text()
    # ServerAlias plus @LOCAL are what let the sibling app container manage
    # queues by the compose service name.
    assert "ServerAlias *" in conf
    assert "Allow @LOCAL" in conf
    # services/printing.py builds bare lpadmin argv with no credential, so a
    # user requirement here would break in-app printing on every install.
    assert not re.search(r"^\s*Require\s+user", conf, re.MULTILINE)
