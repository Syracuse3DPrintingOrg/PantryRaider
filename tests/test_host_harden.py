"""foodassistant-host-harden: the drop-ins and unit edits that give the deck,
the reader and the kitchen screen their local credentials (I-5), and the
kiosk-url-local fix for a screen that shows the app through another address.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "image-build" / "foodassistant-host-harden"


def _load():
    loader = importlib.machinery.SourceFileLoader("foodassistant_host_harden", str(HELPER))
    spec = importlib.util.spec_from_loader("foodassistant_host_harden", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


hh = _load()

LOCAL = "http://localhost:9284/ui/?kiosk=1"
EXT = "/opt/foodassistant/kiosk-exit-extension"

# The kiosk unit firstboot.sh writes (0.20.x), rendered with example values.
FIRSTBOOT_KIOSK_UNIT = """[Unit]
Description=FoodAssistant Chromium kiosk
After=foodassistant.target systemd-user-sessions.service getty@tty1.service network-online.target seatd.service
Wants=network-online.target seatd.service
Conflicts=getty@tty1.service
StartLimitIntervalSec=0

[Service]
EnvironmentFile=-/etc/foodassistant/kiosk-env
Type=simple
TimeoutStartSec=900
User=kiosk
PAMName=login
TTYPath=/dev/tty1
Environment=XDG_RUNTIME_DIR=/run/user/1001
Environment=LIBSEAT_BACKEND=seatd
ExecStartPre=-/bin/sh -c 'command -v curl >/dev/null 2>&1 || exit 0; for i in $$(seq 1 420); do if [ -e "/var/lib/foodassistant/firstboot.done" ] || [ "$$(systemctl is-active foodassistant-firstboot.service 2>/dev/null)" != "activating" ]; then curl -sf -o /dev/null --max-time 2 "http://localhost:9284/health" && exit 0; fi; sleep 2; done; exit 0'
# --default-background-color: Chromium paints white while a page loads.
ExecStart=/usr/bin/cage -- /usr/bin/chromium --kiosk --noerrdialogs \\
  --disable-infobars --no-first-run --ozone-platform=wayland \\
  --touch-events=enabled --use-gl=angle --use-angle=gles-egl \\
  --default-background-color=212529 --load-extension=EXT --disable-extensions-except=EXT \\
  --remote-debugging-port=9222 --disable-restore-session-state KIOSK_URL
ExecStartPost=-/usr/local/bin/foodassistant-apply-rotation
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
""".replace("EXT", EXT)

# The boot drop-in a 0.20.x updater wrote: its wait fetched the kiosk page.
OLD_BOOT_DROPIN = """[Unit]
Wants=seatd.service
After=seatd.service
StartLimitIntervalSec=0

[Service]
TimeoutStartSec=240
ExecStartPre=-/bin/sh -c 'command -v curl >/dev/null 2>&1 || exit 0; for i in $$(seq 1 40); do curl -sf -o /dev/null --max-time 2 "KIOSK_URL" && exit 0; sleep 2; done; exit 0'
"""

REMOTE_UNIT = """[Unit]
Description=FoodAssistant Pi Remote UI

[Service]
Type=simple
User=pi
EnvironmentFile=/opt/foodassistant/remote.env
ExecStart=/opt/foodassistant/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 80 --timeout-graceful-shutdown 5 --no-access-log
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""

DECK_UNIT = """[Service]
User=pi
Environment=FOODASSISTANT_BASE_URL=http://localhost:9284
ExecStart=/opt/foodassistant/venv/bin/python -m foodassistant_streamdeck --config /etc/foodassistant/streamdeck.toml
"""

STUB_SYSTEMCTL = """#!{python}
import os, sys
with open(os.environ["STUB_SYSTEMCTL_LOG"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
"""


def kiosk_unit(url=LOCAL):
    # firstboot points the unit's own boot wait at the kiosk page's origin.
    wait = url.split("/ui/")[0] + "/health"
    return FIRSTBOOT_KIOSK_UNIT.replace("KIOSK_URL", url).replace(
        '"http://localhost:9284/health"', f'"{wait}"')


