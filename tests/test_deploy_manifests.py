"""The compose files and templates a new install downloads.

They publish the app on IPv4 only, keep uvicorn from rewriting the real peer
from a forwarding header, and never advise running without a password behind
a reverse proxy. The sign-in copy says what the app actually enforces: from
outside the home network a password alone is not enough, two-factor is needed
too, and the setup code from the container log is the way in until it is on.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
PROD = ROOT / "docker-compose.prod.yml"
DEV = ROOT / "docker-compose.yml"
UNRAID_COMPOSE = ROOT / "unraid" / "docker-compose.yml"
UNRAID_XML = ROOT / "unraid" / "pantryraider.xml"
APPLIANCE = ROOT / "scripts" / "image-build" / "docker-compose.appliance.yml"
REMOTE = ROOT / "scripts" / "image-build" / "docker-compose.remote.yml"
ENV_EXAMPLE = ROOT / ".env.example"
DOCKERFILE = ROOT / "service" / "Dockerfile"

ALL_COMPOSE = (PROD, DEV, UNRAID_COMPOSE, APPLIANCE, REMOTE)
APP_SERVICE = {PROD: "service", DEV: "service", UNRAID_COMPOSE: "pantryraider"}
EM_DASH = "—"


def _compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _comments(path: Path) -> str:
    """Every comment in a file, joined into one lowercase paragraph."""
    parts = []
    for line in path.read_text().splitlines():
        if "#" in line:
            parts.append(line.split("#", 1)[1].strip())
    return " ".join(parts).lower()


def _xml_config(name: str) -> str:
    root = ET.parse(UNRAID_XML).getroot()
    for config in root.iter("Config"):
        if config.get("Name") == name:
            return config.get("Description") or ""
    raise AssertionError(f"no {name} config in the template")


def _auth_text(path: Path) -> str:
    """The Require Login and UI Password advice as written in that file."""
    if path == UNRAID_XML:
        return " ".join((_xml_config("Require Login"), _xml_config("UI Password")))
    return _comments(path)


@pytest.mark.parametrize("path", list(APP_SERVICE), ids=lambda p: p.parent.name + "/" + p.name)
def test_app_port_is_published_on_ipv4_only(path: Path) -> None:
    ports = _compose(path)["services"][APP_SERVICE[path]]["ports"]
    assert ports == ["0.0.0.0:9284:8000"]
    text = path.read_text()
    assert "IPv4 only on purpose" in text


@pytest.mark.parametrize("path", (APPLIANCE, REMOTE), ids=lambda p: p.name)
def test_device_compose_keeps_the_real_peer(path: Path) -> None:
    command = _compose(path)["services"]["service"]["command"]
    assert "--no-proxy-headers" in command


def test_published_image_keeps_the_real_peer() -> None:
    cmd = [line for line in DOCKERFILE.read_text().splitlines() if line.startswith("CMD ")]
    assert len(cmd) == 1 and "--no-proxy-headers" in cmd[0]


@pytest.mark.parametrize("path", ALL_COMPOSE + (DOCKERFILE, ENV_EXAMPLE),
                         ids=lambda p: p.parent.name + "/" + p.name)
def test_no_comment_describes_an_auth_bypass_or_passwordless_proxy(path: Path) -> None:
    comments = _comments(path)
    assert "auth bypass" not in comments
    assert "enabling the auth" not in comments
    # The old advice: turn the password off when a proxy handles login.
    assert not re.search(r"auth_required=false (in \.env )?only when", comments)
    assert "already handles login" not in comments
    assert "already handles the login" not in comments
    assert "already authenticates users" not in comments


@pytest.mark.parametrize("path", (UNRAID_XML, UNRAID_COMPOSE, ENV_EXAMPLE),
                         ids=lambda p: p.parent.name + "/" + p.name)
def test_sign_in_advice_names_two_factor_and_the_setup_code(path: Path) -> None:
    text = _auth_text(path).lower()
    assert "two-factor" in text
    assert "setup code" in text
    assert "reverse proxy or tunnel" in text
    assert "restart the container" in text
    # A password alone is refused from outside the home network, so nothing
    # may present it as enough.
    assert "asks for a password when it is reached through" not in text
    assert "keep true so the app is password protected" not in text


@pytest.mark.parametrize("path, how", ((ENV_EXAMPLE, "docker compose up -d"),
                                       (UNRAID_COMPOSE, "compose up")),
                         ids=("env-example", "unraid-compose"))
def test_password_rescue_says_how_to_apply_the_new_value(path: Path, how: str) -> None:
    # Values from .env or the compose file reach the app only when the
    # container is recreated; a plain restart keeps the old password.
    assert how in _auth_text(path)


def test_unraid_template_parses_and_names_the_home_network_rule() -> None:
    root = ET.parse(UNRAID_XML).getroot()
    assert root.tag == "Container"
    require = _xml_config("Require Login")
    assert "answers only devices on your home network" in require
    assert "two-factor" in require
    password = _xml_config("UI Password")
    assert "setup code" in password
    assert "two-factor" in password


@pytest.mark.parametrize("path", ALL_COMPOSE + (UNRAID_XML, ENV_EXAMPLE, DOCKERFILE),
                         ids=lambda p: p.parent.name + "/" + p.name)
def test_no_em_dash(path: Path) -> None:
    assert EM_DASH not in path.read_text()


# Pantry Raider runs as one uvicorn worker, always (FoodAssistant-n6mta): its
# state files are guarded by in-process locks. Every launch path passes an
# explicit --workers 1 so a WEB_CONCURRENCY variable cannot raise it.
ADDON_DOCKERFILE = ROOT / "homeassistant" / "addon" / "foodassistant" / "Dockerfile"
FIRSTBOOT = ROOT / "scripts" / "image-build" / "firstboot.sh"


def _pins_one_worker(args: list[str]) -> bool:
    return "--workers" in args and args[args.index("--workers") + 1] == "1"


@pytest.mark.parametrize("path", (DOCKERFILE, ADDON_DOCKERFILE),
                         ids=lambda p: p.parent.name + "/" + p.name)
def test_image_command_runs_one_worker(path: Path) -> None:
    import json
    cmd = [line for line in path.read_text().splitlines() if line.startswith("CMD ")]
    assert len(cmd) == 1 and _pins_one_worker(json.loads(cmd[0][4:]))


@pytest.mark.parametrize("path", (DEV, APPLIANCE, REMOTE), ids=lambda p: p.name)
def test_compose_command_runs_one_worker(path: Path) -> None:
    assert _pins_one_worker(_compose(path)["services"]["service"]["command"])


@pytest.mark.parametrize("path", (PROD, UNRAID_COMPOSE), ids=lambda p: p.parent.name + "/" + p.name)
def test_image_compose_keeps_the_image_command(path: Path) -> None:
    # These run the published image, so its one-worker CMD must not be replaced.
    assert "command" not in _compose(path)["services"][APP_SERVICE[path]]


def test_satellite_unit_runs_one_worker() -> None:
    lines = [line for line in FIRSTBOOT.read_text().splitlines()
             if line.startswith("ExecStart=$exec_uvicorn")]
    assert len(lines) == 1 and _pins_one_worker(lines[0].split())


def test_no_launch_path_sets_web_concurrency() -> None:
    for path in ALL_COMPOSE + (DOCKERFILE, ADDON_DOCKERFILE, ENV_EXAMPLE, FIRSTBOOT, UNRAID_XML):
        # Comments may name it; nothing may set it.
        assert not re.search(r"WEB_CONCURRENCY\s*[=:]", path.read_text()), path


def test_install_sh_servers_carry_the_bridge_evidence_variable():
    # services/update_path.py counts FOODASSISTANT_FORCE_MODEL as evidence of
    # an install.sh deployment with a host bridge, so an update can still be
    # offered while the bridge is briefly unreachable (FoodAssistant-8rwkx).
    # install.sh runs firstboot.sh, whose deploy_stack copies the appliance
    # compose on every machine it sets up, x86 servers included, so the
    # variable has to stay on that file's app service.
    env = _compose(APPLIANCE)["services"]["service"]["environment"]
    assert any(str(e).startswith("FOODASSISTANT_FORCE_MODEL=") for e in env)
    firstboot = (ROOT / "scripts" / "image-build" / "firstboot.sh").read_text()
    assert 'COMPOSE_SRC="${COMPOSE_SRC:-$ASSET_DIR/docker-compose.appliance.yml}"' in firstboot
    deploy = firstboot[firstboot.index("deploy_stack() {"):]
    deploy = deploy[:deploy.index("\n}\n")]
    assert set(re.findall(r"docker-compose\.[a-z]+\.yml", deploy)) == {"docker-compose.appliance.yml"}
