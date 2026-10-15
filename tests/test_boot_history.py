"""Boot history (FoodAssistant-85f64): the per-boot record that says whether
the previous run of a Pi ended cleanly, the bridge's part in it (the first
sighting of a power flag, the since-boot warning, the read-only route), and
where the app shows it (an action item and the support bundle).

The journal lives in RAM on the appliances, so this file is the only
evidence a power cut or a freeze leaves. Everything here runs against temp
files; nothing reads the machine running the tests.
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
RECORD = ROOT / "scripts" / "image-build" / "foodassistant-boot-record"
UNIT = ROOT / "scripts" / "image-build" / "foodassistant-boot-record.service"
BRIDGE = ROOT / "scripts" / "image-build" / "foodassistant-host-bridge"
SERVICE = ROOT / "service"
sys.path.insert(0, str(SERVICE))


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


rec = _load("foodassistant_boot_record", RECORD)
bridge = _load("foodassistant_host_bridge_boot", BRIDGE)

UNCLEAN_KLOG = """\
EXT4-fs (mmcblk0p2): INFO: recovery required on readonly filesystem
EXT4-fs (mmcblk0p2): write access will be enabled during recovery
EXT4-fs (mmcblk0p2): orphan cleanup on readonly fs
EXT4-fs (mmcblk0p2): 3 orphan inodes deleted
EXT4-fs (mmcblk0p2): recovery complete
EXT4-fs (mmcblk0p2): mounted filesystem 1234 ro with ordered data mode.
"""
CLEAN_KLOG = """\
Booting Linux on physical CPU 0x0
EXT4-fs (mmcblk0p2): mounted filesystem 1234 ro with ordered data mode.
EXT4-fs (mmcblk0p2): re-mounted 1234 r/w.
EXT4-fs (sda1): recovery complete
"""


# --- the record helper: pure parts --------------------------------------------

def test_kernel_log_with_recovery_is_unclean():
    state, msgs = rec.classify_kernel_log(UNCLEAN_KLOG, "mmcblk0p2")
    assert state == "unclean"
    assert any("recovery complete" in m for m in msgs)
    assert any("orphan" in m for m in msgs)
    assert len(msgs) <= rec.MAX_MESSAGES


def test_kernel_log_with_a_plain_mount_is_clean():
    # A recovery on another disk (a USB stick) says nothing about the root.
    assert rec.classify_kernel_log(CLEAN_KLOG, "mmcblk0p2") == ("clean", [])


def test_kernel_log_without_the_root_mount_is_unknown():
    assert rec.classify_kernel_log("usb 1-1: new device\n", "mmcblk0p2") == ("unknown", [])
    assert rec.classify_kernel_log("", "") == ("unknown", [])


def test_unknown_root_device_reads_every_ext4_line():
    assert rec.classify_kernel_log(UNCLEAN_KLOG, "")[0] == "unclean"


def test_last_alive_is_the_previous_runs_heartbeat():
    boot = 1_000_000.0
    assert rec.last_alive(None, boot, True) is None
    # At boot the app has not started yet: whatever is there is the last run's.
    assert rec.last_alive(boot + 50, boot, True) == boot + 50
    # Later in the boot, a heartbeat from this run says nothing.
    assert rec.last_alive(boot + 50, boot, False) is None
    assert rec.last_alive(boot - 50, boot, False) == boot - 50


def test_add_entry_keeps_one_line_per_boot_and_the_last_200():
    entries = [{"boot_id": f"b{i}"} for i in range(rec.MAX_LINES)]
    assert rec.add_entry(entries, {"boot_id": "b5"}) is None
    out = rec.add_entry(entries, {"boot_id": "new"})
    assert len(out) == rec.MAX_LINES
    assert out[0]["boot_id"] == "b1" and out[-1]["boot_id"] == "new"


def test_parse_history_skips_damaged_lines():
    text = '{"boot_id": "a"}\n\x00\x00garbage\n[1, 2]\n{"boot_id": "b"}\n'
    assert [e["boot_id"] for e in rec.parse_history(text)] == ["a", "b"]


# --- the record helper: a run --------------------------------------------------

def _record_env(tmp_path, klog=UNCLEAN_KLOG, boot_id="boot-2", uptime="12.34 40.0\n",
                heartbeat_age=None):
    (tmp_path / "klog").write_text(klog)
    (tmp_path / "boot_id").write_text(boot_id + "\n")
    (tmp_path / "uptime").write_text(uptime)
    vc = tmp_path / "vcgencmd"
    vc.write_text("#!/bin/sh\necho throttled=0x50005\n")
    vc.chmod(0o755)
    hb = tmp_path / "app-instance.json"
    if heartbeat_age is not None:
        hb.write_text("{}")
        t = 1_700_000_000
        os.utime(hb, (t, t))
    return {
        **os.environ,
        "BOOT_HISTORY": str(tmp_path / "lib" / "boot-history.jsonl"),
        "BOOT_HISTORY_LOCK": str(tmp_path / "history.lock"),
        "BOOT_RECORD_KLOG": str(tmp_path / "klog"),
        "BOOT_ID_FILE": str(tmp_path / "boot_id"),
        "UPTIME_FILE": str(tmp_path / "uptime"),
        "VCGENCMD": str(vc),
        "HEARTBEAT_FILE": str(hb),
        "ROOT_DEV": "mmcblk0p2",
        "TIMESYNC_FLAG": str(tmp_path / "no-sync-flag"),
    }


def _run_record(env, *args):
    return subprocess.run([sys.executable, str(RECORD), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def test_a_boot_after_a_power_cut_is_recorded(tmp_path):
    env = _record_env(tmp_path, heartbeat_age=1)
    r = _run_record(env, "--at-boot")
    assert r.returncode == 0, r.stderr
    hist = Path(env["BOOT_HISTORY"])
    entries = rec.parse_history(hist.read_text())
    assert len(entries) == 1
    e = entries[0]
    assert e["boot_id"] == "boot-2"
    assert e["previous_shutdown"] == "unclean"
    assert e["previous_last_alive"] == "2023-11-14T22:13:20Z"
    assert e["throttled"] == "0x50005"
    assert e["uptime_s"] == 12.3
    assert e["clock_synced"] is False
    assert oct(hist.stat().st_mode & 0o777) == "0o644"


def test_the_same_boot_is_recorded_once(tmp_path):
    env = _record_env(tmp_path)
    _run_record(env, "--at-boot")
    hist = Path(env["BOOT_HISTORY"])
    first = hist.read_bytes()
    r = _run_record(env)
    assert "already recorded" in r.stdout
    assert hist.read_bytes() == first
    # The next boot adds a line.
    (tmp_path / "boot_id").write_text("boot-3\n")
    (tmp_path / "klog").write_text(CLEAN_KLOG)
    _run_record(env, "--at-boot")
    entries = rec.parse_history(hist.read_text())
    assert [(e["boot_id"], e["previous_shutdown"]) for e in entries] == [
        ("boot-2", "unclean"), ("boot-3", "clean")]


def test_a_record_never_fails_the_boot(tmp_path):
    env = _record_env(tmp_path)
    env["BOOT_HISTORY"] = "/proc/nope/boot-history.jsonl"
    r = _run_record(env, "--at-boot")
    assert r.returncode == 0


def test_the_unit_runs_before_the_app_starts():
    text = UNIT.read_text()
    assert "Type=oneshot" in text
    assert "ExecStart=/usr/local/bin/foodassistant-boot-record --at-boot" in text
    before = next(ln for ln in text.splitlines() if ln.startswith("Before="))
    assert "docker.service" in before and "foodassistant-remote.service" in before
    assert "WantedBy=multi-user.target" in text


# --- the bridge -------------------------------------------------------------------

def _history(tmp_path, *entries):
    p = tmp_path / "boot-history.jsonl"
    p.write_text(bridge._render_boot_history(list(entries)))
    return p


def test_unclean_boot_raises_a_since_boot_warning():
    entry = {"boot_id": "b", "previous_shutdown": "unclean"}
    snap = bridge._warnings_snapshot(0, 40.0, 10, 5.0, checked_epoch=1.0, boot_entry=entry)
    assert snap["warnings"] == [{
        "key": "unclean_shutdown", "message": bridge._UNCLEAN_MESSAGE, "live": False}]
    for state in ("clean", "unknown"):
        snap = bridge._warnings_snapshot(
            0, 40.0, 10, 5.0, checked_epoch=1.0,
            boot_entry={"boot_id": "b", "previous_shutdown": state})
        assert snap["warnings"] == []
    assert bridge._warnings_snapshot(0, 40.0, 10, 5.0, checked_epoch=1.0)["warnings"] == []


def test_current_boot_entry_is_found_by_boot_id(tmp_path):
    p = _history(tmp_path, {"boot_id": "a", "previous_shutdown": "unclean"},
                 {"boot_id": "b", "previous_shutdown": "clean"})
    assert bridge._current_boot_entry(str(p), "b")["previous_shutdown"] == "clean"
    assert bridge._current_boot_entry(str(p), "c") is None
    assert bridge._current_boot_entry(str(tmp_path / "missing"), "b") is None


def test_throttle_seen_update_keeps_the_first_sighting():
    entry = {"boot_id": "b"}
    assert bridge._throttle_seen_update(entry, 0, 0, 1.0) is None
    assert bridge._throttle_seen_update(entry, 0x1, 0, 1.0) is None  # live only
    first = bridge._throttle_seen_update(entry, 0x10001, 1_700_000_000, 42.0)
    assert first == {"first_seen": "2023-11-14T22:13:20Z", "first_uptime_s": 42.0,
                     "bits": "0x10000", "flags": ["undervoltage"]}
    entry["throttle_seen"] = first
    assert bridge._throttle_seen_update(entry, 0x10000, 1_700_000_100, 99.0) is None
    later = bridge._throttle_seen_update(entry, 0x50000, 1_700_000_100, 99.0)
    assert later["first_seen"] == first["first_seen"]
    assert later["first_uptime_s"] == 42.0
    assert later["bits"] == "0x50000"
    assert later["flags"] == ["throttled", "undervoltage"]
    assert later["changed"] == "2023-11-14T22:15:00Z"


def test_throttle_sighting_is_written_once(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_throttle_noted", ("", 0))
    p = _history(tmp_path, {"boot_id": "old"}, {"boot_id": "b", "previous_shutdown": "clean"})
    lock = str(tmp_path / "lock")
    kw = dict(path=str(p), boot_id="b", now_epoch=1_700_000_000, uptime_s=5.0, lock_path=lock)
    assert bridge._note_throttle_sighting(0x50005, **kw) is True
    entries = bridge._read_boot_history(str(p))
    assert entries[0] == {"boot_id": "old"}
    assert entries[1]["throttle_seen"]["bits"] == "0x50000"
    before = p.read_bytes()
    # The same flags again write nothing (and are not even read again).
    assert bridge._note_throttle_sighting(0x50005, **kw) is False
    assert p.read_bytes() == before
    assert oct(p.stat().st_mode & 0o777) == "0o644"


def test_throttle_sighting_waits_for_the_boot_record(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_throttle_noted", ("", 0))
    p = _history(tmp_path, {"boot_id": "old"})
    before = p.read_bytes()
    assert bridge._note_throttle_sighting(
        0x10000, path=str(p), boot_id="b", now_epoch=1.0, uptime_s=1.0,
        lock_path=str(tmp_path / "lock")) is False
    assert p.read_bytes() == before
    # Once the record has the running boot, the sighting lands.
    p.write_text(bridge._render_boot_history([{"boot_id": "old"}, {"boot_id": "b"}]))
    assert bridge._note_throttle_sighting(
        0x10000, path=str(p), boot_id="b", now_epoch=1.0, uptime_s=1.0,
        lock_path=str(tmp_path / "lock")) is True


def test_boot_history_route_needs_the_token(tmp_path):
    assert "/system/boot-history" not in bridge.TOKEN_EXEMPT_GET
    p = _history(tmp_path, *({"boot_id": f"b{i}"} for i in range(5)))
    payload = bridge._boot_history_payload(str(p), "b4", limit=3)
    assert payload["ok"] is True and payload["boot_id"] == "b4"
    assert [e["boot_id"] for e in payload["entries"]] == ["b2", "b3", "b4"]
    assert "/system/boot-history" in BRIDGE.read_text().split('"""', 2)[1]


