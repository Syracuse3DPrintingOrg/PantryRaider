#!/usr/bin/env python3
"""Render the boot screen artwork (FoodAssistant-y8vj).

The on-device boot screen (foodassistant-boot-splash) is Python standard
library only, so it cannot scale a logo or draw text. This builder does that
work on a dev machine with Pillow and writes the results into one gzip:

- the Pantry Raider mark and the wordmark, each prebuilt at several common
  panel sizes (keyed by the short side of the landscape screen);
- each as an 8-bit index image plus a 256-colour palette, so the device turns
  a whole sprite into packed framebuffer pixels with one bytes.translate()
  per output byte.

The mark's palette is two ramps: indices 0..127 run from the background to
the mark's pink (the anti-aliased outer edges), 128..255 from the pink to the
mark's light fill (the eyes). The wordmark's palette is one ramp from the
background to the text colour. Index 0 is always exactly the background.

Run at BUILD time (needs Pillow); the output,
scripts/image-build/boot-splash/splash-art.gz, is checked in so provisioning
and OTA updates ship it without any image tooling on the device. Regenerate
after a logo change:

  python3 scripts/image-build/make-boot-splash.py

The gzip stream is written with mtime=0 and no file name, so an unchanged
rerun is byte-identical (the updater's cmp -s guards rely on that).

Container layout (all integers little-endian):
  b"PRSPLASH2\\n", u32 header length, header JSON (UTF-8), sprite bytes.
The header lists every size set with its sprites' width, height, offset into
the sprite bytes and palette (768 hex digits per 256 colours).
"""
import argparse
import gzip
import io
import json
import os
import struct

from PIL import Image, ImageDraw, ImageFont

MAGIC = b"PRSPLASH2\n"
# The app's dark background, matching the kiosk browser's
# --default-background-color, so the hand-off shows no colour jump.
BG = (33, 37, 41)
# Wordmark colour (Bootstrap gray-300, the app's light text).
TEXT = (222, 226, 230)
WORDMARK = "Pantry Raider"
# Short side of the landscape screen each set is built for. The device picks
# the largest one that fits, so these cover the official 7 inch panel
# (800x480), Touch Display 2 (720x1280 turned), 1024x600, 1280x800 and the
# common HDMI sizes.
UNITS = (320, 480, 600, 720, 800, 1080, 1200)
# Fractions of the short side. The device lays the screen out with the same
# numbers (MARK_FRAC and WORD_FRAC in foodassistant-boot-splash).
MARK_FRAC = 0.42
WORD_FRAC = 0.068

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
DEFAULT_LOGO = os.path.join(REPO_ROOT, "service", "app", "static", "icons", "logo.png")
DEFAULT_OUT = os.path.join(SCRIPT_DIR, "boot-splash", "splash-art.gz")
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/DejaVuSans-Bold.ttf",
)

_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def _mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def mark_palette(pink, light, bg=BG):
    """Two ramps: bg to pink (0..127), pink to light (128..255)."""
    pal = [_mix(bg, pink, i / 127) for i in range(128)]
    pal += [_mix(pink, light, i / 127) for i in range(128)]
    return pal


def text_palette(color=TEXT, bg=BG):
    return [_mix(bg, color, i / 255) for i in range(256)]


def mark_colors(logo):
    """The mark's line colour (its most common opaque colour) and its light
    fill colour (the lightest opaque one)."""
    counts = {}
    data = logo.tobytes()
    for i in range(0, len(data), 4):
        r, g, b, a = data[i:i + 4]
        if a >= 250:
            counts[(r, g, b)] = counts.get((r, g, b), 0) + 1
    pink = max(counts, key=counts.get)
    light = max(counts, key=lambda c: (sum(c), counts[c]))
    return pink, light


def load_logo(path):
    logo = Image.open(path).convert("RGBA")
    box = logo.getchannel("A").getbbox()
    return logo.crop(box) if box else logo


def mark_indices(logo, height, pink, light):
    """The mark resized to `height` as palette indices (mark_palette)."""
    width = max(1, round(logo.width * height / logo.height))
    img = logo.resize((width, height), _LANCZOS)
    pr, pg, pb = pink
    dl = (light[0] - pr, light[1] - pg, light[2] - pb)
    dl2 = float(sum(v * v for v in dl)) or 1.0
    out = bytearray(width * height)
    data = img.tobytes()
    for i in range(width * height):
        r, g, b, a = data[4 * i:4 * i + 4]
        if a == 0:
            continue
        # How far this pixel's own colour sits from the pink toward the light
        # fill. Light pixels sit inside the pink outline (fully covered), so
        # they take the second ramp; everything else is pink coverage over
        # the background.
        w = ((r - pr) * dl[0] + (g - pg) * dl[1] + (b - pb) * dl[2]) / dl2
        if a >= 192 and w > 0.5 / 127:
            out[i] = 128 + min(127, round(w * 127))
        else:
            out[i] = min(127, round(a / 255 * 127))
    return width, height, bytes(out)


def find_font(path=None):
    for p in ((path,) if path else FONT_CANDIDATES):
        if p and os.path.isfile(p):
            return p
    raise SystemExit("no wordmark font found; pass --font /path/to/DejaVuSans-Bold.ttf")


def text_indices(font_path, size, text=WORDMARK):
    """The wordmark as 8-bit coverage, cropped to its ink."""
    font = ImageFont.truetype(font_path, size)
    left, top, right, bottom = font.getbbox(text)
    img = Image.new("L", (right - left + 4, bottom - top + 4), 0)
    ImageDraw.Draw(img).text((2 - left, 2 - top), text, font=font, fill=255)
    box = img.getbbox()
    if box:
        img = img.crop(box)
    return img.width, img.height, img.tobytes()


def build(logo_path=DEFAULT_LOGO, font_path=None, units=UNITS):
    """The container bytes (uncompressed)."""
    logo = load_logo(logo_path)
    pink, light = mark_colors(logo)
    font = find_font(font_path)
    mpal = "".join("%02x%02x%02x" % c for c in mark_palette(pink, light))
    tpal = "".join("%02x%02x%02x" % c for c in text_palette())
    blob = bytearray()
    sets = []
    for unit in units:
        sprites = {}
        mw, mh, mdata = mark_indices(logo, max(8, round(unit * MARK_FRAC)), pink, light)
        sprites["mark"] = {"w": mw, "h": mh, "off": len(blob), "palette": mpal}
        blob += mdata
        tw, th, tdata = text_indices(font, max(8, round(unit * WORD_FRAC)))
        sprites["word"] = {"w": tw, "h": th, "off": len(blob), "palette": tpal}
        blob += tdata
        sets.append({"unit": unit, "sprites": sprites})
    header = json.dumps({"bg": list(BG), "sets": sets}, sort_keys=True,
                        separators=(",", ":")).encode()
    return MAGIC + struct.pack("<I", len(header)) + header + bytes(blob)


def write_gz(data, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    buf = io.BytesIO()
    # filename="" and mtime=0 keep the bytes dependent on the content alone.
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0,
                       compresslevel=9) as gz:
        gz.write(data)
    with open(out_path, "wb") as f:
        f.write(buf.getvalue())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--logo", default=DEFAULT_LOGO, help="source logo PNG")
    parser.add_argument("--font", default=None, help="wordmark TTF (DejaVu Sans Bold)")
    parser.add_argument("--out", default=DEFAULT_OUT, help="output .gz path")
    args = parser.parse_args(argv)
    data = build(args.logo, args.font)
    write_gz(data, args.out)
    print("wrote %s (%d bytes before gzip, %d sets)" % (args.out, len(data), len(UNITS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
