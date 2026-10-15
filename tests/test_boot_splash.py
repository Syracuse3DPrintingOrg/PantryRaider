"""Animated framebuffer boot screen (FoodAssistant-y8vj).

Covers the shipped pieces without a device: the boot screen
(scripts/image-build/foodassistant-boot-splash, stdlib-only Python) is
exercised as a module and as a process against a plain file standing in for
/dev/fb0; the build-time art builder (make-boot-splash.py) must produce art
the boot screen loads; and the unit, the getty@tty1 drop-in and the kiosk's
VT switch are content-pinned, since each one alone decides whether the
console ends up drawn over the animation.

Run: python -m pytest tests/test_boot_splash.py -q
"""
from __future__ import annotations

import gzip
import http.server
import importlib.machinery
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
IMAGE_BUILD = REPO / "scripts" / "image-build"
WRITER = IMAGE_BUILD / "foodassistant-boot-splash"
GENERATOR = IMAGE_BUILD / "make-boot-splash.py"
UNIT = IMAGE_BUILD / "foodassistant-boot-splash.service"
ASSET = IMAGE_BUILD / "boot-splash" / "splash-art.gz"
GETTY = IMAGE_BUILD / "boot-splash" / "getty-tty1.conf"
FIRSTBOOT = IMAGE_BUILD / "firstboot.sh"

BG = (33, 37, 41)
CHVT_LINE = "ExecStartPre=-+/usr/bin/timeout 10 /usr/bin/chvt 7"


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


splash = _load("fa_boot_splash", WRITER)
ART = splash.load_art(str(ASSET))


