"""A small Code 128 encoder for printed labels.

Pure Python, no dependency: labels carry a barcode on every install without
an extra package. It covers printable ASCII (code set B) and switches to code
set C for runs of digits, which packs two digits into each symbol and keeps a
long product code short enough to print on a small label.

``symbols(text)`` gives the symbol values (start code, data, checksum, stop),
``modules(text)`` the bar and space widths in modules, starting with a bar,
and ``render(text, box_w, box_h)`` a black-on-white PIL image that fits the
box with whole-pixel modules and a quiet zone on each side. Each returns None
when the text cannot be encoded or will not fit, so a caller can fall back to
printing the text.
"""
from __future__ import annotations

# Bar and space widths for symbol values 0 to 106, in modules. 103 to 105 are
# the start codes for sets A, B and C; 106 is the stop pattern (seven elements,
# the last bar being the termination bar).
PATTERNS = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 "
    "221312 231212 112232 122132 122231 113222 123122 123221 223211 221132 "
    "221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 "
    "212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 "
    "231113 231311 112133 112331 132131 113123 113321 133121 313121 211331 "
    "231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 "
    "314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 "
    "112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
    "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 "
    "214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 "
    "114131 311141 411131 211412 211214 211232 2331112"
).split()

START_B = 104
START_C = 105
CODE_B = 100  # switch to set B (as read in set A or C)
CODE_C = 99   # switch to set C (as read in set A or B)
STOP = 106
QUIET_MODULES = 10


def _digit_run(text: str, i: int) -> int:
    j = i
    while j < len(text) and text[j].isdigit():
        j += 1
    return j - i


def symbols(text: str) -> "list[int] | None":
    """Symbol values for ``text``, from start code to stop, or None when the
    text is empty or holds a character outside printable ASCII."""
    if not text or any(not (32 <= ord(c) <= 126) for c in text):
        return None
    out: list[int] = []
    code = None
    i, n = 0, len(text)
    while i < n:
        run = _digit_run(text, i)
        at_start, at_end = i == 0, i + run == n
        # Set C pays off for a run of four digits at either end, six in the
        # middle (two symbols go on switching there and back), or a code that
        # is nothing but digits.
        need = 2 if (at_start and at_end) else 4 if (at_start or at_end) else 6
        if run >= need:
            if code is None:
                out.append(START_C)
            elif code != "C":
                out.append(CODE_C)
            code = "C"
            end = i + run - run % 2
            while i < end:
                out.append(int(text[i:i + 2]))
                i += 2
            continue
        if code is None:
            out.append(START_B)
        elif code != "B":
            out.append(CODE_B)
        code = "B"
        out.append(ord(text[i]) - 32)
        i += 1
    check = out[0] + sum(pos * v for pos, v in enumerate(out[1:], 1))
    out.append(check % 103)
    out.append(STOP)
    return out


def modules(text: str) -> "list[int] | None":
    """Alternating bar and space widths in modules, starting with a bar."""
    syms = symbols(text)
    if syms is None:
        return None
    return [int(w) for s in syms for w in PATTERNS[s]]


def render(text: str, box_w: int, box_h: int):
    """A barcode image no wider than ``box_w`` and ``box_h`` tall, or None.

    Every module is the same whole number of pixels: stretching the image to
    the box would make some bars a pixel wider than others, which a scanner
    can misread. None when the code does not fit at one pixel per module."""
    widths = modules(text)
    if widths is None or box_h < 1:
        return None
    total = sum(widths) + 2 * QUIET_MODULES
    px = box_w // total
    if px < 1:
        return None
    from PIL import Image, ImageDraw

    img = Image.new("L", (total * px, box_h), 255)
    draw = ImageDraw.Draw(img)
    x = QUIET_MODULES * px
    for i, w in enumerate(widths):
        if i % 2 == 0:
            draw.rectangle([x, 0, x + w * px - 1, box_h - 1], fill=0)
        x += w * px
    return img
