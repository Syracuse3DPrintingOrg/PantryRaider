"""Key image rendering with Pillow.

Draws a single key face: a coloured background, a label, and an optional large
count for status keys. Returns a plain PIL image at the requested size; the
controller converts it to the deck's native format. Kept free of any hardware
import so it can be exercised in tests.
"""
from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


@lru_cache(maxsize=16)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    if len(v) != 6:
        return (60, 60, 60)
    return tuple(int(v[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def render_key(
    width: int,
    height: int,
    label: str,
    color: str,
    count: int | None = None,
    alert: bool = False,
) -> Image.Image:
    """Render one key.

    ``count`` shows a large number for status keys; when it is greater than
    zero and ``alert`` is set the background is brightened so a full fridge or
    a backlog of scans is visible across the room.
    """
    bg = _hex_to_rgb(color)
    if count and alert:
        bg = tuple(min(255, int(c * 1.35) + 25) for c in bg)  # type: ignore[assignment]

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    if count is not None:
        num = str(count)
        num_font = _font(max(18, int(height * 0.42)))
        nw = _text_width(draw, num, num_font)
        draw.text(
            ((width - nw) / 2, height * 0.08),
            num,
            font=num_font,
            fill=(255, 255, 255),
        )
        label_y = height * 0.62
        label_font = _font(max(11, int(height * 0.18)))
    else:
        label_y = (height - max(13, int(height * 0.2))) / 2
        label_font = _font(max(13, int(height * 0.2)))

    lw = _text_width(draw, label, label_font)
    draw.text(
        ((width - lw) / 2, label_y),
        label,
        font=label_font,
        fill=(235, 235, 235),
    )
    return img


def blank_key(width: int, height: int) -> Image.Image:
    return Image.new("RGB", (width, height), (16, 18, 22))
