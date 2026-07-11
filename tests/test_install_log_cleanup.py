"""The install-log progress cleanup (FoodAssistant-n5ky): a pure JS parser
that collapses Docker's per-layer byte flood into a readable summary. Run
through node so the actual logic is exercised, skipped where node is absent."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HELPERS = (Path(__file__).resolve().parents[1]
           / "service/app/static/js/setup/helpers.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not available")


def _run(lines):
    src = HELPERS.read_text()
    m = re.search(r"function _cleanInstallLog\(lines\) \{.*?\n\}", src, re.S)
    assert m, "could not extract _cleanInstallLog from helpers.js"
    script = m.group(0) + "\nconsole.log(JSON.stringify(_cleanInstallLog(" \
        + json.dumps(lines) + ")));"
    out = subprocess.run(["node", "-e", script], capture_output=True,
                         text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_collapses_the_byte_flood_into_a_summary():
    lines = [" grocy Pulling"] + [
        " a1b2c3d4e5f6 Extracting [==>] %dB/39B" % n for n in range(35, 40)
    ] + [" a1b2c3d4e5f6 Pull complete", " grocy Pulled"]
    r = _run(lines)
    # The hundreds of Extracting lines are gone from the body.
    assert not any("Extracting" in b for b in r["body"])
    assert any("grocy Pulling" in b for b in r["body"])
    assert "grocy: downloaded" in r["status"]


def test_counts_layers_and_names_the_phase():
    lines = [
        " mealie Pulling",
        " aaaaaaaaaaaa Pull complete",
        " bbbbbbbbbbbb Downloading [>] 2MB/600MB",
    ]
    r = _run(lines)
    assert r["status"] == ["mealie: 1/2 layers (downloading)"]


def test_real_messages_pass_through():
    lines = [" A real installer message", " Another line"]
    r = _run(lines)
    assert " A real installer message" in r["body"]
    assert r["status"] == []
