"""The label-printing CUPS sidecar stays private and carries its own config.

Adding a printer on that cupsd needs no password, because the app runs lpadmin
against it with no credential. What keeps that from becoming a network-wide
"add or delete any printer" endpoint is that nothing but the app can reach it:
the print service publishes no port and sits on a network only the app joins,
and the app talks to it through a socket in a shared volume. Its config is
written from text inside the compose file itself, checked with cupsd -t first,
so nothing is downloaded at start and a bad config never replaces a good one.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
PROD = ROOT / "docker-compose.prod.yml"
DEV = ROOT / "docker-compose.yml"
COMPOSE_FILES = (DEV, PROD)
CUPSD_CONF = ROOT / "docker" / "cups" / "cupsd.conf"

HEREDOC = re.compile(
    r"^cat > /etc/cups/cupsd\.conf\.pantryraider <<'CUPSD'\n(?P<body>.*?\n)CUPSD\n",
    re.MULTILINE | re.DOTALL,
)
SERVICE_NETWORKS = {PROD: ["default", "updater", "printing"], DEV: ["default", "printing"]}


def _compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _cups(path: Path) -> dict:
    return _compose(path)["services"]["cups"]


def _command(path: Path) -> str:
    command = _cups(path)["command"]
    assert isinstance(command, list) and len(command) == 1, path.name
    return command[0]


def _env(entries) -> dict[str, str]:
    if isinstance(entries, dict):
        return {str(k): str(v) for k, v in entries.items()}
    return dict(str(e).split("=", 1) for e in entries or [])


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_print_service_publishes_no_port(path: Path) -> None:
    cups = _cups(path)
    assert "ports" not in cups
    assert "network_mode" not in cups


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_print_service_joins_only_the_printing_network(path: Path) -> None:
    assert _cups(path)["networks"] == ["printing"]


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_print_service_keeps_its_queues_and_shares_only_the_socket(path: Path) -> None:
    # cups-config keeps its old name, so queues added before the switch survive.
    assert _cups(path)["volumes"] == ["cups-config:/etc/cups", "cups-run:/run/cups"]


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_print_service_downloads_nothing_at_start(path: Path) -> None:
    command = _command(path)
    for word in ("wget", "curl", "http://", "https://", "CUPS_CONFIG_REF"):
        assert word not in command, f"{path.name}: {word!r} in the cups command"
    # No compose interpolation either: the text is taken literally.
    assert "$" not in command


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_print_service_checks_config_then_locks_print_account_then_starts(path: Path) -> None:
    cups = _cups(path)
    assert cups["entrypoint"] == ["/bin/sh", "-c"]
    command = _command(path)
    assert command.startswith("set -e\n")
    steps = ["cupsd -t -c", "passwd -l print", "exec /usr/sbin/cupsd -f"]
    at = [command.find(step) for step in steps]
    assert all(i >= 0 for i in at), dict(zip(steps, at))
    assert at == sorted(at), dict(zip(steps, at))
    # The config is put in place only when the check passes.
    check = command.index("if /usr/sbin/cupsd -t -c /etc/cups/cupsd.conf.pantryraider; then")
    move = command.index("mv -f /etc/cups/cupsd.conf.pantryraider /etc/cups/cupsd.conf")
    other = command.index("else\n", check)
    assert check < move < other
    assert command.rstrip("\n").endswith("exec /usr/sbin/cupsd -f")


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_inline_config_equals_the_repo_config_byte_for_byte(path: Path) -> None:
    match = HEREDOC.search(_command(path))
    assert match, f"{path.name}: no quoted CUPSD heredoc"
    assert match.group("body") == CUPSD_CONF.read_text()
    # The terminator stands alone, so the shell ends the heredoc exactly there.
    assert _command(path).count("\nCUPSD\n") == 1


def test_both_compose_files_carry_the_same_print_command() -> None:
    assert _command(DEV) == _command(PROD)


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_app_joins_the_printing_network_and_keeps_the_default_one(path: Path) -> None:
    # Listing only "printing" would cut the app off from Grocy and Mealie.
    assert _compose(path)["services"]["service"]["networks"] == SERVICE_NETWORKS[path]


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_app_mounts_the_socket_volume_and_never_the_config(path: Path) -> None:
    volumes = _compose(path)["services"]["service"]["volumes"]
    assert "cups-run:/run/cups" in volumes
    # A fresh named volume is seeded by whichever container mounts it first;
    # the app image's /etc/cups is not cupsd's config.
    assert not any(str(v).split(":", 1)[0] == "cups-config" for v in volumes)


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_app_prints_through_the_socket_by_default(path: Path) -> None:
    env = _env(_compose(path)["services"]["service"]["environment"])
    assert env["CUPS_SERVER"] == "${CUPS_SERVER:-/run/cups/cups.sock}"


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_printing_network_and_socket_volume_are_declared(path: Path) -> None:
    compose = _compose(path)
    # An ordinary bridge, not internal: cupsd has to reach network printers.
    assert compose["networks"]["printing"] == {"driver": "bridge"}
    assert "cups-run" in compose["volumes"]
    assert "cups-config" in compose["volumes"]


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_nothing_else_joins_the_printing_network(path: Path) -> None:
    services = _compose(path)["services"]
    joined = {name for name, svc in services.items()
              if "printing" in (svc.get("networks") or [])}
    assert joined == {"service", "cups"}
