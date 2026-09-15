"""Framebuffer boot splash (FoodAssistant-y8vj).

Covers the three shipped pieces without a device: the on-device writer
(scripts/image-build/foodassistant-boot-splash, stdlib-only Python) is
exercised as a module against a fake sysfs and a plain file standing in for
/dev/fb0; the build-time generator (make-boot-splash.py) must produce a PPM
the writer parses back; and the checked-in asset plus the systemd unit are
content-pinned so a regression cannot ship silently.

Run: python -m pytest tests/test_boot_splash.py -q
"""
from __future__ import annotations

import gzip
import importlib.machinery
import importlib.util
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IMAGE_BUILD = REPO / "scripts" / "image-build"
WRITER = IMAGE_BUILD / "foodassistant-boot-splash"
GENERATOR = IMAGE_BUILD / "make-boot-splash.py"
UNIT = IMAGE_BUILD / "foodassistant-boot-splash.service"
ASSET = IMAGE_BUILD / "boot-splash" / "splash.ppm.gz"

BG = (33, 37, 41)
PINK = (251, 9, 120)


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


splash = _load("fa_boot_splash", WRITER)


def _ppm_bytes(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    return (b"P6\n%d %d\n255\n" % (width, height)) + bytes(rgb) * (width * height)


def _write_asset(path: Path, width: int, height: int, rgb=PINK) -> None:
    with gzip.open(path, "wb") as f:
        f.write(_ppm_bytes(width, height, rgb))


def _fake_fb(tmp_path: Path, width: int, height: int, bpp: int, stride: int):
    """A sysfs stand-in plus an empty regular file as the framebuffer."""
    sys_dir = tmp_path / "fb-sys"
    sys_dir.mkdir()
    (sys_dir / "virtual_size").write_text(f"{width},{height}\n")
    (sys_dir / "bits_per_pixel").write_text(f"{bpp}\n")
    (sys_dir / "stride").write_text(f"{stride}\n")
    fb = tmp_path / "fb0"
    fb.write_bytes(b"")
    return sys_dir, fb


# -- PPM parsing --------------------------------------------------------------

def test_parse_ppm_roundtrip():
    w, h, rgb = splash.parse_ppm(_ppm_bytes(4, 2, PINK))
    assert (w, h) == (4, 2)
    assert rgb == bytes(PINK) * 8


def test_parse_ppm_accepts_comments():
    data = b"P6\n# a comment\n4 2\n255\n" + bytes(PINK) * 8
    w, h, rgb = splash.parse_ppm(data)
    assert (w, h) == (4, 2)
    assert len(rgb) == 24


def test_parse_ppm_rejects_bad_input():
    import pytest
    with pytest.raises(ValueError):
        splash.parse_ppm(b"P5\n4 2\n255\n" + b"\x00" * 8)  # not P6
    with pytest.raises(ValueError):
        splash.parse_ppm(_ppm_bytes(4, 2, PINK)[:-1])  # truncated raster


# -- pixel packing ------------------------------------------------------------

def test_rgb565_known_values_and_length():
    # Little-endian RGB565: full red 0xF800, full green 0x07E0, full blue
    # 0x001F, white 0xFFFF.
    assert splash.to_rgb565(bytes((255, 0, 0))) == b"\x00\xf8"
    assert splash.to_rgb565(bytes((0, 255, 0))) == b"\xe0\x07"
    assert splash.to_rgb565(bytes((0, 0, 255))) == b"\x1f\x00"
    assert splash.to_rgb565(bytes((255, 255, 255))) == b"\xff\xff"
    # A full 800x480 frame packs to exactly 2 bytes per pixel.
    frame = bytes(PINK) * (800 * 480)
    assert len(splash.to_rgb565(frame)) == 800 * 480 * 2


def test_xrgb8888_layout_and_length():
    out = splash.to_xrgb8888(bytes((10, 20, 30)))
    assert out == bytes((30, 20, 10, 255))  # B, G, R, X
    frame = bytes(PINK) * (800 * 480)
    assert len(splash.to_xrgb8888(frame)) == 800 * 480 * 4


def test_bgr888_layout():
    assert splash.to_bgr888(bytes((10, 20, 30))) == bytes((30, 20, 10))


def test_pack_pixels_rejects_odd_depths():
    import pytest
    with pytest.raises(ValueError):
        splash.pack_pixels(bytes(PINK), 8)


# -- composition helpers ------------------------------------------------------

def test_center_canvas_centers_smaller_image():
    rgb = bytes(PINK) * (2 * 2)
    canvas = splash.center_canvas(rgb, 2, 2, 6, 4)
    assert len(canvas) == 6 * 4 * 3

    def px(x, y):
        return tuple(canvas[(y * 6 + x) * 3:(y * 6 + x) * 3 + 3])

    assert px(0, 0) == BG and px(5, 3) == BG
    assert px(2, 1) == PINK and px(3, 2) == PINK
    assert px(1, 1) == BG and px(4, 2) == BG


def test_center_canvas_crops_larger_image():
    # 4x4 image onto a 2x2 framebuffer: the middle survives.
    rows = []
    for y in range(4):
        for x in range(4):
            rows.append(bytes(PINK) if (1 <= x <= 2 and 1 <= y <= 2) else bytes(BG))
    canvas = splash.center_canvas(b"".join(rows), 4, 4, 2, 2)
    assert canvas == bytes(PINK) * 4


def test_fade_endpoints():
    rgb = bytes(PINK) * 10
    assert splash.fade_toward_bg(rgb, 1.0) == rgb
    assert splash.fade_toward_bg(rgb, 0.0) == bytes(BG) * 10
    mid = splash.fade_toward_bg(rgb, 0.5)
    assert mid != rgb and mid != bytes(BG) * 10


def test_pad_rows_inserts_per_row_padding():
    pixels = bytes(range(8))  # 2 rows of 2 px at 2 bytes/px
    out = splash.pad_rows(pixels, 2, 2, 2, 6)
    assert out == bytes((0, 1, 2, 3, 0, 0, 4, 5, 6, 7, 0, 0))
    # stride == row is a no-op passthrough
    assert splash.pad_rows(pixels, 2, 2, 2, 4) is pixels


# -- painting end to end ------------------------------------------------------

def test_paint_writes_full_16bpp_frame_with_stride(tmp_path):
    asset = tmp_path / "splash.ppm.gz"
    _write_asset(asset, 4, 2)
    sys_dir, fb = _fake_fb(tmp_path, 8, 4, 16, 20)  # row 16 bytes + 4 padding
    splash.paint(str(asset), str(fb), str(sys_dir), delay=0)
    data = fb.read_bytes()
    assert len(data) == 20 * 4  # stride * height
    pink565 = splash.to_rgb565(bytes(PINK))
    bg565 = splash.to_rgb565(bytes(BG))

    def px(x, y):
        return data[y * 20 + x * 2:y * 20 + x * 2 + 2]

    assert px(0, 0) == bg565 and px(7, 3) == bg565
    assert px(2, 1) == pink565 and px(5, 2) == pink565


def test_paint_32bpp(tmp_path):
    asset = tmp_path / "splash.ppm.gz"
    _write_asset(asset, 2, 2)
    sys_dir, fb = _fake_fb(tmp_path, 2, 2, 32, 8)
    splash.paint(str(asset), str(fb), str(sys_dir), delay=0)
    data = fb.read_bytes()
    assert len(data) == 8 * 2
    assert data[:4] == bytes((PINK[2], PINK[1], PINK[0], 255))


def test_main_swallows_every_failure(tmp_path, monkeypatch):
    # No framebuffer, no asset: exit code 0, nothing raised.
    monkeypatch.setenv("SPLASH_ASSET", str(tmp_path / "missing.ppm.gz"))
    monkeypatch.setenv("SPLASH_FB", str(tmp_path / "missing-fb"))
    monkeypatch.setenv("SPLASH_FB_SYS", str(tmp_path / "missing-sys"))
    assert splash.main() == 0
    # Unsupported depth: still exit 0 and leave the framebuffer untouched.
    asset = tmp_path / "splash.ppm.gz"
    _write_asset(asset, 2, 2)
    sys_dir, fb = _fake_fb(tmp_path, 2, 2, 8, 2)
    monkeypatch.setenv("SPLASH_ASSET", str(asset))
    monkeypatch.setenv("SPLASH_FB", str(fb))
    monkeypatch.setenv("SPLASH_FB_SYS", str(sys_dir))
    assert splash.main() == 0
    assert fb.read_bytes() == b""


# -- the build-time generator and the checked-in asset ------------------------

def test_generator_output_parses_and_carries_the_mark(tmp_path):
    gen = _load("fa_make_boot_splash", GENERATOR)
    out = tmp_path / "splash.ppm.gz"
    image = gen.compose_splash(str(REPO / "service/app/static/icons/logo.png"),
                               size=(160, 96))
    gen.write_ppm_gz(image, str(out))
    with gzip.open(out, "rb") as f:
        w, h, rgb = splash.parse_ppm(f.read())
    assert (w, h) == (160, 96)
    assert len(rgb) == 160 * 96 * 3
    assert tuple(rgb[0:3]) == BG  # corner is the dark background
    assert rgb.count(bytes(BG)) < len(rgb) // 3  # and the mark is on it
    # Deterministic: a rerun produces byte-identical output (mtime=0), so the
    # updater's cmp -s guard does not thrash.
    again = tmp_path / "again.ppm.gz"
    gen.write_ppm_gz(image, str(again))
    assert out.read_bytes() == again.read_bytes()


def test_checked_in_asset_is_the_expected_frame():
    with gzip.open(ASSET, "rb") as f:
        w, h, rgb = splash.parse_ppm(f.read())
    assert (w, h) == (800, 480)
    assert len(rgb) == 800 * 480 * 3
    assert tuple(rgb[0:3]) == BG
    # Its RGB565 packing is exactly the raw frame size the bead asked for.
    assert len(splash.to_rgb565(rgb)) == 800 * 480 * 2


# -- the systemd unit ---------------------------------------------------------

def test_unit_is_a_failsafe_oneshot():
    """Content pin: the unit must never be able to hang or fail a boot. Every
    prerequisite is a Condition (a skip, not a failure), it is a bounded
    oneshot, and it defers to the opt-in Plymouth splash."""
    text = UNIT.read_text()
    assert "Type=oneshot" in text
    assert "DefaultDependencies=no" in text
    assert "After=local-fs.target" in text
    assert "ConditionPathExists=/dev/fb0" in text
    assert "ConditionPathExists=/opt/foodassistant/boot-splash/splash.ppm.gz" in text
    assert "ConditionPathExists=/usr/local/bin/foodassistant-boot-splash" in text
    assert "ConditionKernelCommandLine=!splash" in text
    assert "ExecStart=/usr/local/bin/foodassistant-boot-splash" in text
    assert "TimeoutStartSec=" in text
    assert "WantedBy=sysinit.target" in text
    # Nothing may wait on the splash: it must not order itself before boot
    # targets (shutdown ordering is the one conventional exception).
    for line in text.splitlines():
        if line.startswith("Before="):
            assert line == "Before=shutdown.target"


def test_unit_parses_with_systemd_analyze_when_available(tmp_path):
    import shutil
    import subprocess

    import pytest
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze not available")
    dst = tmp_path / UNIT.name
    dst.write_text(UNIT.read_text())
    proc = subprocess.run(
        ["systemd-analyze", "verify", str(dst)],
        capture_output=True, text=True,
    )
    # On a dev machine the writer is not installed at /usr/local/bin, so
    # verify complains about (only) that; any other complaint is a real
    # problem with the unit itself.
    out = proc.stdout + proc.stderr
    problems = [
        line for line in out.splitlines()
        if line.strip() and "is not executable" not in line
    ]
    assert proc.returncode == 0 or problems == [], out


def test_writer_is_executable_and_stdlib_only():
    assert os.access(WRITER, os.X_OK)
    text = WRITER.read_text()
    assert text.startswith("#!/usr/bin/env python3")
    for banned in ("PIL", "numpy", "import requests", "httpx"):
        assert banned not in text
