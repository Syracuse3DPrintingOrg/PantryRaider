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


def _completed(stdout: bytes, returncode: int = 0):
    import subprocess
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=b"")


def test_available_false_when_binary_present_but_no_scheduler(monkeypatch):
    # The container always ships the lpstat client, so binary presence alone
    # wrongly reported the stack as ready (hid Install now, showed an empty
    # list). With no reachable scheduler, available() must be False.
    monkeypatch.setattr(printing.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(printing.CupsBackend, "_run",
                        lambda self, args, **kw: _completed(b"lpstat: Unable to connect to server\n", 1))
    assert printing.printing_available() is False


def test_available_true_when_scheduler_running(monkeypatch):
    monkeypatch.setattr(printing.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(printing.CupsBackend, "_run",
                        lambda self, args, **kw: _completed(b"scheduler is running\n"))
    assert printing.printing_available() is True


def test_print_bytes_without_lp_returns_error(monkeypatch):
    monkeypatch.setattr(printing.shutil, "which", lambda name: None)
    result = printing.print_bytes("Zebra_ZD410", b"data")
    assert result.ok is False
    assert result.error


def test_print_bytes_requires_queue():
    result = printing.print_bytes("", b"data")
    assert result.ok is False
    assert "queue" in result.error.lower()


# -- Fleet default resolution (FoodAssistant-7u7z) --------------------------


def test_resolve_effective_queue_prefers_local():
    # A device's own choice always wins, so a device that picked its own printer
    # keeps it even when a fleet default exists.
    assert printing.resolve_effective_queue("MyZebra", "FleetZebra") == "MyZebra"


def test_resolve_effective_queue_falls_back_to_inherited():
    # No local choice: use the fleet default the main server published.
    assert printing.resolve_effective_queue("", "FleetZebra") == "FleetZebra"
    assert printing.resolve_effective_queue("   ", "FleetZebra") == "FleetZebra"


def test_resolve_effective_queue_empty_when_neither():
    assert printing.resolve_effective_queue("", "") == ""
    assert printing.resolve_effective_queue(None, None) == ""


# -- Discovered (cups-browsed) remote queues ---------------------------------
# cups-browsed auto-creates a plain local queue for every remote shared printer
# it finds, so those queues appear in ordinary `lpstat -p` / `lpstat -e` output.
# The existing pure parser handles them with no change: this pins that.


def test_parse_printers_includes_cups_browsed_remote_queue():
    # A queue named after a remote host's printer, exactly as cups-browsed
    # materializes it, parses like any other local queue.
    text = (
        "printer Zebra_ZD410 is idle.  enabled since Tue 08 Jul 2026 09:00:00 AM EDT\n"
        "printer KitchenPi_OfficeLaser is idle.  enabled since Tue 08 Jul 2026\n"
    )
    queues = printing._parse_lpstat_printers(text)
    names = [q["name"] for q in queues]
    assert names == ["Zebra_ZD410", "KitchenPi_OfficeLaser"]
    assert {q["name"]: q["state"] for q in queues}["KitchenPi_OfficeLaser"] == "idle"


# -- Discovery + add: pure helpers (FoodAssistant-r9a4) ----------------------

LPINFO_L = """\
Device: uri = dnssd://Brother%20HL-L8360CDW._ipp._tcp.local./?uuid=abc
        class = network
        info = Brother HL-L8360CDW
        make-and-model = Brother HL-L8360CDW
        device-id =
Device: uri = ipp://192.168.1.50/ipp/print
        class = network
        info = Brother HL-L8360CDW (driverless)
        make-and-model = Brother HL-L8360CDW
        device-id =
Device: uri = socket
        class = network
        info = AppSocket/HP JetDirect
        make-and-model = Unknown
        device-id =
Device: uri = socket://192.168.1.77:9100
        class = network
        info = Zebra ZM400
        make-and-model = Zebra ZM400
        device-id =
"""

LPSTAT_V = """\
device for OfficeLaser: ipp://192.168.1.50/ipp/print
device for OldRaw: socket://10.0.0.9:9100
"""


def test_parse_lpinfo_v_classifies_and_skips_bare_scheme():
    devices = printing._parse_lpinfo_v(LPINFO_L)
    uris = [d["uri"] for d in devices]
    # The bare "socket" backend (no ://target) is skipped.
    assert "socket" not in uris
    assert uris == [
        "dnssd://Brother%20HL-L8360CDW._ipp._tcp.local./?uuid=abc",
        "ipp://192.168.1.50/ipp/print",
        "socket://192.168.1.77:9100",
    ]
    by_uri = {d["uri"]: d for d in devices}
    assert by_uri["ipp://192.168.1.50/ipp/print"]["kind"] == "driverless"
    assert by_uri["ipp://192.168.1.50/ipp/print"]["driver"] == "everywhere"
    assert by_uri["socket://192.168.1.77:9100"]["kind"] == "socket"
    assert by_uri["socket://192.168.1.77:9100"]["driver"] == "raw"


def test_parse_lpstat_v_collects_device_uris():
    mapping = printing._parse_lpstat_v(LPSTAT_V)
    assert mapping == {"OfficeLaser": "ipp://192.168.1.50/ipp/print",
                       "OldRaw": "socket://10.0.0.9:9100"}


def test_sanitize_queue_name_keeps_valid_rejects_unsafe():
    assert printing.sanitize_queue_name("KitchenLaser") == "KitchenLaser"
    assert printing.sanitize_queue_name("  Zebra_ZD-410 ") == "Zebra_ZD-410"
    # Spaces, slashes, and shell metacharacters are rejected outright.
    for bad in ["has space", "a/b", "a;rm -rf", "a$(x)", "a`b`", "a|b", "a&b",
                "", "   ", "a b/c"]:
        assert printing.sanitize_queue_name(bad) == ""


def test_valid_connection_requires_scheme_and_safe_chars():
    assert printing.valid_connection("ipp://192.168.1.5/ipp/print") is True
    assert printing.valid_connection("socket://10.0.0.9:9100") is True
    assert printing.valid_connection(
        "dnssd://Brother%20HL._ipp._tcp.local/") is True
    # No scheme, or shell-unsafe characters.
    assert printing.valid_connection("192.168.1.5") is False
    assert printing.valid_connection("socket://h;rm -rf/") is False
    assert printing.valid_connection("ipp://h/`id`") is False
    assert printing.valid_connection("") is False


def test_add_printer_args_driverless_and_socket():
    assert printing.add_printer_args(
        "Brother", "ipp://192.168.1.50/ipp/print", "everywhere") == [
        "lpadmin", "-p", "Brother", "-E", "-v",
        "ipp://192.168.1.50/ipp/print", "-m", "everywhere"]
    # Empty model on a socket add falls back to a raw queue.
    assert printing.add_printer_args(
        "Zebra", "socket://192.168.1.77:9100", "") == [
        "lpadmin", "-p", "Zebra", "-E", "-v",
        "socket://192.168.1.77:9100", "-m", "raw"]


def test_add_printer_args_rejects_unsafe_name_and_connection():
    import pytest
    with pytest.raises(ValueError):
        printing.add_printer_args("bad name", "ipp://h/ipp/print", "everywhere")
    with pytest.raises(ValueError):
        printing.add_printer_args("Good", "socket://h;reboot", "raw")


def test_discover_printers_excludes_configured_queues(monkeypatch):
    monkeypatch.setattr(printing.shutil, "which", lambda name: "/usr/bin/" + name)

    class FakeProc:
        def __init__(self, stdout):
            self.stdout = stdout.encode()
            self.stderr = b""
            self.returncode = 0

    def fake_run(self, args, *, input_bytes=None):
        if args[0] == "lpinfo":
            return FakeProc(LPINFO_L)
        if args[:2] == ["lpstat", "-v"]:
            return FakeProc(LPSTAT_V)
        return FakeProc("")

    monkeypatch.setattr(printing.CupsBackend, "_run", fake_run)
    found = printing.discover_printers()
    uris = [p["uri"] for p in found]
    # ipp://192.168.1.50/... is already configured (OfficeLaser) so it is excluded;
    # the dnssd Brother and the Zebra socket remain.
    assert "ipp://192.168.1.50/ipp/print" not in uris
    assert "dnssd://Brother%20HL-L8360CDW._ipp._tcp.local./?uuid=abc" in uris
    assert "socket://192.168.1.77:9100" in uris
    # Suggested names are always valid queue names.
    for p in found:
        assert printing.sanitize_queue_name(p["name"]) == p["name"]


def test_discover_printers_empty_when_no_lpinfo(monkeypatch):
    monkeypatch.setattr(printing.shutil, "which", lambda name: None)
    assert printing.discover_printers() == []


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


def test_add_printer_args_zebra_zpl_uses_bundled_ppd():
    # A Zebra label printer adds with the bundled ZPL PPD (-P path), not -m,
    # so rendered labels rasterize through rastertolabel (FoodAssistant-zqh0).
    args = printing.add_printer_args("Zebra_ZM400", "socket://192.168.1.233:9100", "zebra-zpl")
    assert args[:5] == ["lpadmin", "-p", "Zebra_ZM400", "-E", "-v"]
    assert args[5] == "socket://192.168.1.233:9100"
    assert args[6] == "-P"
    assert args[7] == printing.PPD_DRIVERS["zebra-zpl"]
    assert "-m" not in args


def test_add_printer_args_socket_still_uses_raw():
    args = printing.add_printer_args("Raw1", "socket://10.0.0.5:9100", "raw")
    assert args[-2:] == ["-m", "raw"]
