"""The bundled Code 128 encoder prints bars a scanner can read.

The decode here works from the image alone: it measures the bar and space
widths along one row, turns them into module widths, and looks each symbol up
in the Code 128 table, then checks the start code, the checksum and the stop
pattern before reading the characters back.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parent.parent / "service"
sys.path.insert(0, str(SERVICE))

from app.services import code128  # noqa: E402

# A few table entries straight from the Code 128 specification, so the test
# does not only check the encoder against its own table.
SPEC = {0: "212222", 16: "123122", 33: "111323", 99: "113141", 100: "114131",
        104: "211214", 105: "211232", 106: "2331112"}


def _runs(row):
    runs, cur, n = [], row[0], 0
    for px in row:
        if px == cur:
            n += 1
        else:
            runs.append((cur, n))
            cur, n = px, 1
    runs.append((cur, n))
    return runs


def _decode(img) -> str:
    row = [0 if img.getpixel((x, img.height // 2)) < 128 else 1 for x in range(img.width)]
    runs = _runs(row)
    # Quiet zones on both ends are white.
    assert runs[0][0] == 1 and runs[-1][0] == 1
    widths = [n for _, n in runs[1:-1]]
    module = min(widths)
    assert all(w % module == 0 for w in widths), "bars are not whole modules"
    mods = "".join(str(w // module) for w in widths)
    assert runs[0][1] >= 10 * module and runs[-1][1] >= 10 * module
    assert mods.endswith(SPEC[106])
    body = mods[: -len(SPEC[106])]
    assert len(body) % 6 == 0
    lookup = {p: v for v, p in enumerate(code128.PATTERNS[:106])}
    syms = [lookup[body[i:i + 6]] for i in range(0, len(body), 6)]
    *data, check = syms
    assert check == (data[0] + sum(i * v for i, v in enumerate(data[1:], 1))) % 103
    assert data[0] in (104, 105)
    out, mode = [], "B" if data[0] == 104 else "C"
    for v in data[1:]:
        if v == 99 and mode == "B":
            mode = "C"
        elif v == 100 and mode == "C":
            mode = "B"
        elif mode == "B":
            out.append(chr(v + 32))
        else:
            out.append("%02d" % v)
    return "".join(out)


def test_spec_patterns_match_the_table():
    for value, pattern in SPEC.items():
        assert code128.PATTERNS[value] == pattern
    assert len(code128.PATTERNS) == 107
    assert len(set(code128.PATTERNS)) == 107
    for p in code128.PATTERNS[:106]:
        assert sum(map(int, p)) == 11


@pytest.mark.parametrize("value", [
    "012345678905",      # a UPC-A as digits only: all set C
    "Pantry Raider",     # set B
    "SKU-0012345678",    # B, then a digit run in C
    "A12345678B",        # a digit run in the middle
    "12345",             # odd digit count
    "x",
])
def test_rendered_bars_decode_back_to_the_value(value):
    img = code128.render(value, 600, 40)
    assert img is not None
    assert _decode(img) == value


def test_known_symbol_values():
    # "PJJ123C" in set B: start B, the characters, checksum 55, stop.
    assert code128.symbols("PJJ123C") == [104, 48, 42, 42, 17, 18, 19, 35, 55, 106]
    # Digits only go into set C two at a time.
    assert code128.symbols("1234")[:3] == [105, 12, 34]


def test_too_long_or_unencodable_returns_none():
    assert code128.render("Café", 600, 40) is None
    assert code128.render("", 600, 40) is None
    assert code128.render("A" * 60, 100, 40) is None


def test_modules_are_whole_pixels_and_fit_the_box():
    img = code128.render("012345678905", 333, 50)
    assert img.width <= 333 and img.height == 50
