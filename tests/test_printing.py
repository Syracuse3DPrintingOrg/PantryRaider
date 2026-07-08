"""Print backend: pure lpstat parsing and graceful absence of CUPS
(FoodAssistant-yg41). No real CUPS server touched."""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import printing  # noqa: E402


LPSTAT_P = """\
printer Zebra_ZD410 is idle.  enabled since Tue 08 Jul 2026 09:00:00 AM EDT
printer OfficeLaser is now printing job-42.  enabled since Tue 08 Jul 2026
printer OldInkjet disabled since Mon 07 Jul 2026 - paused
"""

LPSTAT_E = """\
Zebra_ZD410
OfficeLaser
OldInkjet
"""

LPSTAT_D = "system default destination: OfficeLaser\n"
LPSTAT_D_NONE = "no system default destination\n"


def test_parse_printers_names_and_state():
    queues = printing._parse_lpstat_printers(LPSTAT_P)
    assert [q["name"] for q in queues] == ["Zebra_ZD410", "OfficeLaser", "OldInkjet"]
    by_name = {q["name"]: q["state"] for q in queues}
    assert by_name["Zebra_ZD410"] == "idle"
    assert by_name["OfficeLaser"] == "now"  # "is now printing" -> first token after "is"
    assert by_name["OldInkjet"] == "unknown"  # no "is" clause in that line


def test_parse_printers_from_bare_name_list():
    queues = printing._parse_lpstat_printers(LPSTAT_E)
    assert [q["name"] for q in queues] == ["Zebra_ZD410", "OfficeLaser", "OldInkjet"]
    assert all(q["state"] == "unknown" for q in queues)


def test_parse_printers_dedupes_and_skips_blank():
    text = "printer A is idle.\n\nprinter A is idle.\nprinter B is idle.\n"
    queues = printing._parse_lpstat_printers(text)
    assert [q["name"] for q in queues] == ["A", "B"]


def test_parse_default():
    assert printing._parse_lpstat_default(LPSTAT_D) == "OfficeLaser"
    assert printing._parse_lpstat_default(LPSTAT_D_NONE) == ""
    assert printing._parse_lpstat_default("") == ""


def test_merge_default_flags():
    queues = printing._parse_lpstat_printers(LPSTAT_P)
    merged = printing._merge_default(queues, "OfficeLaser")
    flags = {q["name"]: q["is_default"] for q in merged}
    assert flags == {"Zebra_ZD410": False, "OfficeLaser": True, "OldInkjet": False}


def test_parse_lp_job_id():
    out = "request id is Zebra_ZD410-57 (1 file(s))"
    assert printing._parse_lp_job_id(out) == "Zebra_ZD410-57"
    assert printing._parse_lp_job_id("no id here") == ""


def test_list_queues_empty_when_no_lpstat(monkeypatch):
    monkeypatch.setattr(printing.shutil, "which", lambda name: None)
    assert printing.list_queues() == []
    assert printing.printing_available() is False


def test_print_bytes_without_lp_returns_error(monkeypatch):
    monkeypatch.setattr(printing.shutil, "which", lambda name: None)
    result = printing.print_bytes("Zebra_ZD410", b"data")
    assert result.ok is False
    assert result.error


def test_print_bytes_requires_queue():
    result = printing.print_bytes("", b"data")
    assert result.ok is False
    assert "queue" in result.error.lower()


def test_list_queues_parses_end_to_end(monkeypatch):
    # Simulate a working CUPS by stubbing the backend's subprocess runner.
    monkeypatch.setattr(printing.shutil, "which", lambda name: "/usr/bin/" + name)

    class FakeProc:
        def __init__(self, stdout):
            self.stdout = stdout.encode()
            self.stderr = b""
            self.returncode = 0

    def fake_run(self, args, *, input_bytes=None):
        if args[:2] == ["lpstat", "-p"]:
            return FakeProc(LPSTAT_P)
        if args[:2] == ["lpstat", "-d"]:
            return FakeProc(LPSTAT_D)
        return FakeProc("")

    monkeypatch.setattr(printing.CupsBackend, "_run", fake_run)
    queues = printing.list_queues()
    assert [q["name"] for q in queues] == ["Zebra_ZD410", "OfficeLaser", "OldInkjet"]
    assert {q["name"] for q in queues if q["is_default"]} == {"OfficeLaser"}