# --- boot partition writes in the bridge ---------------------------------------

def test_touch_provision_remounts_a_read_only_boot_partition(tmp_path, monkeypatch):
    cfg = tmp_path / "config.txt"
    cfg.write_text("dtparam=audio=on")  # no trailing newline
    calls = []
    monkeypatch.setattr(bridge, "_mount_of", lambda path: ("/boot/firmware", "ro,relatime,fmask=0077"))

    class Done:
        returncode = 0
        stdout = stderr = ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return Done()
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    synced = []
    monkeypatch.setattr(bridge.os, "sync", lambda: synced.append(True))
    assert bridge._provision_touch("ads7846", str(cfg)) == (True, True)
    assert calls == [["mount", "-o", "remount,rw", "/boot/firmware"],
                     ["mount", "-o", "remount,ro", "/boot/firmware"]]
    assert synced
    body = cfg.read_text()
    assert body.startswith("dtparam=audio=on\n\n[all]\n")
    assert "dtoverlay=ads7846," in body
    assert not list(tmp_path.glob(".config.txt.*"))


def test_touch_provision_leaves_a_writable_boot_partition_mounted_as_it_is(tmp_path, monkeypatch):
    cfg = tmp_path / "config.txt"
    cfg.write_text("dtparam=audio=on\n")
    monkeypatch.setattr(bridge, "_mount_of", lambda path: ("/boot/firmware", "rw,relatime"))

    def no_run(cmd, **kw):
        raise AssertionError(f"unexpected command {cmd}")
    monkeypatch.setattr(bridge.subprocess, "run", no_run)
    monkeypatch.setattr(bridge.os, "sync", lambda: None)
    assert bridge._provision_touch("ads7846", str(cfg)) == (True, True)


