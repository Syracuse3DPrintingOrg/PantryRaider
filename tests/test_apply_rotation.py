"""Tests for foodassistant-apply-rotation (FoodAssistant-ly82).

The helper applies the saved kiosk rotation via wlr-randr. It used to rotate
only the FIRST output the compositor listed; on a Pi driving the official
7-inch DSI panel, an HDMI connector can enumerate ahead of DSI-1, so the panel
never rotated. These tests run the real script against a stub wlr-randr (on
PATH) and a fake runtime dir, using the script's env test hooks, and assert
that every listed output receives the transform.

Run: python -m pytest tests/test_apply_rotation.py -q
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "image-build" / "foodassistant-apply-rotation"

# A stub wlr-randr: with no args it lists outputs (name lines start in column
# 0, details indented, mirroring the real tool); with --output it records the
# call to a log file so the test can see which outputs were rotated.
_STUB = """#!/usr/bin/env bash
if [ "$#" -eq 0 ]; then
  printf '@LISTING@'
  exit 0
fi
echo "$@" >> "$WLR_LOG"
exit "${WLR_EXIT:-0}"
"""


def _run(tmp_path, outputs_listing, rotation="90", wlr_exit_for=None):
    """Run the script with a stubbed environment; return (proc, log_lines)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "wlr.log"
    log.write_text("")
    stub = bindir / "wlr-randr"
    stub.write_text(_STUB.replace("@LISTING@", outputs_listing))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    run_root = tmp_path / "run-user"
    (run_root / "1000").mkdir(parents=True)
    (run_root / "1000" / "wayland-0").write_text("")

    rot_file = tmp_path / "kiosk-rotation"
    rot_file.write_text(rotation + "\n")

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["WLR_LOG"] = str(log)
    env["FOODASSISTANT_ROTATION_FILE"] = str(rot_file)
    env["FOODASSISTANT_RUN_ROOT"] = str(run_root)
    if wlr_exit_for is not None:
        env["WLR_EXIT"] = wlr_exit_for
    proc = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30
    )
    lines = [l for l in log.read_text().splitlines() if l.strip()]
    return proc, lines


TWO_OUTPUTS = (
    'HDMI-A-1 "(null) (HDMI-A-1)"\\n'
    "  Enabled: yes\\n"
    "  Transform: normal\\n"
    'DSI-1 "(null) (DSI-1)"\\n'
    "  Enabled: yes\\n"
    "  Transform: normal\\n"
)


def test_rotates_every_listed_output_not_just_the_first(tmp_path):
    proc, calls = _run(tmp_path, TWO_OUTPUTS, rotation="90")
    assert proc.returncode == 0, proc.stderr
    rotated = {c.split()[1] for c in calls if c.startswith("--output")}
    assert rotated == {"HDMI-A-1", "DSI-1"}
    assert all("--transform 90" in c for c in calls)


def test_single_output_still_works(tmp_path):
    listing = 'DSI-1 "(null) (DSI-1)"\\n  Enabled: yes\\n'
    proc, calls = _run(tmp_path, listing, rotation="180")
    assert proc.returncode == 0, proc.stderr
    assert calls == ["--output DSI-1 --transform 180"]


def test_missing_rotation_file_defaults_to_normal(tmp_path):
    listing = 'DSI-1 "(null) (DSI-1)"\\n'
    bin_run = _run(tmp_path, listing, rotation="")
    # An empty file means normal (no rotation), still applied explicitly so a
    # previously rotated output returns to normal.
    proc, calls = bin_run
    assert proc.returncode == 0, proc.stderr
    assert calls == ["--output DSI-1 --transform normal"]


def test_fails_only_when_no_output_takes_the_transform(tmp_path):
    proc, calls = _run(tmp_path, TWO_OUTPUTS, rotation="90", wlr_exit_for="1")
    assert proc.returncode != 0
    assert len(calls) == 2  # it still tried every output