@pytest.fixture
def host(tmp_path, monkeypatch):
    units = tmp_path / "units"
    units.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("foodassistant-agent-key", "foodassistant-kiosk-ticket",
                 "foodassistant-kiosk-launch"):
        (bindir / name).write_text("#!/bin/sh\nexit 0\n")
        (bindir / name).chmod(0o755)
    install = tmp_path / "opt"
    (install / "data").mkdir(parents=True)
    stub = tmp_path / "systemctl"
    stub.write_text(STUB_SYSTEMCTL.format(python=sys.executable))
    stub.chmod(0o755)
    calls = tmp_path / "systemctl.log"
    monkeypatch.setenv("UNIT_DIR", str(units))
    monkeypatch.setenv("BIN_DIR", str(bindir))
    monkeypatch.setenv("INSTALL_DIR", str(install))
    monkeypatch.setenv("SYSTEMCTL", str(stub))
    monkeypatch.setenv("STUB_SYSTEMCTL_LOG", str(calls))
    monkeypatch.setenv("FA_PYTHON3", sys.executable)
    monkeypatch.setattr(hh, "device_hostname", lambda: "kitchen-pi")

    class Host:
        unit_dir = units
        bin_dir = bindir
        install_dir = install

        def unit(self, name, text):
            p = units / name
            p.write_text(text)
            return p

        def dropin(self, unit, name):
            return units / f"{unit}.d" / name

        def systemctl(self):
            return calls.read_text().splitlines() if calls.exists() else []

    return Host()


def run(*args, capsys):
    assert hh.main(list(args)) == 0
    out = capsys.readouterr().out.strip().splitlines()
    return json.loads(out[-1])


# --- drop-ins ---------------------------------------------------------------

def test_agent_and_kiosk_dropins_are_exact_and_idempotent(host, capsys):
    host.unit("foodassistant-streamdeck.service", DECK_UNIT)
    host.unit("foodassistant-gadgets.service", DECK_UNIT)
    host.unit("foodassistant-kiosk.service", kiosk_unit())
    summary = run("local-credentials", capsys=capsys)
    assert summary["changed"][:3] == ["streamdeck-key", "gadgets-key", "kiosk-launch-dropin"]
    assert "kiosk-launch" in summary["changed"]
    for agent in ("streamdeck", "gadgets"):
        assert host.dropin(f"foodassistant-{agent}.service", "20-foodassistant-key.conf").read_text() == (
            "[Service]\n"
            f"ExecStartPre=+/usr/local/bin/foodassistant-agent-key {agent}\n"
            f"EnvironmentFile=-/run/foodassistant-agents/{agent}.env\n")
    assert host.dropin("foodassistant-kiosk.service", "20-foodassistant-launch.conf").read_text() == (
        "[Service]\n"
        "RuntimeDirectory=foodassistant-kiosk\n"
        "RuntimeDirectoryMode=0711\n"
        "ExecStartPre=+/usr/local/bin/foodassistant-kiosk-ticket\n")
    assert host.systemctl() == ["daemon-reload"]
    again = run("local-credentials", capsys=capsys)
    assert again == {"changed": [], "skipped": []}
    assert host.systemctl() == ["daemon-reload"]  # nothing changed, no reload


def test_no_dropins_for_units_that_do_not_exist(host, capsys):
    summary = run("local-credentials", capsys=capsys)
    assert summary == {"changed": [], "skipped": []}
    assert list(host.unit_dir.iterdir()) == []
    assert host.systemctl() == []


def test_dry_run_writes_nothing(host, capsys):
    host.unit("foodassistant-streamdeck.service", DECK_UNIT)
    k = host.unit("foodassistant-kiosk.service", kiosk_unit())
    host.unit("foodassistant-remote.service", REMOTE_UNIT)
    summary = run("local-credentials", "--dry-run", capsys=capsys)
    assert {"streamdeck-key", "kiosk-launch-dropin", "kiosk-launch",
            "remote-proxy-headers"} <= set(summary["changed"])
    assert sorted(p.name for p in host.unit_dir.iterdir()) == sorted(
        ["foodassistant-streamdeck.service", "foodassistant-kiosk.service",
         "foodassistant-remote.service"])
    assert k.read_text() == kiosk_unit()
    assert host.systemctl() == []


def test_agent_dropins_wait_for_the_helper(host, capsys):
    (host.bin_dir / "foodassistant-agent-key").unlink()
    host.unit("foodassistant-streamdeck.service", DECK_UNIT)
    summary = run("local-credentials", capsys=capsys)
    assert summary["changed"] == []
    assert not host.dropin("foodassistant-streamdeck.service", "20-foodassistant-key.conf").exists()


# --- the kiosk ExecStart patch ----------------------------------------------

def test_execstart_patch_on_the_firstboot_unit(host, capsys):
    k = host.unit("foodassistant-kiosk.service", kiosk_unit())
    run("local-credentials", capsys=capsys)
    text = k.read_text()
    assert "ExecStart=/usr/bin/cage -- /usr/local/bin/foodassistant-kiosk-launch /usr/bin/chromium --kiosk" in text
    assert text.count("foodassistant-kiosk-launch") == 1
    assert "--remote-debugging-port" not in text
    assert hh.kiosk_url_from_unit(text) == LOCAL
    assert text.rstrip().endswith("WantedBy=multi-user.target")
    assert "--disable-restore-session-state " + LOCAL in text
    assert (host.unit_dir / "foodassistant-kiosk.service.bak").read_text() == kiosk_unit()
    # Only the two edits: the launcher in, the port out.
    assert text.replace("/usr/local/bin/foodassistant-kiosk-launch ", "") == \
        kiosk_unit().replace(" --remote-debugging-port=9222", "")
    assert run("local-credentials", capsys=capsys) == {"changed": [], "skipped": []}
    assert k.read_text() == text


