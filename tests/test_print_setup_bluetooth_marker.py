"""Bluetooth print stack marker (FoodAssistant-h2j6).

foodassistant-print-setup's write_env_fragment() is supposed to write a
BLUETOOTH_PRINTING=1 marker into the stack .env whenever Bluetooth printing is
enabled, so an OTA re-assert (foodassistant-update, which runs the helper with
no ENABLE_BLUETOOTH_PRINTING) keeps re-installing the Supvan bridge. Before
this change the header comment claimed this but nothing wrote it. These tests
source the script (FA_PRINT_SETUP_SOURCE=1 skips main()) and call
write_env_fragment directly against a scratch INSTALL_DIR, no apt/systemd
needed.

Run: python -m pytest tests/test_print_setup_bluetooth_marker.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "image-build" / "foodassistant-print-setup"


def _run_write_env_fragment(tmp_path, extra_env: dict) -> str:
    install_dir = tmp_path / "stack"
    install_dir.mkdir(exist_ok=True)
    env_file = install_dir / ".env"
    env = {
        **os.environ,
        "FA_PRINT_SETUP_SOURCE": "1",
        "INSTALL_DIR": str(install_dir),
        "ENV_FILE": str(env_file),
        "SKIP_APT": "1",
    }
    env.update(extra_env)
    proc = subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"; write_env_fragment'],
        env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return env_file.read_text() if env_file.exists() else ""


def test_script_is_valid_bash():
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_bluetooth_enabled_by_env_writes_marker(tmp_path):
    text = _run_write_env_fragment(tmp_path, {"ENABLE_BLUETOOTH_PRINTING": "1"})
    assert "BLUETOOTH_PRINTING=1" in text
    # The base print marker still gets written alongside it.
    assert "PRINTING_ENABLED=1" in text


def test_bluetooth_disabled_writes_no_marker(tmp_path):
    text = _run_write_env_fragment(tmp_path, {})
    assert "BLUETOOTH_PRINTING" not in text
    assert "PRINTING_ENABLED=1" in text


def test_bluetooth_marker_already_present_is_reasserted_without_env(tmp_path):
    # Simulates an OTA re-assert: foodassistant-update runs the helper with no
    # ENABLE_BLUETOOTH_PRINTING, relying on a marker from a prior run.
    install_dir = tmp_path / "stack"
    install_dir.mkdir()
    env_file = install_dir / ".env"
    env_file.write_text("BLUETOOTH_PRINTING=1\n")
    text = _run_write_env_fragment(tmp_path, {})
    assert text.count("BLUETOOTH_PRINTING=1") == 1  # replaced in place, not duplicated
    assert "PRINTING_ENABLED=1" in text


def test_bluetooth_marker_not_duplicated_on_rerun(tmp_path):
    install_dir = tmp_path / "stack"
    install_dir.mkdir()
    env_file = install_dir / ".env"
    env = {
        **os.environ,
        "FA_PRINT_SETUP_SOURCE": "1",
        "INSTALL_DIR": str(install_dir),
        "ENV_FILE": str(env_file),
        "SKIP_APT": "1",
        "ENABLE_BLUETOOTH_PRINTING": "1",
    }
    for _ in range(2):
        proc = subprocess.run(
            ["bash", "-c", f'source "{SCRIPT}"; write_env_fragment'],
            env=env, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
    text = env_file.read_text()
    assert text.count("BLUETOOTH_PRINTING=1") == 1