def test_no_boot_writer_appends_in_place():
    src = BRIDGE.read_text()
    body = src[src.index("def _provision_touch("):]
    body = body[:body.index("\ndef ")]
    assert 'open(cfg, "a")' not in body and "_write_boot_file(cfg" in body


# --- the app ----------------------------------------------------------------------

@pytest.fixture
def db():
    from app.database import SessionLocal
    from app.models.db_models import ActionItem
    s = SessionLocal()
    s.query(ActionItem).delete()
    s.commit()
    try:
        yield s
    finally:
        s.query(ActionItem).delete()
        s.commit()
        s.close()


def test_unclean_shutdown_becomes_an_action_item_that_clears(db):
    from app.services import action_items as ai
    warn = {"key": "unclean_shutdown", "message": "m", "live": False}
    ai.sync_system_warnings(db, [warn])
    items = ai.list_active(db)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "The device lost power or froze"
    assert "power supply" in item["body"]
    assert "Reboot now" in item["body"]
    assert "\u2014" not in item["body"]
    assert item["payload"]["url"] == ai.POWER_GUIDE_URL
    # The next clean start drops it from the feed and the inbox.
    ai.sync_system_warnings(db, [])
    assert ai.list_active(db) == []


def test_unclean_shutdown_never_toasts():
    from app.services import pi_health
    toasts, active = pi_health.warnings_to_toast(
        set(), [{"key": "unclean_shutdown", "message": "m", "live": False}])
    assert toasts == [] and active == set()


def test_support_bundle_carries_the_boot_history():
    from app.services import support_bundle as sb
    payload = {"ok": True, "boot_id": "b2", "entries": [
        {"boot_id": "b1", "previous_shutdown": "clean"},
        {"boot_id": "b2", "previous_shutdown": "unclean",
         "fs_messages": ["EXT4-fs (mmcblk0p2): recovery complete"]}]}
    text = sb.boot_history_text(payload)
    lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert [json.loads(ln)["boot_id"] for ln in lines] == ["b1", "b2"]
    assert "running boot: b2" in text
    assert sb.boot_history_text({"ok": True, "entries": []}) is None
    assert sb.boot_history_text(None) is None


def test_support_bundle_route_asks_the_bridge_for_the_history():
    src = (SERVICE / "app" / "routers" / "admin.py").read_text()
    assert "/system/boot-history" in src
    assert 'host_sections["boot-history"]' in src