def _unit_lines(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def _firstboot_kiosk_unit() -> str:
    text = FIRSTBOOT.read_text()
    start = text.index("cat > /etc/systemd/system/foodassistant-kiosk.service <<EOF")
    return text[start:text.index("\nEOF\n", start)]


# -- the unit -------------------------------------------------------------------

def test_unit_is_a_long_oneshot_that_holds_nothing_up():
    lines = _unit_lines(UNIT)
    for want in (
        "Type=oneshot",
        "DefaultDependencies=no",
        "WantedBy=sysinit.target",
        "After=systemd-udev-trigger.service local-fs.target",
        "Conflicts=shutdown.target",
        "Before=shutdown.target",
        "ConditionKernelCommandLine=!splash",
        "ExecStart=/usr/local/bin/foodassistant-boot-splash",
        "ConditionPathExists=/opt/foodassistant/boot-splash/splash-art.gz",
        "ConditionPathExists=/usr/local/bin/foodassistant-boot-splash",
    ):
        assert want in lines, want
    # Nothing in the boot may wait on it: the only Before= is shutdown.
    assert [ln for ln in lines if ln.startswith("Before=")] == ["Before=shutdown.target"]
    # The DSI panel registers late: the script polls for /dev/fb0 instead of a
    # Condition that would skip the boot screen on every slow probe.
    assert "ConditionPathExists=/dev/fb0" not in lines
    # systemd never kills it before its own give-up.
    timeout = [int(ln.split("=", 1)[1]) for ln in lines if ln.startswith("TimeoutStartSec=")]
    assert timeout and timeout[0] > splash.GIVE_UP


def test_unit_parses_with_systemd_analyze_when_available(tmp_path):
    import shutil
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze not available")
    dst = tmp_path / UNIT.name
    dst.write_text(UNIT.read_text())
    proc = subprocess.run(["systemd-analyze", "verify", str(dst)],
                          capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    problems = [ln for ln in out.splitlines()
                if ln.strip() and "is not executable" not in ln
                and "No such file" not in ln]
    assert proc.returncode == 0 or problems == [], out


def test_getty_waits_for_the_boot_screen():
    lines = _unit_lines(GETTY)
    assert lines[0] == "[Unit]"
    assert "After=foodassistant-boot-splash.service" in lines
    # A kiosk device keeps tty1 free of a login prompt, as before.
    assert "ConditionPathExists=!/etc/systemd/system/foodassistant-kiosk.service" in lines
    assert not any(ln.startswith("Before=") for ln in lines)


def test_firstboot_kiosk_switches_to_its_own_vt_after_the_app_wait():
    unit = _firstboot_kiosk_unit()
    lines = [ln.strip() for ln in unit.splitlines()]
    assert CHVT_LINE in lines
    assert "TTYPath=/dev/tty7" in lines and "UtmpIdentifier=tty7" in lines
    # No Conflicts= with the tty1 console: a VT switch to tty1 starts getty,
    # which would otherwise stop the kiosk.
    assert not any("getty@tty1" in ln for ln in lines)
    wait = next(i for i, ln in enumerate(lines)
                if ln.startswith("ExecStartPre=") and "kiosk_wait_url" in ln)
    chvt = lines.index(CHVT_LINE)
    start = next(i for i, ln in enumerate(lines) if ln.startswith("ExecStart="))
    assert wait < chvt < start
    # The boot screen recognises the line it waits for.
    assert splash.kiosk_switches_vt(unit)


def test_firstboot_kiosk_runs_nothing_next_to_cage():
    # systemd resets and hangs up the unit's TTY (TTYReset=, TTYVHangup=) for
    # every command it starts. An ExecStartPost= starts right next to cage, and
    # when it won that race cage died of SIGHUP before it opened the display:
    # a black screen until the 30 s rotation retry gave up and Restart=always
    # came round. So the unit has no command that runs alongside cage, and the
    # rotation helper is queued as its own unit instead.
    lines = [ln.strip() for ln in _firstboot_kiosk_unit().splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    assert "TTYVHangup=yes" in lines and "TTYReset=yes" in lines
    for key in ("ExecStartPost=", "ExecReload=", "ExecStop="):
        assert not any(ln.startswith(key) for ln in lines), key
    rot = [i for i, ln in enumerate(lines)
           if ln.startswith("ExecStartPre=") and "foodassistant-apply-rotation" in ln]
    assert len(rot) == 1
    queue = lines[rot[0]]
    # Root (+, to reach the system manager) and never fatal (-). --no-block is
    # load-bearing: the queued unit is ordered after the kiosk, so waiting for
    # it from the kiosk's own ExecStartPre would deadlock until the timeout.
    assert queue.startswith("ExecStartPre=-+/usr/bin/systemd-run ")
    assert "--no-block" in queue.split() and "--wait" not in queue
    assert "--property=After=foodassistant-kiosk.service" in queue.split()
    assert queue.endswith(" /usr/local/bin/foodassistant-apply-rotation")
    # Queued after the VT switch and before cage starts.
    chvt = lines.index(CHVT_LINE)
    start = next(i for i, ln in enumerate(lines) if ln.startswith("ExecStart="))
    assert chvt < rot[0] < start


def test_boot_screen_refuses_a_kiosk_without_chvt():
    assert not splash.kiosk_switches_vt("[Service]\nTTYPath=/dev/tty1\nExecStart=/usr/bin/cage\n")
    assert not splash.kiosk_switches_vt("# ExecStartPre=-+/usr/bin/chvt 7\n")
    assert splash.kiosk_switches_vt("[Service]\n" + CHVT_LINE + "\n")


def test_health_target_follows_the_kiosk_wait():
    unit = ("ExecStartPre=-/bin/sh -c 'curl -sf -o /dev/null --max-time 2 "
            "\"http://localhost/health\" && exit 0'\n")
    assert splash.health_target(unit) == ("127.0.0.1", 80, "/health")
    assert splash.health_target("x http://10.0.0.5:9284/health y") == ("10.0.0.5", 9284, "/health")
    assert splash.health_target("") == ("127.0.0.1", 9284, "/health")
    assert splash.health_target("curl https://pr.example/health") is None


# -- rotation and pixel formats -------------------------------------------------

@pytest.mark.parametrize("rotation,corners", [
    # logical (0,0), (W-1,0), (0,H-1) on a 6x4 framebuffer
    (0, [(0, 0), (5, 0), (0, 3)]),
    (270, [(5, 0), (5, 3), (0, 0)]),
    (90, [(0, 3), (0, 0), (5, 3)]),
    (180, [(5, 3), (0, 3), (5, 0)]),
])
def test_rotation_mapping(rotation, corners):
    xres, yres = 6, 4
    W, H = splash.logical_size(xres, yres, rotation)
    assert (W, H) == ((4, 6) if rotation in (90, 270) else (6, 4))
    got = [splash.map_point(x, y, xres, yres, rotation)
           for x, y in [(0, 0), (W - 1, 0), (0, H - 1)]]
    assert got == corners
    # A bijection onto the framebuffer.
    pts = {splash.map_point(x, y, xres, yres, rotation) for x in range(W) for y in range(H)}
    assert pts == {(x, y) for x in range(xres) for y in range(yres)}


def test_parse_rotation():
    assert splash.parse_rotation("270\n") == 270
    assert splash.parse_rotation("WLR_OUTPUT_TRANSFORM=90") == 90
    assert splash.parse_rotation("normal") == 0
    assert splash.parse_rotation("flipped-90") == 0
    assert splash.parse_rotation("") == 0


def test_rgb565_packing():
    f = splash.PixelFormat.rgb565()
    assert f.pack((255, 0, 0)) == b"\x00\xf8"
    assert f.pack((0, 255, 0)) == b"\xe0\x07"
    assert f.pack((0, 0, 255)) == b"\x1f\x00"
    assert f.pack((255, 255, 255)) == b"\xff\xff"
    assert f.pack(BG) == ((33 >> 3) << 11 | (37 >> 2) << 5 | (41 >> 3)).to_bytes(2, "little")


def test_xrgb8888_packing():
    f = splash.PixelFormat.xrgb8888()
    assert f.pack((10, 20, 30)) == bytes((30, 20, 10, 255))
    # The same layout read from FBIOGET_VSCREENINFO offsets and lengths.
    g = splash.PixelFormat(32, (16, 8), (8, 8), (0, 8))
    assert g.pack((1, 2, 3)) == f.pack((1, 2, 3))
    # A BGR565 panel puts red in the low bits.
    h = splash.PixelFormat(16, (0, 5), (5, 6), (11, 5))
    assert h.pack((255, 0, 0)) == b"\x1f\x00"


def test_odd_depths_are_refused():
    for bpp in (8, 24):
        with pytest.raises(ValueError):
            splash.PixelFormat(bpp, (16, 8), (8, 8), (0, 8))


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("bpp,pad", [(16, 6), (32, 0)])
def test_copy_out_turns_every_pixel(rotation, bpp, pad):
    xres, yres = 7, 5
    fmt = splash.PixelFormat.rgb565() if bpp == 16 else splash.PixelFormat.xrgb8888()
    B = fmt.bytes
    out = splash.Output(xres, yres, xres * B + pad, fmt, rotation)
    W, H = out.W, out.H
    canvas = bytearray(W * H * B)
    for y in range(H):
        for x in range(W):
            canvas[(y * W + x) * B:(y * W + x + 1) * B] = (y * W + x + 1).to_bytes(B, "little")
    # Two boxes covering the canvas, so partial boxes are exercised too.
    out.copy(canvas, (0, 0, W, H // 2))
    out.copy(canvas, (0, H // 2, W, H))
    for y in range(H):
        for x in range(W):
            px, py = splash.map_point(x, y, xres, yres, rotation)
            o = py * out.stride + px * B
            assert int.from_bytes(out.shadow[o:o + B], "little") == y * W + x + 1


# -- every item draws from exactly what its key holds ---------------------------

def _timeline(frames):
    """Bar values over a boot: a time-based guess, the app answering at
    frame 70, then the fill."""
    prog = splash.Progress(expected=3.0)
    for i in range(frames):
        t = i / 30
        yield t, prog.at(t, t, i >= 70)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("xres,yres,bpp", [
    (800, 480, 16),     # the official 7 inch panel
    (720, 1280, 32),    # Touch Display 2
    (1024, 600, 16),
])
def test_frames_built_up_equal_frames_drawn_whole(xres, yres, bpp, rotation):
    fmt = splash.PixelFormat.rgb565() if bpp == 16 else splash.PixelFormat.xrgb8888()
    out = splash.Output(xres, yres, xres * fmt.bytes, fmt, rotation)
    art = splash.pick_set(ART, min(out.W, out.H))
    live = splash.Scene(out.W, out.H, fmt, art)
    ref = splash.Scene(out.W, out.H, fmt, art)
    ref_out = splash.Output(xres, yres, xres * fmt.bytes, fmt, rotation)
    changed = 0
    for i, (t, value) in enumerate(_timeline(110)):
        keys = live.keys_at(t, value)
        boxes = live.update(keys)
        changed += bool(boxes)
        for box in boxes:
            out.copy(live.canvas, box)
        ref.keys = None
        ref.draw_whole(keys)
        assert live.canvas == ref.canvas, f"canvas differs at frame {i}"
        if i % 10 == 0 or i == 109:
            ref_out.copy(ref.canvas, (0, 0, ref.W, ref.H))
            assert out.shadow == ref_out.shadow, f"framebuffer differs at frame {i}"
    assert changed > 30  # the fade, the float and the bar all moved


def test_scene_layout_fits_and_the_mark_floats():
    fmt = splash.PixelFormat.rgb565()
    sc = splash.Scene(800, 480, fmt, splash.pick_set(ART, 480))
    a = sc.extent("mark", sc.keys_at(BOB := splash.BOB_PERIOD / 4, 0)["mark"])
    b = sc.extent("mark", sc.keys_at(3 * BOB, 0)["mark"])
    assert a[1] - b[1] == 2 * sc.amp and sc.amp >= 2
    for box in (a, b, sc.extent("word", (9,)), sc.extent("bar", (0, 9))):
        assert 0 <= box[0] < box[2] <= 800 and 0 <= box[1] < box[3] <= 480
    # Stacked: mark, wordmark, bar.
    assert a[3] <= sc.extent("word", (9,))[1] < sc.extent("bar", (0, 9))[1]


def test_pick_set():
    sets = {320: "s", 480: "m", 720: "l"}
    assert splash.pick_set(sets, 480) == "m"
    assert splash.pick_set(sets, 500) == "m"
    assert splash.pick_set(sets, 700) == "l"   # within 10%
    assert splash.pick_set(sets, 200) == "s"


# -- progress -------------------------------------------------------------------

def test_progress_waits_below_the_cap_and_never_goes_back():
    p = splash.Progress(expected=40)
    seen = [p.at(t, t, False) for t in range(0, 300, 2)]
    assert seen == sorted(seen)
    assert max(seen) <= splash.CAP
    assert seen[-1] > 0.95
    # Halfway to the expected time is a little under half.
    assert 0.4 < splash.Progress(40).at(20, 20, False) < 0.5
    # A value never drops, even if the input does.
    q = splash.Progress(expected=40)
    hi = q.at(30, 30, False)
    assert q.at(5, 31, False) == hi


def test_progress_fills_once_the_app_answers():
    p = splash.Progress(expected=100)
    v = p.at(10, 10.0, False)
    assert v < 0.2
    assert p.at(11, 10.1, True) < 1.0
    assert p.at(11, 10.1 + splash.FILL_S, True) == 1.0


def test_learned_boot_time_round_trips(tmp_path):
    state = tmp_path / "lib" / "boot-splash.json"
    assert splash.load_expected(str(state)) == splash.DEFAULT_BOOT_S
    splash.save_expected(str(state), 37.26)
    assert json.loads(state.read_text()) == {"boot_s": 37.3}
    assert splash.load_expected(str(state)) == 37.3
    state.write_text('{"boot_s": 9999}')
    assert splash.load_expected(str(state)) == splash.DEFAULT_BOOT_S
    state.write_text("not json")
    assert splash.load_expected(str(state)) == splash.DEFAULT_BOOT_S


# -- running it against a fake framebuffer -----------------------------------

def _fake_fb(tmp_path: Path, xres: int, yres: int, bpp: int, stride: int):
    sys_dir = tmp_path / "fb-sys"
    sys_dir.mkdir()
    (sys_dir / "virtual_size").write_text(f"{xres},{yres}\n")
    (sys_dir / "bits_per_pixel").write_text(f"{bpp}\n")
    (sys_dir / "stride").write_text(f"{stride}\n")
    fb = tmp_path / "fb0"
    fb.write_bytes(b"\0" * (stride * yres))
    return sys_dir, fb


def _env(tmp_path, sys_dir, fb, **extra):
    env = {
        **os.environ,
        "SPLASH_ASSET": str(ASSET),
        "SPLASH_FB": str(fb),
        "SPLASH_FB_SYS": str(sys_dir),
        "SPLASH_VT": "off",
        "SPLASH_ROTATION": "normal",
        "SPLASH_STATE": str(tmp_path / "state" / "boot-splash.json"),
        "SPLASH_KIOSK_UNIT": str(tmp_path / "kiosk.service"),
        "SPLASH_HEALTH_URL": "http://127.0.0.1:9/health",
        "SPLASH_FB_WAIT": "0",
    }
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _start(env):
    return subprocess.Popen([sys.executable, str(WRITER)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_painted(fb: Path, proc, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if fb.read_bytes().count(0) < len(fb.read_bytes()) // 2:
            return
        assert proc.poll() is None, proc.communicate()
        time.sleep(0.05)
    raise AssertionError("nothing painted")


def test_hand_off_on_sigusr1_paints_and_learns(tmp_path):
    xres, yres = 800, 480
    stride = xres * 2 + 32
    sys_dir, fb = _fake_fb(tmp_path, xres, yres, 16, stride)
    env = _env(tmp_path, sys_dir, fb, SPLASH_UPTIME=42)
    proc = _start(env)
    _wait_painted(fb, proc)
    time.sleep(0.6)  # past the fade
    proc.send_signal(signal.SIGUSR1)
    _, err = proc.communicate(timeout=5)
    assert proc.returncode == 0
    assert "handed over" in err and "app answered never" in err
    data = fb.read_bytes()
    bg = splash.PixelFormat.rgb565().pack(BG)
    assert data[0:2] == bg and data[(yres - 1) * stride:(yres - 1) * stride + 2] == bg
    pink = splash.PixelFormat.rgb565().pack(splash.BAR_FILL)
    assert pink in data or splash.PixelFormat.rgb565().pack((251, 9, 120)) in data
    # The stride padding is never painted over.
    assert data[xres * 2:stride] == b"\0" * (stride - xres * 2)
    # No app seen: the boot time is learned from the switch moment.
    learned = json.loads((tmp_path / "state" / "boot-splash.json").read_text())["boot_s"]
    assert 42 <= learned < 52


class _Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def test_health_answer_is_learned(tmp_path):
    server = http.server.HTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        sys_dir, fb = _fake_fb(tmp_path, 480, 800, 32, 480 * 4)
        env = _env(tmp_path, sys_dir, fb, SPLASH_UPTIME=20, SPLASH_ROTATION=270,
                   SPLASH_HEALTH_URL="http://127.0.0.1:%d/health" % server.server_port)
        proc = _start(env)
        _wait_painted(fb, proc)
        time.sleep(1.0)
        proc.send_signal(signal.SIGUSR1)
        _, err = proc.communicate(timeout=5)
    finally:
        server.shutdown()
    assert proc.returncode == 0
    m = re.search(r"app answered at ([0-9.]+) s", err)
    assert m, err
    learned = json.loads((tmp_path / "state" / "boot-splash.json").read_text())["boot_s"]
    assert 20 <= learned == round(float(m.group(1)), 1) < 22


def test_gives_up_by_itself_without_learning(tmp_path):
    sys_dir, fb = _fake_fb(tmp_path, 320, 240, 16, 640)
    env = _env(tmp_path, sys_dir, fb, SPLASH_GIVE_UP=0.5)
    proc = _start(env)
    _, err = proc.communicate(timeout=10)
    assert proc.returncode == 0 and "gave up" in err
    assert not (tmp_path / "state" / "boot-splash.json").exists()


def test_sigterm_ends_it_cleanly(tmp_path):
    sys_dir, fb = _fake_fb(tmp_path, 320, 240, 32, 1280)
    proc = _start(_env(tmp_path, sys_dir, fb))
    _wait_painted(fb, proc)
    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=5)
    assert proc.returncode == 0 and "stopped" in err


def _vt_env(tmp_path, sys_dir, fb, unit_text, active="tty1"):
    unit = tmp_path / "kiosk.service"
    unit.write_text(unit_text)
    chvt = tmp_path / "chvt"
    chvt.write_text("")
    act = tmp_path / "active"
    act.write_text(active + "\n")
    env = _env(tmp_path, sys_dir, fb, SPLASH_CHVT=chvt, SPLASH_VT_ACTIVE=act,
               SPLASH_TTY=tmp_path / "no-tty")
    env.pop("SPLASH_VT")
    return env


@pytest.mark.parametrize("unit_text,active,why", [
    ("[Service]\nExecStart=/usr/bin/cage\n", "tty1", "does not switch"),
    ("[Service]\n" + CHVT_LINE + "\n", "tty7", "not on screen"),
])
def test_refuses_and_leaves_the_screen_alone(tmp_path, unit_text, active, why):
    sys_dir, fb = _fake_fb(tmp_path, 320, 240, 16, 640)
    env = _vt_env(tmp_path, sys_dir, fb, unit_text, active)
    proc = subprocess.run([sys.executable, str(WRITER)], env=env,
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0 and why in proc.stderr
    assert fb.read_bytes() == b"\0" * (640 * 240)


def test_chvt_found_in_a_kiosk_dropin(tmp_path):
    unit = tmp_path / "kiosk.service"
    unit.write_text("[Service]\nTTYPath=/dev/tty1\n")
    d = tmp_path / "kiosk.service.d"
    d.mkdir()
    (d / "30-foodassistant-vt.conf").write_text("[Service]\n" + CHVT_LINE + "\n")
    assert splash.kiosk_switches_vt(splash.kiosk_text(str(unit)))


def test_waits_for_the_display_driver_framebuffer(tmp_path):
    # The firmware's simple framebuffer is replaced when the DRM driver binds;
    # the splash must open the driver's one, not the one about to vanish.
    sys_dir, fb = _fake_fb(tmp_path, 8, 4, 16, 16)
    (sys_dir / "name").write_text("simple\n")

    def swap():
        time.sleep(0.4)
        (sys_dir / "name").write_text("vc4drmfb\n")

    t = threading.Thread(target=swap)
    t.start()
    t0 = time.monotonic()
    opened = splash.open_framebuffer(str(fb), str(sys_dir), 5, 5)
    t.join()
    assert opened is not None
    os.close(opened[0])
    assert time.monotonic() - t0 >= 0.35


def test_uses_a_simple_framebuffer_when_no_driver_takes_over(tmp_path):
    sys_dir, fb = _fake_fb(tmp_path, 8, 4, 16, 16)
    (sys_dir / "name").write_text("simple\n")
    opened = splash.open_framebuffer(str(fb), str(sys_dir), 5, 0.2)
    assert opened is not None
    os.close(opened[0])


def test_retries_a_framebuffer_that_appears_late(tmp_path):
    sys_dir, fb = _fake_fb(tmp_path, 8, 4, 16, 16)
    data = fb.read_bytes()
    fb.unlink()

    def appear():
        time.sleep(0.3)
        fb.write_bytes(data)

    t = threading.Thread(target=appear)
    t.start()
    opened = splash.open_framebuffer(str(fb), str(sys_dir), 5, 0)
    t.join()
    assert opened is not None
    os.close(opened[0])
    assert splash.open_framebuffer(str(tmp_path / "never"), str(sys_dir), 0.2, 0) is None


def test_every_failure_is_a_quiet_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("SPLASH_VT", "off")
    monkeypatch.setenv("SPLASH_FB_WAIT", "0")
    monkeypatch.setenv("SPLASH_ASSET", str(tmp_path / "missing.gz"))
    monkeypatch.setenv("SPLASH_FB", str(tmp_path / "missing-fb"))
    assert splash.main() == 0
    # An unsupported depth: still exit 0 and the framebuffer untouched.
    sys_dir, fb = _fake_fb(tmp_path, 4, 4, 24, 12)
    monkeypatch.setenv("SPLASH_ASSET", str(ASSET))
    monkeypatch.setenv("SPLASH_FB", str(fb))
    monkeypatch.setenv("SPLASH_FB_SYS", str(sys_dir))
    assert splash.main() == 0
    assert fb.read_bytes() == b"\0" * 48
    # Corrupt art.
    bad = tmp_path / "bad.gz"
    with gzip.open(bad, "wb") as f:
        f.write(b"P6\n1 1\n255\n\0\0\0")
    monkeypatch.setenv("SPLASH_ASSET", str(bad))
    assert splash.main() == 0


# -- the art --------------------------------------------------------------------

def test_checked_in_art_covers_the_common_panels():
    gen = _load("fa_make_boot_splash", GENERATOR)
    assert sorted(ART) == sorted(gen.UNITS)
    for unit, sprites in ART.items():
        for name in ("mark", "word"):
            w, h, idx, pal = sprites[name]
            assert pal[0] == BG
            assert len(idx) == w * h and idx.count(0) < len(idx)
        assert sprites["mark"][1] == round(unit * splash.MARK_FRAC)
    assert gen.MARK_FRAC == splash.MARK_FRAC and gen.WORD_FRAC == splash.WORD_FRAC
    assert tuple(gen.BG) == BG


def test_builder_output_loads_and_is_deterministic(tmp_path):
    gen = _load("fa_make_boot_splash", GENERATOR)
    try:
        font = gen.find_font()
    except SystemExit:
        pytest.skip("no DejaVu Sans Bold on this machine")
    data = gen.build(font_path=font, units=(240,))
    out, again = tmp_path / "a.gz", tmp_path / "b.gz"
    gen.write_gz(data, str(out))
    gen.write_gz(gen.build(font_path=font, units=(240,)), str(again))
    assert out.read_bytes() == again.read_bytes()
    sets = splash.load_art(str(out))
    assert list(sets) == [240]
    w, h, idx, pal = sets[240]["mark"]
    assert h == round(240 * gen.MARK_FRAC)
    assert max(idx) > 100  # carries the mark's solid lines


def test_writer_is_executable_and_stdlib_only():
    assert os.access(WRITER, os.X_OK)
    text = WRITER.read_text()
    assert text.startswith("#!/usr/bin/env python3")
    for banned in ("PIL", "numpy", "import requests", "httpx", "urllib.request"):
        assert banned not in text


def test_a_console_without_vt_ioctls_leaves_the_screen_alone(tmp_path):
    # A terminal that is not a VT (here a pty) fails VT_GETMODE: the boot
    # screen ends quietly before painting anything.
    master, slave = os.openpty()
    try:
        sys_dir, fb = _fake_fb(tmp_path, 320, 240, 16, 640)
        env = _vt_env(tmp_path, sys_dir, fb, "[Service]\n" + CHVT_LINE + "\n")
        env["SPLASH_TTY"] = os.ttyname(slave)
        proc = subprocess.run([sys.executable, str(WRITER)], env=env,
                              capture_output=True, text=True, timeout=10)
    finally:
        os.close(master)
        os.close(slave)
    assert proc.returncode == 0 and "skipped (OSError" in proc.stderr
    assert fb.read_bytes() == b"\0" * (640 * 240)
