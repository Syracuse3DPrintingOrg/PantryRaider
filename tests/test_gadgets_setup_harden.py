"""foodassistant-gadgets-setup gives the reader its key before it starts.

The reader set up after the 0.21 update must start with its key drop-in in
place, so the host-harden call sits after the unit is written and before
the restart.
"""
from __future__ import annotations

from pathlib import Path

SETUP = Path(__file__).resolve().parents[1] / "scripts" / "image-build" / "foodassistant-gadgets-setup"


def _line(lines, needle):
    hits = [i for i, line in enumerate(lines) if needle in line and not line.lstrip().startswith("#")]
    assert hits, needle
    return hits[0]


def test_host_harden_runs_after_the_unit_write_and_before_the_restart():
    lines = SETUP.read_text(encoding="utf-8").splitlines()
    unit_write = _line(lines, '> "/etc/systemd/system/$GD_SERVICE"')
    reload_ = _line(lines, "systemctl daemon-reload")
    harden = _line(lines, "/usr/local/bin/foodassistant-host-harden local-credentials")
    restart = _line(lines, 'systemctl restart "$GD_SERVICE"')
    assert unit_write < reload_ < harden < restart


def test_host_harden_is_optional():
    text = SETUP.read_text(encoding="utf-8")
    assert "[ -x /usr/local/bin/foodassistant-host-harden ]" in text
