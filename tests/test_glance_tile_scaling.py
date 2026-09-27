"""Glance tile scaling guard (Dan's 7 inch landscape panel).

A source-level pin, because CSS sizing cannot be asserted from pytest and this
bug only shows on real hardware.

The Glance tiles stretch to fill a kiosk panel, but their contents were sized
against the VIEWPORT (vmin on the web layout, vh in the kiosk tile mode). On a
short landscape panel both of those measure the 600px-tall side, so the icon and
label shrank exactly where the tile had grown: a 7 inch 1024x600 panel showed a
small island of content floating in a big empty tile (icon 32.5px and label 18px
inside a 205x172 tile, filling half its height). Portrait panels never showed it,
because there vh is the LONG side.

The fix ties the contents to the tile itself with container queries, so a big
tile gets big contents. Two things have to stay true:

  * the tile is a query container and its contents are sized in container units;
  * every container-unit rule keeps a viewport-unit declaration ahead of it, so
    a browser without container query support drops the cq line and lands on
    today's behaviour rather than on nothing.

The size containment is deliberately landscape-only: a portrait tile has no
definite height to contain on a cramped screen, and containing it there
collapsed the cards (139.9px tall to 90.3px at a large UI scale, crushing the
hint text to 3px).
"""
from __future__ import annotations

import re
from pathlib import Path

_START = Path(__file__).resolve().parents[1] / "service/app/templates/start.html"


def _css() -> str:
    return _START.read_text(encoding="utf-8")


def test_glance_card_is_a_query_container():
    src = _css()
    assert "container-type: inline-size" in src, (
        "the .glance-card must be a query container, otherwise its contents "
        "size against the window and a big tile keeps small contents")


def test_tile_contents_are_sized_from_the_card_not_the_viewport():
    """Icon, label and blurb each size in container units."""
    src = _css()
    for selector in (".glance-card-ico", ".glance-card-label", ".glance-card-blurb"):
        # Grab every declaration block for this selector.
        blocks = re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", src)
        assert blocks, f"{selector} vanished from start.html"
        joined = " ".join(blocks)
        assert re.search(r"\d+(\.\d+)?cq(i|h|min|w|b)\b", joined), (
            f"{selector} must size from the card in container units (cqi/cqh); "
            "sizing it in vmin/vh pins it to the window's short side and leaves "
            "a big landscape tile holding small contents")


def test_container_units_keep_a_viewport_unit_fallback():
    """A viewport-unit size always precedes the first container-unit one.

    An unsupported unit makes its whole declaration invalid and it is dropped,
    so an earlier declaration wins. The fallback may sit in the same block (the
    web layout) or in an earlier matching block (the kiosk tile rules back the
    landscape ones), but it must come FIRST either way, or a browser without
    container queries is left with no size at all.
    """
    src = _css()
    for selector in (".glance-card-ico", ".glance-card-label", ".glance-card-blurb"):
        sizes = []  # (position, declaration) for every font-size on this selector
        for block in re.finditer(re.escape(selector) + r"\s*\{([^}]*)\}", src):
            for decl in re.finditer(r"font-size:\s*([^;]+);", block.group(1)):
                sizes.append((block.start() + decl.start(), decl.group(1)))
        sizes.sort()
        cq = [i for i, (_, d) in enumerate(sizes) if re.search(r"cq(i|h|min)\b", d)]
        assert cq, f"{selector} lost its container-unit sizing"
        viewport = [i for i, (_, d) in enumerate(sizes)
                    if re.search(r"\b\d+(\.\d+)?(vmin|vh|vw)\b", d)]
        assert viewport, (
            f"{selector} has no viewport-unit fallback; a browser without "
            "container query support would render it with no size at all")
        assert min(viewport) < min(cq), (
            f"{selector}: the viewport-unit fallback must be declared before the "
            "container-unit size, otherwise the fallback wins on browsers that "
            "do support container queries and the fix does nothing")


def test_size_containment_is_landscape_only():
    """`container-type: size` must never apply to a portrait tile.

    A portrait tile's height comes from its content, so containing it removes
    that floor and the cards collapse on a cramped screen.
    """
    src = _css()
    assert "container-type: size" in src, "the landscape tile lost its size containment"
    # The only size-containment rule lives in a landscape-gated media query.
    for match in re.finditer(r"container-type:\s*size", src):
        head = src[:match.start()]
        opener = head.rfind("@media")
        assert opener != -1, "container-type: size must sit inside a media query"
        query = src[opener:src.index("{", opener)]
        assert "orientation: landscape" in query, (
            "container-type: size must be gated on landscape; containing a "
            f"portrait tile collapses the cards. Found under: {query.strip()!r}")


def test_portrait_tile_mode_keeps_its_viewport_sizing():
    """The portrait tile rules stay vh-based: that orientation reads correctly."""
    src = _css()
    block = re.search(r"@media \(max-height: 560px\), \(max-width: 560px\) \{", src)
    assert block, "the kiosk tile-mode media query moved; re-check this guard"
