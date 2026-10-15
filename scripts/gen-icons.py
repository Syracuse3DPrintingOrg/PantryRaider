#!/usr/bin/env python3
"""Generate every icon derivative from the master Pantry Raider logo.

Source: service/app/static/icons/logo.png (transparent line-art raccoon, brand
pink #F2006E). Run after replacing the master to regenerate all favicons, PWA
icons, apple-touch icon, and the SVG favicon wrapper. Pure Pillow, no external
binaries.

    python scripts/gen-icons.py
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image

ICONS = Path(__file__).resolve().parents[1] / "service/app/static/icons"
SRC = ICONS / "logo.png"
# Maskable/app-icon background. White is the logo's native context and keeps the
# pink line-art crisp at small sizes on every launcher.
BG = (255, 255, 255, 255)


def _content(img: Image.Image) -> Image.Image:
    """Tight-crop to the non-transparent content so every icon is centred and
    sized consistently regardless of the master's own margins."""
    img = img.convert("RGBA")
    bbox = img.getbbox()
    return img.crop(bbox) if bbox else img


def _fit(content: Image.Image, size: int, frac: float) -> Image.Image:
    """Scale content to occupy `frac` of a size x size box, preserving aspect."""
    w, h = content.size
    scale = (size * frac) / max(w, h)
    return content.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                          Image.LANCZOS)


def _canvas(content: Image.Image, size: int, frac: float, bg) -> Image.Image:
    fitted = _fit(content, size, frac)
    canvas = Image.new("RGBA", (size, size), bg)
    x = (size - fitted.width) // 2
    y = (size - fitted.height) // 2
    canvas.alpha_composite(fitted, (x, y))
    return canvas


def _save_png(img: Image.Image, path: Path, opaque: bool) -> None:
    """Save a palette-quantized, optimised PNG. The logo is only a couple of
    hues, so quantising to a small palette shrinks the files ~5-7x with no
    visible change and keeps pushes small. Opaque icons drop the alpha channel."""
    if opaque:
        img = img.convert("RGB").quantize(colors=128, method=Image.FASTOCTREE)
    else:
        img = img.quantize(colors=128, method=Image.FASTOCTREE)
    img.save(path, optimize=True)


def main() -> None:
    content = _content(Image.open(SRC))

    # Re-save the master itself quantised so the repo (and every push) stays
    # small; the line art is visually identical at a fraction of the size.
    _save_png(Image.open(SRC).convert("RGBA"), SRC, opaque=False)

    # Transparent mark for the navbar/site header and the ghosted background,
    # crisp on hi-dpi without shipping the full-res master on every page.
    _save_png(_canvas(content, 128, 1.0, (0, 0, 0, 0)), ICONS / "logo-mark.png", opaque=False)

    # Favicons: transparent, near-full-bleed so the mark reads at 16px.
    for size in (16, 32):
        _save_png(_canvas(content, size, 0.92, (0, 0, 0, 0)), ICONS / f"favicon-{size}.png", opaque=False)
    # Multi-resolution .ico from a 48px transparent master.
    ico = _canvas(content, 48, 0.92, (0, 0, 0, 0))
    ico.save(ICONS / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])

    # PWA icons are "any maskable": filled background + content inside the ~80%
    # safe circle so a circular/squircle mask never clips the raccoon.
    for size in (192, 512):
        _save_png(_canvas(content, size, 0.66, BG), ICONS / f"icon-{size}.png", opaque=True)
    # apple-touch: iOS ignores transparency, so a white tile with padding.
    _save_png(_canvas(content, 180, 0.74, BG), ICONS / "apple-touch-icon.png", opaque=True)

    # SVG favicon: wrap a small transparent PNG so the <link type=image/svg+xml>
    # keeps working and scales crisply on the tab without shipping the 190KB master.
    png = _canvas(content, 64, 0.92, (0, 0, 0, 0))
    buf = io.BytesIO()
    png.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    (ICONS / "favicon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" '
        'viewBox="0 0 64 64" role="img" aria-label="Pantry Raider">'
        f'<image width="64" height="64" href="data:image/png;base64,{b64}"/></svg>\n'
    )

    for f in sorted(ICONS.glob("*")):
        print(f"  {f.name}: {f.stat().st_size} bytes")


if __name__ == "__main__":
    main()