def test_unit_without_separator_is_restored_from_bak(host, capsys):
    odd = kiosk_unit().replace("/usr/bin/cage -- ", "/usr/bin/cage ")
    k = host.unit("foodassistant-kiosk.service", odd)
    summary = run("local-credentials", capsys=capsys)
    assert "kiosk-launch (unit not in the expected form)" in summary["skipped"]
    assert k.read_text() == odd


@pytest.mark.parametrize("execstart", [
    # xinit's ' -- ' separates the X server, not a cage client.
    "ExecStart=/usr/bin/xinit /usr/bin/chromium --kiosk " + LOCAL + " -- /usr/bin/X :0",
    # cage running something other than Chromium.
    "ExecStart=/usr/bin/cage -- /usr/bin/firefox --kiosk " + LOCAL,
])
def test_only_cage_running_chromium_is_patched(host, capsys, execstart):
    odd = FIRSTBOOT_KIOSK_UNIT.replace(
        FIRSTBOOT_KIOSK_UNIT[FIRSTBOOT_KIOSK_UNIT.index("ExecStart=/usr/bin/cage"):
                             FIRSTBOOT_KIOSK_UNIT.index("\nExecStartPost=")],
        execstart)
    k = host.unit("foodassistant-kiosk.service", odd)
    summary = run("local-credentials", capsys=capsys)
    assert "kiosk-launch (unit not in the expected form)" in summary["skipped"]
    assert k.read_text() == odd


def test_failed_check_after_the_write_restores_the_bak(host, capsys, monkeypatch):
    k = host.unit("foodassistant-kiosk.service", kiosk_unit())
    real_write = hh._write

    def bad_write(path, text, mode=None):
        if path.endswith("foodassistant-kiosk.service"):
            text = text.replace("foodassistant-kiosk-launch", "foodassistant-kiosk-lunch")
        real_write(path, text, mode)
    monkeypatch.setattr(hh, "_write", bad_write)
    summary = run("local-credentials", capsys=capsys)
    assert "kiosk-launch (unit not in the expected form)" in summary["skipped"]
    assert k.read_text() == kiosk_unit()


def test_no_patch_without_the_launcher_or_python(host, capsys, monkeypatch):
    k = host.unit("foodassistant-kiosk.service", kiosk_unit())
    (host.bin_dir / "foodassistant-kiosk-launch").unlink()
    summary = run("local-credentials", capsys=capsys)
    assert summary["changed"] == ["kiosk-launch-dropin"]
    assert k.read_text() == kiosk_unit()
    (host.bin_dir / "foodassistant-kiosk-launch").write_text("#!/bin/sh\n")
    (host.bin_dir / "foodassistant-kiosk-launch").chmod(0o755)
    monkeypatch.setenv("FA_PYTHON3", str(host.bin_dir / "no-python3"))
    assert run("local-credentials", capsys=capsys)["changed"] == []
    assert k.read_text() == kiosk_unit()


def test_bare_host_name_is_local_but_dot_local_is_not(host, capsys, monkeypatch):
    k = host.unit("foodassistant-kiosk.service", kiosk_unit("http://kitchen-pi:9284/ui/?kiosk=1"))
    summary = run("local-credentials", capsys=capsys)
    assert {"kiosk-launch-dropin", "kiosk-launch"} <= set(summary["changed"])
    assert "foodassistant-kiosk-launch" in k.read_text()

    other = host.unit_dir / "other"
    other.mkdir()
    monkeypatch.setenv("UNIT_DIR", str(other))
    k2 = other / "foodassistant-kiosk.service"
    k2.write_text(kiosk_unit("http://kitchen-pi.local:9284/ui/?kiosk=1"))
    summary = run("local-credentials", capsys=capsys)
    assert summary == {"changed": [], "skipped": ["kiosk-url-not-local"]}
    assert k2.read_text() == kiosk_unit("http://kitchen-pi.local:9284/ui/?kiosk=1")
    assert not (other / "foodassistant-kiosk.service.d").exists()


def test_proxied_kiosk_url_is_left_exactly_as_it_is(host, capsys):
    url = "https://pantry.example.com/ui/?kiosk=1"
    k = host.unit("foodassistant-kiosk.service", kiosk_unit(url))
    boot = host.dropin("foodassistant-kiosk.service", "10-foodassistant-boot.conf")
    boot.parent.mkdir()
    boot.write_text(OLD_BOOT_DROPIN.replace("KIOSK_URL", url))
    summary = run("local-credentials", capsys=capsys)
    assert summary == {"changed": [], "skipped": ["kiosk-url-not-local"]}
    assert k.read_text() == kiosk_unit(url)
    assert not host.dropin("foodassistant-kiosk.service", "20-foodassistant-launch.conf").exists()
    assert not (host.unit_dir / "foodassistant-kiosk.service.bak").exists()
    assert boot.read_text() == OLD_BOOT_DROPIN.replace("KIOSK_URL", url)
    assert host.systemctl() == []


