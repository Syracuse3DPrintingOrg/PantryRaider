#!/usr/bin/env python3
"""Render the boot splash asset (FoodAssistant-y8vj).

Composes the Pantry Raider mark centered on the app's dark background
(#212529) and writes it as a gzipped binary PPM (P6, maxval 255), the format
the on-device writer (foodassistant-boot-splash) parses with the Python
standard library alone. Run at BUILD time on a dev machine (needs Pillow);
the output, scripts/image-build/boot-splash/splash.ppm.gz, is checked into
the repo so provisioning and OTA updates ship it without any image tooling
on the device.

Regenerate after a logo change:
  python3 scripts/image-build/make-boot-splash.py

The gzip stream is written with mtime=0 so an unchanged rerun produces
byte-identical output (the updater's cmp -s guards rely on that).
"""
import argparse
import gzip
import io
import os

from PIL import Image

# Matches BG in foodassistant-boot-splash and the kiosk browser's
# --default-background-color.
BG = (33, 37, 41)
# The official Pi touch panel resolution; the on-device writer centers or
# crops this for whatever panel is actually attached.
DEFAULT_SIZE = (800, 480)
# The mark occupies this fraction of the splash height.
LOGO_FRACTION = 0.5

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
DEFAULT_LOGO = os.path.join(REPO_ROOT, "service", "app", "static", "icons", "logo.png")
DEFAULT_OUT = os.path.join(SCRIPT_DIR, "boot-splash", "splash.ppm.gz")

_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def compose_splash(logo_path, size=DEFAULT_SIZE, bg=BG, logo_fraction=LOGO_FRACTION):
    """The splash frame: the mark centered on the dark background."""
    logo = Image.open(logo_path).convert("RGBA")
    box = logo.getchannel("A").getbbox()
    if box:
        logo = logo.crop(box)
    target_h = max(1, round(size[1] * logo_fraction))
    target_w = max(1, round(logo.width * target_h / logo.height))
    if target_w > size[0]:
        target_w = size[0]
        target_h = max(1, round(logo.height * target_w / logo.width))
    logo = logo.resize((target_w, target_h), _LANCZOS)
    canvas = Image.new("RGB", size, bg)
    canvas.paste(logo, ((size[0] - target_w) // 2, (size[1] - target_h) // 2), logo)
    return canvas


def write_ppm_gz(image, out_path):
    """Write an RGB image as a deterministic gzipped binary PPM."""
    buf = io.BytesIO()
    image.save(buf, format="PPM")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        # filename="" keeps the gzip header free of the output path, so the
        # bytes depend only on the image content.
        with gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0) as gz:
            gz.write(buf.getvalue())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--logo", default=DEFAULT_LOGO, help="source logo PNG")
    parser.add_argument("--out", default=DEFAULT_OUT, help="output .ppm.gz path")
    parser.add_argument(
        "--size", default="%dx%d" % DEFAULT_SIZE,
        help="splash resolution as WIDTHxHEIGHT (default %(default)s)",
    )
    args = parser.parse_args(argv)
    width, height = (int(v) for v in args.size.lower().split("x"))
    image = compose_splash(args.logo, (width, height))
    write_ppm_gz(image, args.out)
    print("wrote %s (%dx%d)" % (args.out, width, height))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
