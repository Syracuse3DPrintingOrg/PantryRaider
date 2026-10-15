"""A state file holding valid JSON of the wrong shape must not break a read.

The cross-surface state files (timers, scanner mode, on-screen events) are
plain JSON objects. A file that parses but holds a list or a string used to
reach the loaders' .get() calls and raise AttributeError: the mtime was never
advanced, so every following poll raised again and /events/poll stopped
answering entirely. A wrong shape now reads exactly like a torn file: keep the
in-memory state, let the next save overwrite it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import ha_events, scanner_mode, timers  # noqa: E402

# Valid JSON that is not an object, which is what the loaders assumed.
WRONG_SHAPES = ["[]", '["timers"]', '"a string"', "42", "null"]


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    timers.clear_all()
    ha_events.reset()
    scanner_mode.reset()
    yield
    timers.clear_all()
    ha_events.reset()
    # Scanner mode lives in a module-global that outlives the tmp_path, so a
    # test that leaves it on "shopping" hands that mode to whatever file runs
    # next (this one sorts immediately before test_stemma.py, which reads the
    # mode back out of /gadgets/outputs).
    scanner_mode.reset()


@pytest.mark.parametrize("body", WRONG_SHAPES)
def test_timers_survive_a_wrong_shaped_state_file(tmp_path, body):
    timers.create_timer("Bread", 600)
    timers._mtime = None  # a second worker that has not read the file yet
    (tmp_path / "timers.json").write_text(body)
    assert [t["label"] for t in timers.list_timers()] == ["Bread"]
    # And still readable on the next poll, rather than raising again.
    assert len(timers.list_timers()) == 1


@pytest.mark.parametrize("body", WRONG_SHAPES)
def test_scanner_mode_survives_a_wrong_shaped_state_file(tmp_path, body):
    scanner_mode.set_mode("consume")
    scanner_mode._state["mode"] = "inventory"
    scanner_mode._state["mtime"] = None
    (tmp_path / "scanner_mode.json").write_text(body)
    assert scanner_mode.get_mode() == "inventory"


@pytest.mark.parametrize("body", WRONG_SHAPES)
def test_event_poll_survives_a_wrong_shaped_state_file(tmp_path, body):
    ha_events.add_notification("Dishwasher finished")
    ha_events._mtime = None
    (tmp_path / "ha_events.json").write_text(body)
    out = ha_events.poll(0)
    assert isinstance(out, dict)
    assert ha_events.poll(0) == out  # every following poll answers too


def test_a_real_state_file_still_loads(tmp_path):
    """The guard must not swallow a correctly shaped file."""
    scanner_mode.set_mode("shopping")
    scanner_mode._state["mode"] = "inventory"
    scanner_mode._state["mtime"] = None
    assert json.loads((tmp_path / "scanner_mode.json").read_text())["mode"] == "shopping"
    assert scanner_mode.get_mode() == "shopping"