def test_boot_dropin_curl_moves_to_health(host, capsys):
    host.unit("foodassistant-kiosk.service", kiosk_unit())
    boot = host.dropin("foodassistant-kiosk.service", "10-foodassistant-boot.conf")
    boot.parent.mkdir()
    boot.write_text(OLD_BOOT_DROPIN.replace("KIOSK_URL", LOCAL))
    summary = run("local-credentials", capsys=capsys)
    assert "kiosk-boot-wait" in summary["changed"]
    assert boot.read_text() == OLD_BOOT_DROPIN.replace("KIOSK_URL", "http://localhost:9284/health")
    assert "kiosk-boot-wait" not in run("local-credentials", capsys=capsys)["changed"]


# --- the Bandit's web service -------------------------------------------------

def test_no_proxy_headers_added_once(host, capsys):
    r = host.unit("foodassistant-remote.service", REMOTE_UNIT)
    summary = run("local-credentials", capsys=capsys)
    assert summary["changed"] == ["remote-proxy-headers"]
    assert r.read_text() == REMOTE_UNIT.replace("--no-access-log", "--no-access-log --no-proxy-headers")
    assert (host.unit_dir / "foodassistant-remote.service.bak").read_text() == REMOTE_UNIT
    assert run("local-credentials", capsys=capsys)["changed"] == []
    assert r.read_text().count("--no-proxy-headers") == 1


def _remote_env(host, settings):
    env = host.install_dir / "remote.env"
    env.write_text("DEPLOYMENT_MODE=pi_remote\nAUTH_REQUIRED=false\nTZ=UTC\n")
    env.chmod(0o600)
    if settings is not None:
        (host.install_dir / "data" / "settings.json").write_text(settings)
    return env


