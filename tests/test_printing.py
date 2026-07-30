"""Print backend: pure lpstat parsing and graceful absence of CUPS
(FoodAssistant-yg41). No real CUPS server touched."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("bad_queue", [
    "-oraw",        # a leading dash could be read as an lp option
    "my queue",     # whitespace
    "queue\nname",  # embedded newline / control char
    " leading",     # untrimmed
])
def test_print_bytes_rejects_implausible_queue_names(monkeypatch, bad_queue):
    # Defense in depth: even though the command is argv (no shell), an
    # implausible queue name is refused before lp is invoked.
    monkeypatch.setattr(printing.shutil, "which", lambda name: "/usr/bin/lp")
    result = printing.print_bytes(bad_queue, b"data")
    assert result.ok is False
    assert "queue" in result.error.lower()


# -- Powered-off printer detection (FoodAssistant-fu62) ---------------------


@pytest.mark.parametrize("text", [
    "lp: Unable to print file: Host is down",
    "HOST IS DOWN",
    "connect: No route to host",
    "backend failed: Connection timed out",
    "Operation timed out after 30000 milliseconds",
    "The printer is not connected.",
    "Printer not connected",
    "printer is offline",
    "Could not connect to printer",
    "Connection refused",
    "rfcomm: Device or resource busy",
    "connect: No such device",
    "Network host is down",
])
def test_classify_print_error_recognizes_offline_signatures(text):
    assert printing.classify_print_error(text) == "offline"


@pytest.mark.parametrize("text", [
    "lp: Unsupported document-format",
    "Bad request",
    "The printer queue does not exist.",
    "client-error-not-found",
    "Permission denied",
])
def test_classify_print_error_other_failures_stay_other(text):
    assert printing.classify_print_error(text) == "other"


def test_classify_print_error_blank_text_is_other():
    assert printing.classify_print_error("") == "other"
    assert printing.classify_print_error("   ") == "other"


def test_print_bytes_classifies_offline_error_from_stderr(monkeypatch):
    monkeypatch.setattr(printing.shutil, "which", lambda name: "/usr/bin/" + name)

    def fake_run(args, **kwargs):
        import subprocess
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout=b"",
            stderr=b"lp: Unable to contact printer: Host is down\n")

    monkeypatch.setattr(printing.subprocess, "run", fake_run)
    result = printing.print_bytes("Supvan_kitchen", b"data")
    assert result.ok is False
    assert result.error_kind == "offline"


def test_print_bytes_other_error_is_not_classified_offline(monkeypatch):
    monkeypatch.setattr(printing.shutil, "which", lambda name: "/usr/bin/" + name)

    def fake_run(args, **kwargs):
        import subprocess
        return subprocess.CompletedProcess(
            args=args, returncode=1, stdout=b"",
            stderr=b"lp: The printer or class does not exist.\n")

    monkeypatch.setattr(printing.subprocess, "run", fake_run)
    result = printing.print_bytes("Supvan_kitchen", b"data")
    assert result.ok is False
    assert result.error_kind == "other"


def test_print_bytes_timeout_is_classified_offline_and_bounded(monkeypatch):
    """A hung connect attempt (a sleeping Bluetooth printer) must never hang
    the request forever: it is caught as a timeout and reported as offline."""
    monkeypatch.setattr(printing.shutil, "which", lambda name: "/usr/bin/" + name)

    def fake_run(args, **kwargs):
        import subprocess
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(printing.subprocess, "run", fake_run)
    result = printing.print_bytes("Supvan_kitchen", b"data")
    assert result.ok is False
    assert result.error_kind == "offline"
    assert result.error


def test_print_bytes_success_has_no_error_kind(monkeypatch):
    monkeypatch.setattr(printing.shutil, "which", lambda name: "/usr/bin/" + name)

    def fake_run(args, **kwargs):
        import subprocess
        return subprocess.CompletedProcess(
            args=args, returncode=0,
            stdout=b"request id is Supvan_kitchen-7 (1 file(s))\n", stderr=b"")

    monkeypatch.setattr(printing.subprocess, "run", fake_run)
    result = printing.print_bytes("Supvan_kitchen", b"data")
    assert result.ok is True
    assert result.error_kind == ""
    assert result.job_id == "Supvan_kitchen-7"


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
    # Pin the hostname so the self-import dedup never depends on the machine
    # running the tests.
    monkeypatch.setenv("FOODASSISTANT_HOSTNAME", "testhost")
    queues = printing.list_queues()
    assert [q["name"] for q in queues] == ["Zebra_ZD410", "OfficeLaser", "OldInkjet"]
    assert {q["name"] for q in queues if q["is_default"]} == {"OfficeLaser"}


# -- Self-import dedup (FoodAssistant-zq6b) ----------------------------------
# cups-browsed re-imports a device's own shared queues as <name>_<hostname>
# duplicates; the app hides them from the queue list rather than fighting
# cups-browsed configuration.


def _queues(*names, default=""):
    return [{"name": n, "state": "idle", "is_default": n == default}
            for n in names]


def test_dedupe_hides_own_hostname_duplicate():
    queues = _queues("Zebra_ZM400", "Zebra_ZM400_foodassistant")
    out = printing.dedupe_self_imports(queues, "foodassistant")
    assert [q["name"] for q in out] == ["Zebra_ZM400"]


def test_dedupe_matches_fqdn_and_short_hostname():
    # cups-browsed may suffix with the short host while gethostname() returns
    # the FQDN (or the reverse); both forms of the suffix are recognized.
    queues = _queues("Brother", "Brother_foodassistant")
    out = printing.dedupe_self_imports(queues, "foodassistant.local")
    assert [q["name"] for q in out] == ["Brother"]
    # And a dotted suffix folded to queue-name characters is caught too.
    queues = _queues("Brother", "Brother_foodassistant_local")
    out = printing.dedupe_self_imports(queues, "foodassistant.local")
    assert [q["name"] for q in out] == ["Brother"]


def test_dedupe_is_case_insensitive_on_the_suffix():
    queues = _queues("Zebra", "Zebra_FoodAssistant")
    out = printing.dedupe_self_imports(queues, "foodassistant")
    assert [q["name"] for q in out] == ["Zebra"]


def test_dedupe_keeps_suffix_queue_when_base_is_missing():
    # Another fleet device's imported queue has no local base here: kept, so
    # remote printers stay usable.
    queues = _queues("Zebra_ZM400_korolev", "OfficeLaser")
    out = printing.dedupe_self_imports(queues, "korolev")
    assert [q["name"] for q in out] == ["Zebra_ZM400_korolev", "OfficeLaser"]


def test_dedupe_keeps_unrelated_underscore_names():
    queues = _queues("Office_Laser", "HP_LaserJet", "Zebra_ZD410")
    out = printing.dedupe_self_imports(queues, "foodassistant")
    assert [q["name"] for q in out] == ["Office_Laser", "HP_LaserJet", "Zebra_ZD410"]


def test_dedupe_hides_series_duplicate_only_with_base_present():
    # The driverless self-import sometimes lands as "<name>_series": hidden
    # only when the base queue exists, so a printer genuinely named
    # "..._series" (no base) is never touched.
    queues = _queues("HP_Envy", "HP_Envy_series")
    out = printing.dedupe_self_imports(queues, "foodassistant")
    assert [q["name"] for q in out] == ["HP_Envy"]

    alone = _queues("HP_LaserJet_400_series")
    out = printing.dedupe_self_imports(alone, "foodassistant")
    assert [q["name"] for q in out] == ["HP_LaserJet_400_series"]


def test_dedupe_never_hides_a_configured_queue():
    # Someone picked the suffixed queue as their printer before the dedup
    # existed: hiding it would break their settings, so protected names stay.
    queues = _queues("Zebra_ZM400", "Zebra_ZM400_foodassistant")
    out = printing.dedupe_self_imports(
        queues, "foodassistant", protected={"Zebra_ZM400_foodassistant"})
    assert [q["name"] for q in out] == ["Zebra_ZM400", "Zebra_ZM400_foodassistant"]


def test_dedupe_empty_hostname_only_hides_series():
    queues = _queues("Zebra", "Zebra_foodassistant", "Zebra_series")
    out = printing.dedupe_self_imports(queues, "")
    assert [q["name"] for q in out] == ["Zebra", "Zebra_foodassistant"]


def test_device_hostname_env_override(monkeypatch):
    monkeypatch.setenv("FOODASSISTANT_HOSTNAME", "bandit")
    assert printing.device_hostname() == "bandit"


def test_list_queues_hides_self_import_end_to_end(monkeypatch):
    # Integration through the module-level list_queues with lpstat mocked:
    # the device's own re-imported queue disappears; a configured pick and a
    # remote device's queue survive.
    monkeypatch.setattr(printing.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setenv("FOODASSISTANT_HOSTNAME", "foodassistant")

    lpstat_p = (
        "printer Zebra_ZM400 is idle.  enabled since Tue 08 Jul 2026\n"
        "printer Zebra_ZM400_foodassistant is idle.  enabled since Tue 08 Jul 2026\n"
        "printer Brother is idle.  enabled since Tue 08 Jul 2026\n"
        "printer Brother_foodassistant is idle.  enabled since Tue 08 Jul 2026\n"
        "printer Laser_korolev is idle.  enabled since Tue 08 Jul 2026\n"
    )

    class FakeProc:
        def __init__(self, stdout):
            self.stdout = stdout.encode()
            self.stderr = b""
            self.returncode = 0

    def fake_run(self, args, *, input_bytes=None):
        if args[:2] == ["lpstat", "-p"]:
            return FakeProc(lpstat_p)
        if args[:2] == ["lpstat", "-d"]:
            return FakeProc("no system default destination\n")
        return FakeProc("")

    monkeypatch.setattr(printing.CupsBackend, "_run", fake_run)

    from app.config import settings
    monkeypatch.setattr(settings, "label_printer_queue",
                        "Brother_foodassistant", raising=False)
    monkeypatch.setattr(settings, "document_printer_queue", "", raising=False)
    monkeypatch.setattr(settings, "fleet_label_printer_queue", "", raising=False)
    monkeypatch.setattr(settings, "fleet_document_printer_queue", "", raising=False)

    names = [q["name"] for q in printing.list_queues()]
    # The self-import of Zebra_ZM400 is hidden; the Brother self-import is the
    # user's configured label queue so it stays; the remote-only queue stays.
    assert names == ["Zebra_ZM400", "Brother", "Brother_foodassistant",
                     "Laser_korolev"]


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


# --- satellite printer sharing (FoodAssistant-h1ms) ------------------------

def test_parse_supvan_bridge_page_extracts_name_and_queue():
    html = ('<ul><li><b>Supvan T50 Series T0148C260429G993</b> '
            '(<code>supvan_t50_series_t0148c260429g993</code>) &mdash; '
            '<code>ipp://localhost:8631/ipp/print/supvan_t50_series_t0148c260429g993</code>'
            '</li></ul>')
    assert printing.parse_supvan_bridge_page(html) == [
        ("Supvan T50 Series T0148C260429G993", "supvan_t50_series_t0148c260429g993")]
    # An empty bridge (no printer registered yet) yields nothing.
    assert printing.parse_supvan_bridge_page("<ul></ul>") == []


def test_satellite_printers_offers_lan_uri_for_online_remote(monkeypatch):
    import asyncio

    devs = [
        {"ip": "192.168.1.201", "online": True, "deployment_mode": "pi_remote",
         "label": "Bandit", "hostname": "bandit"},
        # offline satellite: skipped
        {"ip": "192.168.1.9", "online": False, "deployment_mode": "pi_remote",
         "label": "Off", "hostname": "off"},
        # a server (not a satellite): skipped
        {"ip": "192.168.1.170", "online": True, "deployment_mode": "server",
         "label": "Korolev", "hostname": "korolev"},
    ]
    monkeypatch.setattr("app.services.devices.list_devices", lambda: devs)

    class _Resp:
        status_code = 200
        text = ('<ul><li><b>Supvan T50 Series T0148</b> (<code>supvan_t50_series_t0148</code>)'
                ' &mdash; <code>ipp://localhost:8631/ipp/print/supvan_t50_series_t0148</code></li></ul>')

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            assert url == "http://192.168.1.201:8631/"  # only the online remote is probed
            return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    out = asyncio.run(printing.satellite_printers())
    assert len(out) == 1
    p = out[0]
    assert p["uri"] == "ipp://192.168.1.201:8631/ipp/print/supvan_t50_series_t0148"
    # The queue name is CUPS-safe (no spaces); the human text lives in info.
    assert p["name"] == "Supvan_Bandit_201"
    assert "Supvan T50 Series T0148 on Bandit" in p["info"]
    assert p["kind"] == "driverless" and p["driver"] == "everywhere"


# -- Label media discovery (FoodAssistant-u55y) ------------------------------

LPOPTIONS_DRIVER = """\
PageSize/Media Size: 40x30mm.Borderless *50x30mm.Borderless 40x60mm.Borderless Custom.WIDTHxHEIGHTmm
Resolution/Resolution: *203dpi 300dpi
"""

LPOPTIONS_PWG = """\
media/Media Size: om_40x30mm_40x30mm *om_50x30mm_50x30mm oe_shipping-label_101.6x152.4mm na_letter_8.5x11in
media-source/Media Source: *auto manual
"""

LPOPTIONS_NO_SIZES = """\
Resolution/Resolution: *203dpi 300dpi
Copies/Copies: 1
"""


def test_parse_media_token_driver_form_with_default_marker():
    got = printing._parse_media_token("*40x30mm.Borderless")
    assert got == {"w_mm": 40.0, "h_mm": 30.0,
                    "label": "40 x 30 mm (Borderless)", "is_default": True}


def test_parse_media_token_driver_form_no_descriptor():
    got = printing._parse_media_token("40x60mm")
    assert got == {"w_mm": 40.0, "h_mm": 60.0, "label": "40 x 60 mm",
                    "is_default": False}


def test_parse_media_token_pwg_form_no_descriptor():
    got = printing._parse_media_token("om_40x30mm_40x30mm")
    assert got["w_mm"] == 40.0 and got["h_mm"] == 30.0
    assert got["label"] == "40 x 30 mm"


def test_parse_media_token_pwg_form_with_descriptor_and_decimals():
    got = printing._parse_media_token("oe_shipping-label_101.6x152.4mm")
    assert got["w_mm"] == 101.6 and got["h_mm"] == 152.4
    assert got["label"] == "101.6 x 152.4 mm (Shipping Label)"


def test_parse_media_token_pwg_form_inches_converted_to_mm():
    got = printing._parse_media_token("na_letter_8.5x11in")
    # 8.5in * 25.4 = 215.9mm, 11in * 25.4 = 279.4mm
    assert got["w_mm"] == 215.9 and got["h_mm"] == 279.4
    assert got["label"] == "215.9 x 279.4 mm (Letter)"


def test_parse_media_token_ignores_custom_placeholder_and_junk():
    assert printing._parse_media_token("Custom.WIDTHxHEIGHTin") is None
    assert printing._parse_media_token("Letter") is None
    assert printing._parse_media_token("") is None
    assert printing._parse_media_token("*") is None


def test_parse_lpoptions_media_driver_queue():
    sizes = printing._parse_lpoptions_media(LPOPTIONS_DRIVER)
    assert [(s["w_mm"], s["h_mm"]) for s in sizes] == [
        (40.0, 30.0), (50.0, 30.0), (40.0, 60.0)]
    # Custom.WIDTHxHEIGHTmm carries no concrete size and is skipped; the
    # *-marked entry is flagged default.
    assert sizes[1]["is_default"] is True
    assert sizes[0]["is_default"] is False


def test_parse_lpoptions_media_pwg_queue():
    sizes = printing._parse_lpoptions_media(LPOPTIONS_PWG)
    assert [(s["w_mm"], s["h_mm"]) for s in sizes] == [
        (40.0, 30.0), (50.0, 30.0), (101.6, 152.4), (215.9, 279.4)]
    assert sizes[1]["is_default"] is True


def test_parse_lpoptions_media_ignores_unrelated_options():
    assert printing._parse_lpoptions_media(LPOPTIONS_NO_SIZES) == []
    assert printing._parse_lpoptions_media("") == []
    assert printing._parse_lpoptions_media(None) == []


def test_parse_lpoptions_media_dedupes_same_size():
    text = "PageSize/Media Size: 40x30mm.Borderless 40x30mm\n"
    sizes = printing._parse_lpoptions_media(text)
    assert len(sizes) == 1


def test_mm_to_inches_matches_stored_precision():
    assert printing.mm_to_inches(40) == round(40 / 25.4, 2)
    assert printing.mm_to_inches(50.8) == 2.0


def test_backend_label_media_runs_lpoptions_for_queue(monkeypatch):
    calls = []

    def fake_run(self, args, **kw):
        calls.append(args)
        class _Proc:
            returncode = 0
            stdout = LPOPTIONS_DRIVER.encode()
        return _Proc()

    monkeypatch.setattr(printing, "shutil", printing.shutil)
    monkeypatch.setattr(printing.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(printing.CupsBackend, "_run", fake_run)
    backend = printing.CupsBackend()
    sizes = backend.label_media("Zebra_ZD410")
    assert calls == [["lpoptions", "-p", "Zebra_ZD410", "-l"]]
    assert len(sizes) == 3


def test_backend_label_media_rejects_invalid_queue_without_shelling(monkeypatch):
    called = []
    monkeypatch.setattr(printing.CupsBackend, "_run",
                         lambda self, *a, **k: called.append(a) or None)
    backend = printing.CupsBackend()
    assert backend.label_media("bad name; rm -rf") == []
    assert called == []


def test_module_label_media_never_raises(monkeypatch):
    monkeypatch.setattr(printing._backend, "label_media",
                         lambda q: (_ for _ in ()).throw(RuntimeError("boom")))
    assert printing.label_media("Zebra_ZD410") == []


# -- Label shape setting (FoodAssistant-bprm) ---------------------------------
# The round/square shape is a designer-side layout aid plus a persisted setting;
# it does not change the printed raster, so these checks need no printer.

def test_label_shape_defaults_to_rectangle():
    from app.config import Settings, _SAVEABLE
    s = Settings()
    assert s.label_shape == "rectangle"
    assert "label_shape" in _SAVEABLE


def test_label_shape_is_persistable(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    settings.save({"label_shape": "round"})
    assert settings.label_shape == "round"
    settings.save({"label_shape": "rectangle"})
