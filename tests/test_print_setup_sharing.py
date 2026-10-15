"""Printer sharing and admin access in foodassistant-print-setup (0.21).

The helper decides PRINT_SHARING once from what the device already does,
never opens printer administration to the network, and follows the mode for
cupsd's listener, printer sharing and cups-browsed. These tests source the
script (FA_PRINT_SETUP_SOURCE=1 skips main()) against scratch files, with
cupsctl, systemctl, lpstat, lpadmin, ip and cupsd stubbed on PATH, so no
CUPS, apt or systemd is needed.

Run: python -m pytest tests/test_print_setup_sharing.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "image-build" / "foodassistant-print-setup"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

SHARE_BLOCK = (
    "# >>> foodassistant print sharing (FoodAssistant-gdwm) >>>\n"
    "Browsing On\n"
    "BrowseLocalProtocols dnssd\n"
    "ErrorPolicy retry-job\n"
    "# <<< foodassistant print sharing (FoodAssistant-gdwm) <<<\n"
)

STOCK_LOCAL = (
    "LogLevel warn\n"
    "Listen localhost:631\n"
    "Listen /run/cups/cups.sock\n"
    "Browsing No\n"
    "<Location />\n"
    "  Order allow,deny\n"
    "</Location>\n"
    "<Location /admin>\n"
    "  Order allow,deny\n"
    "</Location>\n"
)

_STUBS = {
    # cupsctl: record the call; with no arguments print the settings the test
    # asked for, the way the real tool does. With arguments, rewrite cupsd.conf
    # without its comment lines, as the real tool does when it saves a change
    # (that is what makes the marker blocks fragile).
    "cupsctl": (
        'echo "cupsctl $*" >> "$STUB_LOG"\n'
        'if [ "$#" = "0" ]; then printf "%b" "${STUB_CUPSCTL_OUT:-_share_printers=0\\n}"; exit 0; fi\n'
        'if [ -f "$CUPSD_CONF" ]; then\n'
        '  grep -vE "^[[:space:]]*#" "$CUPSD_CONF" > "$CUPSD_CONF.stub" || true\n'
        '  cat "$CUPSD_CONF.stub" > "$CUPSD_CONF"; rm -f "$CUPSD_CONF.stub"\n'
        'fi\n'
        "exit 0\n"
    ),
    "systemctl": (
        'echo "systemctl $*" >> "$STUB_LOG"\n'
        'case "$1" in\n'
        '  is-enabled) [ "${STUB_BROWSED_ENABLED:-0}" = "1" ] && exit 0; exit 1 ;;\n'
        '  is-active) [ "${STUB_BROWSED_ACTIVE:-0}" = "1" ] && exit 0; exit 3 ;;\n'
        "esac\n"
        "exit 0\n"
    ),
    "lpstat": "exit 0\n",
    "lpadmin": 'echo "lpadmin $*" >> "$STUB_LOG"\nexit 0\n',
    "ip": 'printf "%b" "${STUB_IP_OUT:-}"\nexit 0\n',
    # cupsd -t: the config "does not validate" unless the test says it does.
    "cupsd": '[ "${STUB_CUPSD_VALID:-0}" = "1" ] && exit 0\nexit 1\n',
}


class Rig:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.stack = tmp_path / "stack"
        self.stack.mkdir()
        self.env_file = self.stack / ".env"
        self.compose = self.stack / "docker-compose.yml"
        # The shipped appliance layout: the app shares the host network.
        self.compose.write_text("services:\n  app:\n    network_mode: host\n")
        self.cupsd_conf = tmp_path / "cupsd.conf"
        self.browsed_conf = tmp_path / "cups-browsed.conf"
        self.log = tmp_path / "stub.log"
        self.log.write_text("")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, body in _STUBS.items():
            path = self.bin / name
            path.write_text("#!/usr/bin/env bash\n" + body)
            path.chmod(0o755)
        self.extra: dict = {}

    def run(self, commands: str, **extra) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.root),
            "TMPDIR": str(self.root),
            "FA_PRINT_SETUP_SOURCE": "1",
            "SKIP_APT": "1",
            "INSTALL_DIR": str(self.stack),
            "ENV_FILE": str(self.env_file),
            "COMPOSE_FILE": str(self.compose),
            "CUPSD_CONF": str(self.cupsd_conf),
            "CUPS_BROWSED_CONF": str(self.browsed_conf),
            "CUPSD_BIN": str(self.bin / "cupsd"),
            "STUB_LOG": str(self.log),
        }
        env.update(self.extra)
        env.update({k: str(v) for k, v in extra.items()})
        proc = subprocess.run(
            ["bash", "-c", f'source "{SCRIPT}"; {commands}'],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc

    def calls(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line]

    def env_text(self) -> str:
        return self.env_file.read_text() if self.env_file.exists() else ""


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def _mode(rig: Rig, **extra) -> str:
    proc = rig.run('resolve_print_sharing_mode >/dev/null; printf "MODE=%s\\n" "$(print_sharing_mode)"',
                   **extra)
    return proc.stdout.strip().splitlines()[-1].split("=", 1)[1]


def _active_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _location_blocks(text: str) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.lower().startswith("<location"):
            current = line[len("<Location"):].rstrip(">").strip()
            blocks[current] = []
        elif line.lower().startswith("</location>"):
            current = None
        elif current is not None:
            blocks[current].append(line)
    return blocks


# -- Deciding PRINT_SHARING ---------------------------------------------------


def test_script_is_valid_bash():
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("conf", [
    STOCK_LOCAL + SHARE_BLOCK,
    STOCK_LOCAL.replace("Listen localhost:631", "Listen 0.0.0.0:631"),
    STOCK_LOCAL.replace("Listen localhost:631", "Listen *:631"),
    STOCK_LOCAL.replace("Listen localhost:631", "Port 631"),
], ids=["share-block", "listen-any", "listen-star", "port"])
def test_a_device_that_shares_in_its_config_is_marked_legacy(rig, conf):
    rig.cupsd_conf.write_text(conf)
    assert _mode(rig) == "legacy"
    assert "PRINT_SHARING=legacy" in rig.env_text()


def test_cupsctl_reporting_sharing_marks_legacy(rig):
    rig.cupsd_conf.write_text(STOCK_LOCAL)
    assert _mode(rig, STUB_CUPSCTL_OUT="_debug_logging=0\\n_share_printers=1\\n") == "legacy"
    assert "PRINT_SHARING=legacy" in rig.env_text()


@pytest.mark.parametrize("flag", ["STUB_BROWSED_ENABLED", "STUB_BROWSED_ACTIVE"])
def test_cups_browsed_running_or_enabled_marks_legacy(rig, flag):
    rig.cupsd_conf.write_text(STOCK_LOCAL)
    assert _mode(rig, **{flag: "1"}) == "legacy"


def test_a_device_that_never_shared_is_marked_off(rig):
    rig.cupsd_conf.write_text(STOCK_LOCAL)
    assert _mode(rig) == "off"
    assert rig.env_text().count("PRINT_SHARING=off") == 1


def test_a_fresh_device_with_no_cups_yet_is_marked_off(rig):
    assert _mode(rig) == "off"
    assert "PRINT_SHARING=off" in rig.env_text()


def test_off_on_a_bridge_networked_stack_is_written_as_legacy(rig):
    # The app container reaches cupsd over docker0 here, so cupsd must keep
    # listening beyond loopback or the app could not print at all.
    rig.compose.write_text("services:\n  app:\n    image: example\n")
    rig.cupsd_conf.write_text(STOCK_LOCAL)
    proc_mode = _mode(rig, STUB_IP_OUT="    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\n")
    assert proc_mode == "legacy"
    assert "PRINT_SHARING=legacy" in rig.env_text()


@pytest.mark.parametrize("stored", ["off", "legacy"])
def test_the_mode_is_never_rewritten_once_present(rig, stored):
    rig.env_file.write_text(f"FOO=1\nPRINT_SHARING={stored}\n")
    # Signals that would decide the other way are ignored.
    rig.cupsd_conf.write_text(STOCK_LOCAL + SHARE_BLOCK if stored == "off" else STOCK_LOCAL)
    before = rig.env_text()
    assert _mode(rig, STUB_BROWSED_ACTIVE="1" if stored == "off" else "0") == stored
    assert rig.env_text() == before


def test_serve_is_kept_and_behaves_like_legacy_for_now(rig):
    rig.env_file.write_text("PRINT_SHARING=serve\n")
    assert _mode(rig) == "legacy"
    assert rig.env_text() == "PRINT_SHARING=serve\n"


# -- Admin access ----------------------------------------------------------------


ADMIN_OPEN = (
    "Listen 0.0.0.0:631\n"
    "<Location />\n"
    "  Order allow,deny\n"
    "  Allow @LOCAL\n"
    "</Location>\n"
    "<Location /admin>\n"
    "  Order allow,deny\n"
    "  Allow @LOCAL\n"
    "</Location>\n"
    "<Location /admin/conf>\n"
    "  AuthType Default\n"
    "  Require user @SYSTEM\n"
    "  Order allow,deny\n"
    "\tAllow @LOCAL\n"
    "</Location>\n"
    "<Location /admin/log>\n"
    "  Order allow,deny\n"
    "    Allow @LOCAL\n"
    "  allow from @local\n"
    "  Allow 192.168.1.0/24\n"
    "</Location>\n"
    "<Location /printers>\n"
    "  Allow @LOCAL\n"
    "</Location>\n"
)


def test_harden_removes_local_network_admin_only_inside_admin_blocks(rig):
    rig.cupsd_conf.write_text(ADMIN_OPEN)
    rig.run("harden_admin_access")
    blocks = _location_blocks(rig.cupsd_conf.read_text())
    for loc in ("/admin", "/admin/conf", "/admin/log"):
        assert not any(line.lower().replace("from ", "") == "allow @local"
                       for line in blocks[loc]), loc
    # Printing and other locations are untouched, and so is anything that is
    # not exactly "Allow @LOCAL".
    assert "Allow @LOCAL" in blocks["/"]
    assert "Allow @LOCAL" in blocks["/printers"]
    assert "Allow 192.168.1.0/24" in blocks["/admin/log"]
    assert "Require user @SYSTEM" in blocks["/admin/conf"]


def test_harden_is_idempotent(rig):
    rig.cupsd_conf.write_text(ADMIN_OPEN)
    rig.run("harden_admin_access")
    once = rig.cupsd_conf.read_text()
    rig.run("harden_admin_access")
    assert rig.cupsd_conf.read_text() == once


def test_harden_keeps_the_file_mode(rig):
    rig.cupsd_conf.write_text(ADMIN_OPEN)
    rig.cupsd_conf.chmod(0o640)
    rig.run("harden_admin_access")
    assert (rig.cupsd_conf.stat().st_mode & 0o777) == 0o640


# -- Following the mode -------------------------------------------------------------


def test_off_mode_listens_on_this_device_only_and_stops_sharing(rig):
    rig.env_file.write_text("PRINT_SHARING=off\n")
    rig.cupsd_conf.write_text(
        STOCK_LOCAL.replace("Listen localhost:631", "Listen 0.0.0.0:631") + SHARE_BLOCK)
    rig.browsed_conf.write_text("BrowseRemoteProtocols dnssd cups\n")
    rig.run("resolve_print_sharing_mode; configure_cups_sharing; install_cups_browsed")
    text = rig.cupsd_conf.read_text()
    active = _active_lines(text)
    assert "Listen 0.0.0.0:631" not in active
    assert active.count("Listen localhost:631") == 1
    assert "Listen /run/cups/cups.sock" in active
    assert "Browsing Off" in active and "DefaultShared No" in active
    assert "ErrorPolicy retry-job" in active
    assert "foodassistant print sharing" not in text
    calls = rig.calls()
    assert "cupsctl --no-share-printers" in calls
    assert "cupsctl --share-printers" not in calls
    assert "systemctl disable --now cups-browsed" in calls
    assert not any(c.startswith("systemctl enable") for c in calls)


def test_off_mode_never_doubles_the_loopback_listener(rig):
    rig.env_file.write_text("PRINT_SHARING=off\n")
    rig.cupsd_conf.write_text(STOCK_LOCAL + "Listen 0.0.0.0:631\nPort 631\n")
    rig.run("resolve_print_sharing_mode; configure_cups_sharing")
    active = _active_lines(rig.cupsd_conf.read_text())
    assert active.count("Listen localhost:631") == 1
    assert not any(a.startswith("Port ") or "0.0.0.0" in a for a in active)


def test_off_mode_rerun_converges(rig):
    rig.env_file.write_text("PRINT_SHARING=off\n")
    rig.cupsd_conf.write_text(STOCK_LOCAL.replace("Listen localhost:631", "Listen *:631"))
    rig.run("resolve_print_sharing_mode; configure_cups_sharing")
    once = rig.cupsd_conf.read_text()
    rig.run("resolve_print_sharing_mode; configure_cups_sharing")
    assert rig.cupsd_conf.read_text() == once
    # An off device's own block never reads as sharing on a later decision.
    rig.env_file.write_text("")
    assert _mode(rig) == "off"


@pytest.mark.parametrize("mode", ["off", "legacy"])
def test_rerun_converges_although_cupsctl_drops_comments(rig, mode):
    # cupsctl saves cupsd.conf without its comments, so a block written before
    # it runs would lose its markers and pile up again on every update.
    rig.env_file.write_text(f"PRINT_SHARING={mode}\n")
    rig.cupsd_conf.write_text(STOCK_LOCAL)
    rig.run("resolve_print_sharing_mode; configure_cups_sharing")
    once = rig.cupsd_conf.read_text()
    begin = ("foodassistant print stack, this device only" if mode == "off"
             else "foodassistant print sharing")
    assert once.count(begin) == 2  # the begin and end markers survive
    for _ in range(2):
        rig.run("resolve_print_sharing_mode; configure_cups_sharing")
    assert rig.cupsd_conf.read_text() == once
    assert _active_lines(once).count("ErrorPolicy retry-job") == 1


def test_legacy_mode_keeps_sharing_as_before(rig):
    rig.env_file.write_text("PRINT_SHARING=legacy\n")
    rig.cupsd_conf.write_text(STOCK_LOCAL)
    rig.run("resolve_print_sharing_mode; configure_cups_sharing; install_cups_browsed")
    active = _active_lines(rig.cupsd_conf.read_text())
    assert "Listen 0.0.0.0:631" in active
    assert "Browsing On" in active
    calls = rig.calls()
    assert "cupsctl --share-printers" in calls
    assert "systemctl enable --now cups-browsed" in calls


@pytest.mark.parametrize("mode", ["off", "legacy"])
def test_heal_writes_a_config_with_no_network_admin(rig, mode):
    rig.env_file.write_text(f"PRINT_SHARING={mode}\n")
    rig.cupsd_conf.write_text("this is not a valid config\n")
    rig.run("resolve_print_sharing_mode; heal_cupsd_conf")
    text = rig.cupsd_conf.read_text()
    blocks = _location_blocks(text)
    for loc in ("/admin", "/admin/conf"):
        assert "Allow @LOCAL" not in blocks[loc]
    active = _active_lines(text)
    assert "Listen /run/cups/cups.sock" in active
    calls = rig.calls()
    if mode == "off":
        assert "Allow @LOCAL" not in text
        assert "Listen localhost:631" in active and "Listen 0.0.0.0:631" not in active
        assert "Browsing Off" in active and "DefaultShared No" in active
        assert "cupsctl --share-printers" not in calls
    else:
        assert "Allow @LOCAL" in blocks["/"]
        assert "Listen 0.0.0.0:631" in active
        assert "cupsctl --share-printers" in calls
    # The broken file is kept for review.
    assert (rig.root / "cupsd.conf.foodassistant-broken").exists()


def test_heal_leaves_a_valid_config_alone(rig):
    rig.env_file.write_text("PRINT_SHARING=off\n")
    rig.cupsd_conf.write_text(STOCK_LOCAL)
    rig.run("resolve_print_sharing_mode; heal_cupsd_conf", STUB_CUPSD_VALID="1")
    assert rig.cupsd_conf.read_text() == STOCK_LOCAL
    assert not any(c.startswith("cupsctl") for c in rig.calls())


# -- cups-browsed ---------------------------------------------------------------------


BROWSED = (
    "# Sample cups-browsed.conf\n"
    "# BrowseRemoteProtocols dnssd cups\n"
    "BrowseRemoteProtocols dnssd cups\n"
    "BrowseLocalProtocols CUPS\n"
    "  BrowseProtocols cups dnssd\n"
    "BrowseRemoteProtocols cups\n"
    "BrowsePoll printserver.example\n"
)


@pytest.mark.parametrize("mode", ["off", "legacy"])
def test_cups_browsed_finds_printers_over_dnssd_only(rig, mode):
    rig.env_file.write_text(f"PRINT_SHARING={mode}\n")
    rig.browsed_conf.write_text(BROWSED)
    rig.run("resolve_print_sharing_mode; install_cups_browsed")
    active = _active_lines(rig.browsed_conf.read_text())
    remote = [a for a in active if a.lower().startswith("browseremoteprotocols")]
    local = [a for a in active if a.lower().startswith("browselocalprotocols")]
    assert remote == ["BrowseRemoteProtocols dnssd"]
    assert local == ["BrowseLocalProtocols none"]
    assert not any(a.lower().startswith("browseprotocols") for a in active)
    assert not any("cups" in r.lower().split()[1:] for r in remote)
    # Other settings are kept.
    assert "BrowsePoll printserver.example" in active
    assert "systemctl try-restart cups-browsed" in rig.calls()


def test_cups_browsed_conf_rerun_changes_nothing(rig):
    rig.env_file.write_text("PRINT_SHARING=legacy\n")
    rig.browsed_conf.write_text(BROWSED)
    rig.run("resolve_print_sharing_mode; configure_cups_browsed_protocols")
    once = rig.browsed_conf.read_text()
    rig.log.write_text("")
    rig.run("resolve_print_sharing_mode; configure_cups_browsed_protocols")
    assert rig.browsed_conf.read_text() == once
    assert "systemctl try-restart cups-browsed" not in rig.calls()


def test_missing_cups_browsed_conf_is_not_created(rig):
    rig.run("configure_cups_browsed_protocols")
    assert not rig.browsed_conf.exists()


def test_main_decides_the_mode_first_and_hardens_before_healing():
    src = SCRIPT.read_text()
    body = src[src.index("main() {"):]
    order = ["resolve_print_sharing_mode", "configure_cupsd", "harden_admin_access",
             "configure_cups_sharing", "install_cups_browsed", "heal_cupsd_conf"]
    positions = [body.index(name) for name in order]
    assert positions == sorted(positions)