def test_auth_pin_goes_only_with_a_password(host, capsys):
    env = _remote_env(host, json.dumps({"auth_password": "hashed-value"}))
    summary = run("local-credentials", capsys=capsys)
    assert summary["changed"] == ["remote-auth-pin"]
    assert env.read_text() == "DEPLOYMENT_MODE=pi_remote\nTZ=UTC\n"
    assert oct(env.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("settings", [None, "{not json", json.dumps({"auth_password": ""}),
                                      json.dumps({"auth_password": None})])
def test_auth_pin_stays_without_a_password(host, capsys, settings):
    env = _remote_env(host, settings)
    summary = run("local-credentials", capsys=capsys)
    assert summary["changed"] == []
    assert "AUTH_REQUIRED=false" in env.read_text()
    assert host.systemctl() == []


# --- kiosk-url-local ----------------------------------------------------------

def _proxied(host, url="https://pantry.example.com/ui/?kiosk=1", bandit=False):
    k = host.unit("foodassistant-kiosk.service", kiosk_unit(url))
    if bandit:
        host.unit("foodassistant-remote.service", REMOTE_UNIT)
    ext = host.install_dir / "kiosk-exit-extension"
    ext.mkdir()
    (ext / "origin.js").write_text(f'const PR_KIOSK_HOME_URL = "{url}";\n')
    boot = host.dropin("foodassistant-kiosk.service", "10-foodassistant-boot.conf")
    boot.parent.mkdir()
    boot.write_text(OLD_BOOT_DROPIN.replace("KIOSK_URL", url))
    return k, ext / "origin.js", boot


@pytest.mark.parametrize("bandit,new_url", [(False, LOCAL), (True, "http://localhost/ui/?kiosk=1")])
def test_kiosk_url_local_points_the_screen_home(host, capsys, bandit, new_url):
    url = "https://pantry.example.com/ui/?kiosk=1"
    k, origin_js, boot = _proxied(host, url, bandit)
    summary = run("kiosk-url-local", capsys=capsys)
    assert summary["changed"][0] == "kiosk-url"
    assert {"kiosk-launch-dropin", "kiosk-launch", "kiosk-boot-wait"} <= set(summary["changed"])
    text = k.read_text()
    assert hh.kiosk_url_from_unit(text) == new_url
    # Every other byte as before, apart from the launcher patch that follows.
    assert text.replace("/usr/local/bin/foodassistant-kiosk-launch ", "") == \
        kiosk_unit(new_url).replace(" --remote-debugging-port=9222", "")
    assert (host.unit_dir / "foodassistant-kiosk.service.bak").exists()
    assert origin_js.read_text() == f'const PR_KIOSK_HOME_URL = "{new_url}";\n'
    origin = new_url.split("/ui/")[0]
    assert boot.read_text() == OLD_BOOT_DROPIN.replace("KIOSK_URL", origin + "/health")
    assert host.dropin("foodassistant-kiosk.service", "20-foodassistant-launch.conf").exists()
    assert host.systemctl()[-2:] == ["daemon-reload", "restart foodassistant-kiosk.service"]
    # The unit's own boot wait followed the page: nothing waits on the old address.
    assert "pantry.example.com" not in text
    assert f'"{origin}/health"' in text


def test_kiosk_url_local_leaves_an_unrelated_wait_alone(host, capsys):
    url = "https://pantry.example.com/ui/?kiosk=1"
    custom = kiosk_unit(url).replace('"https://pantry.example.com/health"',
                                     '"https://status.example.com/health"')
    k = host.unit("foodassistant-kiosk.service", custom)
    assert run("kiosk-url-local", capsys=capsys)["changed"][0] == "kiosk-url"
    text = k.read_text()
    assert '"https://status.example.com/health"' in text
    assert hh.kiosk_url_from_unit(text) == LOCAL


def test_kiosk_url_local_leaves_a_local_unit_alone(host, capsys):
    k = host.unit("foodassistant-kiosk.service", kiosk_unit())
    assert run("kiosk-url-local", capsys=capsys) == {"changed": [], "skipped": []}
    assert k.read_text() == kiosk_unit()
    assert host.systemctl() == []
    k.write_text(kiosk_unit("http://kitchen-pi:9284/ui/?kiosk=1"))
    assert run("kiosk-url-local", capsys=capsys) == {"changed": [], "skipped": []}


def test_kiosk_url_local_without_a_kiosk_unit(host, capsys):
    assert run("kiosk-url-local", capsys=capsys) == {
        "changed": [], "skipped": ["kiosk-url (no kiosk unit)"]}


def test_kiosk_url_local_restores_a_unit_not_in_the_expected_form(host, capsys, monkeypatch):
    url = "https://pantry.example.com/ui/?kiosk=1"
    k, origin_js, boot = _proxied(host, url)
    real_write = hh._write

    def bad_write(path, text, mode=None):
        if path.endswith("foodassistant-kiosk.service"):
            text = text.replace(LOCAL, LOCAL + " http://elsewhere/")
        real_write(path, text, mode)
    monkeypatch.setattr(hh, "_write", bad_write)
    summary = run("kiosk-url-local", capsys=capsys)
    assert summary == {"changed": [], "skipped": ["kiosk-url (unit not in the expected form)"]}
    assert k.read_text() == kiosk_unit(url)
    assert origin_js.read_text() == f'const PR_KIOSK_HOME_URL = "{url}";\n'
    assert host.systemctl() == []


def test_kiosk_url_local_glued_continuation_is_not_the_expected_form(host, capsys):
    odd = kiosk_unit("https://pantry.example.com/ui/?kiosk=1").replace(
        "?kiosk=1\n", "?kiosk=1\\\n  --no-first-run\n")
    k = host.unit("foodassistant-kiosk.service", odd)
    summary = run("kiosk-url-local", capsys=capsys)
    assert summary["skipped"] == ["kiosk-url (unit not in the expected form)"]
    assert k.read_text() == odd


def test_kiosk_url_local_dry_run(host, capsys):
    url = "https://pantry.example.com/ui/?kiosk=1"
    k, origin_js, boot = _proxied(host, url)
    summary = run("kiosk-url-local", "--dry-run", capsys=capsys)
    assert {"kiosk-url", "kiosk-launch-dropin", "kiosk-launch"} <= set(summary["changed"])
    assert k.read_text() == kiosk_unit(url)
    assert origin_js.read_text() == f'const PR_KIOSK_HOME_URL = "{url}";\n'
    assert boot.read_text() == OLD_BOOT_DROPIN.replace("KIOSK_URL", url)
    assert not host.dropin("foodassistant-kiosk.service", "20-foodassistant-launch.conf").exists()
    assert host.systemctl() == []


# --- the command line ---------------------------------------------------------

def test_cli_last_line_is_the_json_summary_and_exit_zero(host, tmp_path):
    host.unit("foodassistant-streamdeck.service", DECK_UNIT)
    env = dict(os.environ)
    r = subprocess.run([sys.executable, str(HELPER), "local-credentials"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {
        "changed": ["streamdeck-key"], "skipped": []}
    r = subprocess.run([sys.executable, str(HELPER), "bogus"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {"changed": [], "skipped": []}


def test_local_host_rule():
    for host_ in ("127.0.0.1", "localhost", "::1", "[::1]", "Kitchen-Pi"):
        assert hh.is_local_kiosk_host(host_, "kitchen-pi"), host_
    for host_ in ("kitchen-pi.local", "pantry.example.com", "192.168.1.20", ""):
        assert not hh.is_local_kiosk_host(host_, "kitchen-pi"), host_
    assert not hh.is_local_kiosk_host("kitchen-pi.lan", "kitchen-pi.lan")


# --- boot-wifi-psk (FoodAssistant-1yi4i) ---------------------------------------
#
# The home Wi-Fi password sits in the boot partition's network-config after
# cloud-init has used it. It comes off only once the device keeps the same
# password in its own network configuration and cloud-init is told never to
# render the network again, so the device always rejoins after a reboot.

NETWORK_CONFIG = """# This file sets up the network on first start.
network:
  version: 2
  wifis:
    renderer: NetworkManager
    wlan0:
      dhcp4: true
      optional: true
      access-points:
        "Home Net":
          password: "s3cret pass"
      regulatory-domain: US
"""

NETPLAN = """network:
  version: 2
  wifis:
    wlan0:
      renderer: NetworkManager
      access-points:
        "Home Net":
          auth:
            key-management: "psk"
            password: "s3cret pass"
"""


class _Boot:
    def __init__(self, root):
        self.root = root
        self.boot = root / "boot"
        self.netplan = root / "netplan"
        self.nm = root / "nm"
        self.cloud = root / "cloud.cfg.d"
        self.finished = root / "boot-finished"
        for d in (self.boot, self.netplan, self.nm):
            d.mkdir(parents=True)
        (self.boot / "network-config").write_text(NETWORK_CONFIG)

    @property
    def config(self):
        return (self.boot / "network-config").read_text()


@pytest.fixture
def boot(tmp_path, monkeypatch):
    b = _Boot(tmp_path / "dev")
    monkeypatch.setenv("BOOT_DIRS", str(b.boot))
    monkeypatch.setenv("NETPLAN_DIR", str(b.netplan))
    monkeypatch.setenv("NM_CONN_DIR", str(b.nm))
    monkeypatch.setenv("CLOUD_CFG_DIR", str(b.cloud))
    monkeypatch.setenv("CLOUD_BOOT_FINISHED", str(b.finished))
    return b


def _psk_run(dry_run=False):
    run = hh.Run(dry_run=dry_run)
    hh.cmd_boot_wifi_psk(run)
    return run


def test_psk_is_removed_once_netplan_keeps_it(boot, capsys):
    boot.finished.write_text("done\n")
    (boot.netplan / "50-cloud-init.yaml").write_text(NETPLAN)
    run = _psk_run()
    assert run.changed == ["boot-wifi-psk"] and run.skipped == []
    text = boot.config
    assert "s3cret" not in text
    assert '"Home Net":' in text            # the network name stays
    assert "regulatory-domain: US" in text  # and everything else
    assert hh.PSK_REMOVED_COMMENT in text
    assert hh.config_passwords(text) == []
    dropin = (boot.cloud / hh.CLOUD_NET_DROPIN).read_text()
    assert "network: {config: disabled}" in dropin
    err = capsys.readouterr().err
    assert "Removed the Wi-Fi password" in err and "s3cret" not in err
    # Idempotent: a second run finds nothing to do.
    again = _psk_run()
    assert again.changed == [] and again.skipped == []


def test_psk_is_removed_when_networkmanager_keeps_it(boot):
    boot.finished.write_text("done\n")
    (boot.nm / "Home Net.nmconnection").write_text(
        "[connection]\nid=Home Net\ntype=wifi\n\n[wifi-security]\nkey-mgmt=wpa-psk\npsk=s3cret pass\n")
    run = _psk_run()
    assert run.changed == ["boot-wifi-psk"]
    assert "s3cret" not in boot.config


def test_psk_stays_until_the_device_keeps_it(boot, capsys):
    boot.finished.write_text("done\n")
    run = _psk_run()
    assert run.changed == []
    assert run.skipped == ["boot-wifi-psk (not saved on the device)"]
    assert boot.config == NETWORK_CONFIG
    assert not boot.cloud.exists()
    assert "s3cret" not in capsys.readouterr().err


def test_psk_stays_when_the_device_holds_a_different_password(boot):
    boot.finished.write_text("done\n")
    (boot.netplan / "50-cloud-init.yaml").write_text(NETPLAN.replace("s3cret pass", "other"))
    run = _psk_run()
    assert run.skipped == ["boot-wifi-psk (not saved on the device)"]
    assert boot.config == NETWORK_CONFIG


def test_psk_stays_until_cloud_init_has_finished(boot):
    (boot.netplan / "50-cloud-init.yaml").write_text(NETPLAN)
    run = _psk_run()
    assert run.skipped == ["boot-wifi-psk (cloud-init has not finished yet)"]
    assert boot.config == NETWORK_CONFIG


def test_psk_stays_when_cloud_init_cannot_be_told(boot, monkeypatch):
    boot.finished.write_text("done\n")
    (boot.netplan / "50-cloud-init.yaml").write_text(NETPLAN)
    blocker = boot.root / "blocked"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("CLOUD_CFG_DIR", str(blocker / "cloud.cfg.d"))
    run = _psk_run()
    assert run.skipped == ["boot-wifi-psk (cloud-init drop-in not written)"]
    assert boot.config == NETWORK_CONFIG


def test_psk_dry_run_writes_nothing(boot):
    boot.finished.write_text("done\n")
    (boot.netplan / "50-cloud-init.yaml").write_text(NETPLAN)
    run = _psk_run(dry_run=True)
    assert run.changed == ["boot-wifi-psk"]
    assert boot.config == NETWORK_CONFIG
    assert not boot.cloud.exists()


def test_no_network_config_or_no_password_is_quiet(boot):
    boot.finished.write_text("done\n")
    (boot.boot / "network-config").write_text("network:\n  version: 2\n  ethernets: {}\n")
    run = _psk_run()
    assert run.changed == [] and run.skipped == []
    (boot.boot / "network-config").unlink()
    run = _psk_run()
    assert run.changed == [] and run.skipped == []


def test_commented_password_lines_are_ignored():
    text = '        # password: "example"\n        password: real\n'
    assert [v for _, _, v in hh.config_passwords(text)] == ["real"]


@pytest.mark.parametrize("raw,value", [
    ('"quoted pass"', "quoted pass"),
    ("'single ''q'' pass'", "single 'q' pass"),
    ("plainpass  # a comment", "plainpass"),
    ('"esc\\"aped"', 'esc"aped'),
    ("", None),
    ("|", None),
    ('"unterminated', None),
])
def test_yaml_scalar(raw, value):
    assert hh.yaml_scalar(raw) == value


def test_cli_boot_wifi_psk_prints_the_summary(boot):
    boot.finished.write_text("done\n")
    (boot.netplan / "50-cloud-init.yaml").write_text(NETPLAN)
    r = subprocess.run([sys.executable, str(HELPER), "boot-wifi-psk"], env=dict(os.environ),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {
        "changed": ["boot-wifi-psk"], "skipped": []}
    assert "s3cret" not in r.stdout + r.stderr


def test_updater_and_firstboot_run_boot_wifi_psk_on_appliances_only():
    upd = (ROOT / "scripts" / "image-build" / "foodassistant-update").read_text()
    assert 'foodassistant-host-harden" boot-wifi-psk' in upd
    assert '"$_bw_mode" != "server"' in upd
    fb = (ROOT / "scripts" / "image-build" / "firstboot.sh").read_text()
    assert fb.count('_step_requested "boot_wifi_psk"') == 2
    assert '"$harden" boot-wifi-psk' in fb


# --- boot-password-hash (FoodAssistant-7qw44) ----------------------------------
#
# The account password hash cloud-init used from the boot partition's
# user-data comes off once the account on the device holds that same hash and
# cloud-init is told never to treat the device as new again.

HASH = "$5$examplesalt$Zm9vYmFyYmF6cXV4ZXhhbXBsZWhhc2hvbmx5MTIz"

USER_DATA = f"""#cloud-config
hostname: pr
manage_etc_hosts: true
users:
- name: kitchen
  groups: users,adm,sudo
  shell: /bin/bash
  lock_passwd: false
  passwd: {HASH}
enable_ssh: true
"""

SHADOW = f"root:*:20000:0:99999:7:::\nkitchen:{HASH}:20000:0:99999:7:::\n"


@pytest.fixture
def ud(boot, monkeypatch):
    (boot.boot / "network-config").unlink()
    (boot.boot / "user-data").write_text(USER_DATA)
    shadow = boot.root / "shadow"
    shadow.write_text(SHADOW)
    monkeypatch.setenv("SHADOW_FILE", str(shadow))
    boot.shadow = shadow
    return boot


def _hash_run(dry_run=False):
    run = hh.Run(dry_run=dry_run)
    hh.cmd_boot_password_hash(run)
    return run


def _user_data(b):
    return (b.boot / "user-data").read_text()


def test_hash_is_removed_once_the_account_holds_it(ud, capsys):
    ud.finished.write_text("done\n")
    run = _hash_run()
    assert run.changed == ["boot-password-hash"] and run.skipped == []
    text = _user_data(ud)
    assert HASH not in text
    assert "- name: kitchen" in text and "lock_passwd: false" in text
    assert "enable_ssh: true" in text
    assert hh.HASH_REMOVED_COMMENT in text
    assert hh.user_data_hashes(text) == []
    dropin = (ud.cloud / hh.CLOUD_USERS_DROPIN).read_text()
    assert "manual_cache_clean: true" in dropin
    out = capsys.readouterr()
    assert HASH not in out.err + out.out
    again = _hash_run()
    assert again.changed == [] and again.skipped == []


def test_hash_stays_until_cloud_init_has_finished(ud):
    run = _hash_run()
    assert run.skipped == ["boot-password-hash (cloud-init has not finished yet)"]
    assert _user_data(ud) == USER_DATA
    assert not ud.cloud.exists()


def test_hash_stays_when_the_account_is_missing(ud, capsys):
    ud.finished.write_text("done\n")
    ud.shadow.write_text("root:*:20000:0:99999:7:::\n")
    run = _hash_run()
    assert run.skipped == ["boot-password-hash (not set on the device)"]
    assert _user_data(ud) == USER_DATA
    assert not ud.cloud.exists()
    assert HASH not in capsys.readouterr().err


def test_hash_stays_when_the_account_holds_another_hash(ud):
    ud.finished.write_text("done\n")
    ud.shadow.write_text(SHADOW.replace(HASH, "$6$other$hash"))
    run = _hash_run()
    assert run.skipped == ["boot-password-hash (not set on the device)"]
    assert _user_data(ud) == USER_DATA


def test_hash_stays_when_accounts_cannot_be_read(ud):
    ud.finished.write_text("done\n")
    ud.shadow.unlink()
    run = _hash_run()
    assert run.skipped == ["boot-password-hash (accounts unreadable)"]
    assert _user_data(ud) == USER_DATA


def test_hash_without_an_account_name_is_left(ud):
    ud.finished.write_text("done\n")
    (ud.boot / "user-data").write_text(f"#cloud-config\npasswd: {HASH}\n")
    run = _hash_run()
    assert run.skipped == ["boot-password-hash (user-data not in the expected form)"]
    assert HASH in _user_data(ud)


def test_hash_stays_when_cloud_init_cannot_be_told(ud, monkeypatch):
    ud.finished.write_text("done\n")
    blocker = ud.root / "blocked"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("CLOUD_CFG_DIR", str(blocker / "cloud.cfg.d"))
    run = _hash_run()
    assert run.skipped == ["boot-password-hash (cloud-init drop-in not written)"]
    assert _user_data(ud) == USER_DATA


def test_hash_dry_run_writes_nothing(ud):
    ud.finished.write_text("done\n")
    run = _hash_run(dry_run=True)
    assert run.changed == ["boot-password-hash"]
    assert _user_data(ud) == USER_DATA
    assert not ud.cloud.exists()


def test_quoted_hashed_passwd_and_two_accounts(ud):
    ud.finished.write_text("done\n")
    other = "$6$salt2$c2Vjb25kZXhhbXBsZWhhc2g"
    (ud.boot / "user-data").write_text(
        "#cloud-config\nusers:\n"
        f"  - name: kitchen\n    passwd: \"{HASH}\"\n"
        f"  - name: 'second'\n    hashed_passwd: '{other}'\n"
        "  # passwd: $1$commented$out\n")
    ud.shadow.write_text(SHADOW + f"second:{other}:20000:0:99999:7:::\n")
    run = _hash_run()
    assert run.changed == ["boot-password-hash"]
    text = _user_data(ud)
    assert HASH not in text and other not in text
    assert "# passwd: $1$commented$out" in text
    assert text.count(hh.HASH_REMOVED_COMMENT) == 2


def test_no_user_data_or_no_hash_is_quiet(ud):
    ud.finished.write_text("done\n")
    (ud.boot / "user-data").write_text(
        "#cloud-config\nusers:\n- name: kitchen\n  lock_passwd: true\n")
    run = _hash_run()
    assert run.changed == [] and run.skipped == []
    (ud.boot / "user-data").unlink()
    run = _hash_run()
    assert run.changed == [] and run.skipped == []


def test_cli_boot_password_hash_prints_the_summary(ud):
    ud.finished.write_text("done\n")
    r = subprocess.run([sys.executable, str(HELPER), "boot-password-hash"],
                       env=dict(os.environ), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {
        "changed": ["boot-password-hash"], "skipped": []}
    assert HASH not in r.stdout + r.stderr


def test_updater_and_firstboot_run_boot_password_hash_on_appliances_only():
    upd = (ROOT / "scripts" / "image-build" / "foodassistant-update").read_text()
    assert 'foodassistant-host-harden" boot-password-hash' in upd
    block = upd.split("grep -q 'boot-password-hash'", 1)[0].rsplit("if ", 1)[1]
    assert '"$_bw_mode" != "server"' in block
    fb = (ROOT / "scripts" / "image-build" / "firstboot.sh").read_text()
    scrub = fb.split("scrub_boot_wifi_psk() {", 1)[1].split("\n}\n", 1)[0]
    assert '"$harden" boot-password-hash' in scrub
