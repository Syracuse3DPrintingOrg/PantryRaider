"""Supvan bridge listen address (FoodAssistant-9yuwu).

The bridge serves its status page and IPP on one listener with no access
control, so foodassistant-print-setup writes the address it binds to into the
unit: every interface by default (the main server reads a Bandit's bridge page
and prints to it), or SUPVAN_HOST from the environment or the stack .env.
These tests source the script and call write_supvan_unit against scratch paths
with a stub systemctl, so nothing touches the host.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "image-build" / "foodassistant-print-setup"


def _write_unit(tmp_path, env_text: str | None = None, **extra) -> tuple[str, str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    (stubs / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    (stubs / "systemctl").chmod(0o755)
    install_dir = tmp_path / "stack"
    install_dir.mkdir(exist_ok=True)
    env_file = install_dir / ".env"
    if env_text is not None:
        env_file.write_text(env_text)
    unit = tmp_path / "units" / "foodassistant-supvan-app.service"
    env = {k: v for k, v in os.environ.items() if k != "SUPVAN_HOST"}
    env.update({
        "FA_PRINT_SETUP_SOURCE": "1",
        "INSTALL_DIR": str(install_dir),
        "ENV_FILE": str(env_file),
        "SKIP_APT": "1",
        "SUPVAN_UNIT": str(unit),
        "SUPVAN_MODELS_DIR": str(tmp_path / "models"),
        "SUPVAN_MODELS_FILE": str(tmp_path / "models" / "models.toml"),
        "PATH": f"{stubs}:{os.environ['PATH']}",
    })
    env.update(extra)
    proc = subprocess.run(["bash", "-c", f'source "{SCRIPT}"; write_supvan_unit'],
                          env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return unit.read_text(), proc.stdout + proc.stderr


def test_default_listens_on_every_interface_for_fleet_printing(tmp_path):
    text, out = _write_unit(tmp_path)
    assert "Environment=SUPVAN_HOST=0.0.0.0\n" in text
    assert "IPP on 0.0.0.0:8631" in out


def test_environment_can_keep_the_bridge_on_this_device(tmp_path):
    text, out = _write_unit(tmp_path, SUPVAN_HOST="127.0.0.1")
    assert "Environment=SUPVAN_HOST=127.0.0.1\n" in text
    assert "IPP on 127.0.0.1:8631" in out


def test_stack_env_choice_survives_an_update_run(tmp_path):
    text, _ = _write_unit(tmp_path, "PRINTING_ENABLED=1\nSUPVAN_HOST=127.0.0.1\n")
    assert "Environment=SUPVAN_HOST=127.0.0.1\n" in text


def test_a_value_that_is_not_an_address_falls_back(tmp_path):
    text, out = _write_unit(tmp_path, SUPVAN_HOST="0.0.0.0\nExecStart=/bin/evil")
    assert "Environment=SUPVAN_HOST=0.0.0.0\n" in text
    assert "/bin/evil" not in text
    assert "not an address" in out
