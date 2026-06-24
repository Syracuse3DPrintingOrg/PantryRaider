"""Key layout and paging for whatever deck happens to be plugged in.

A deck has a fixed number of keys (Mini 6, Original/MK.2 15, XL 32). When the
configured action list is longer than the deck, the last key becomes a page
cycle and the rest of the actions spill onto further pages. This module turns
a flat list of action names into one or more pages, each a fixed-length list
of slots where a slot is an ActionSpec or None for a blank key.
"""
from __future__ import annotations

from typing import Optional

from .actions import (
    ACTIONS,
    KEYPAD_CANCEL,
    KEYPAD_CLEAR,
    KEYPAD_ENTER,
    ActionSpec,
    keypad_specs,
)

# Physical grid for each known deck size, handy for docs and previews.
GRID: dict[int, tuple[int, int]] = {
    6: (3, 2),    # Stream Deck Mini / Module 6
    15: (5, 3),   # Stream Deck / MK.2 / Module 15
    32: (8, 4),   # Stream Deck XL / Module 32
}


def supported_key_counts() -> tuple[int, ...]:
    return tuple(sorted(GRID))


def display_dims(key_count: int, rotation: int) -> tuple[int, int]:
    """Return the (cols, rows) of the grid as the user sees it after rotating.

    For 0 and 180 the deck keeps its native shape. For 90 and 270 it is turned
    on its side, so columns and rows swap (an 8x4 XL becomes a 4x8 portrait).
    """
    cols, rows = GRID[key_count]
    if rotation in (90, 270):
        return rows, cols
    return cols, rows


def rotated_index(index: int, key_count: int, rotation: int) -> int:
    """Map a visual slot to the physical key it lands on after rotation.

    ``index`` is a slot in row-major order of the *displayed* grid (the grid the
    web editor draws, with columns and rows swapped for 90/270). We recover its
    (row, col) using the displayed dimensions, rotate the coordinate into the
    deck's native grid, and flatten to a physical key. The map is an exact
    bijection for all four rotations, so every slot lands on a distinct key.
    """
    if rotation == 0 or key_count not in GRID:
        return index
    p_cols, p_rows = GRID[key_count]
    d_cols, d_rows = display_dims(key_count, rotation)
    if not (0 <= index < d_cols * d_rows):
        return index
    vr, vc = divmod(index, d_cols)
    if rotation == 180:
        pr, pc = p_rows - 1 - vr, p_cols - 1 - vc
    elif rotation == 90:
        pr, pc = p_rows - 1 - vc, vr
    else:  # 270
        pr, pc = vc, p_cols - 1 - vr
    return pr * p_cols + pc


def slot_for_physical(phys: int, key_count: int, rotation: int) -> int:
    """Inverse of ``rotated_index``: physical key -> displayed-grid slot.

    Used when a key is pressed: the device reports the physical index, and we
    recover which slot the user sees there so the right action fires.
    """
    if rotation == 0 or key_count not in GRID:
        return phys
    p_cols, p_rows = GRID[key_count]
    d_cols, d_rows = display_dims(key_count, rotation)
    if not (0 <= phys < p_cols * p_rows):
        return phys
    pr, pc = divmod(phys, p_cols)
    if rotation == 180:
        vr, vc = p_rows - 1 - pr, p_cols - 1 - pc
    elif rotation == 90:
        vr, vc = pc, p_rows - 1 - pr
    else:  # 270
        vr, vc = p_cols - 1 - pc, pr
    return vr * d_cols + vc


def build_keypad_pages(key_count: int) -> list[list[Optional[ActionSpec]]]:
    """Lay out a numeric PIN keypad across one or more deck-sized pages.

    The pad always offers digits 0-9, a Clear/backspace key, an Enter/submit
    key, and a Cancel key that returns to the normal layout. When the whole pad
    fits on the deck it is a single page: on the wide XL grid (8x4) the digits
    fall in a phone-style 3x3 block with the controls below; otherwise the pad
    is laid out in reading order. A deck too small to hold the pad at once (the
    6-key Mini) spills onto further pages, with the final slot of each page
    becoming a wrapping page-cycle key, exactly like ``build_pages``.

    Returns a list of pages, each a flat list of exactly ``key_count`` slots
    (ActionSpec or None).
    """
    if key_count < 1:
        raise ValueError("key_count must be positive")

    ks = keypad_specs()
    clear = ks[f"keypad_{KEYPAD_CLEAR}"]
    enter = ks[f"keypad_{KEYPAD_ENTER}"]
    cancel = ks[f"keypad_{KEYPAD_CANCEL}"]
    digit = {d: ks[f"keypad_{d}"] for d in "0123456789"}

    cols, rows = GRID.get(key_count, (key_count, 1))

    # The full pad in reading order: digits 1-9, then Clear, 0, Enter, Cancel.
    full: list[ActionSpec] = [digit[d] for d in "123456789"]
    full += [clear, digit["0"], enter, cancel]

    if cols >= 3 and rows >= 4:
        # Phone-style block on a roomy grid (XL): 1-9 in a 3x3, Clear/0/Enter on
        # the fourth row, Cancel in the top-right spare cell.
        page: list[Optional[ActionSpec]] = [None] * key_count
        order = [
            "1", "2", "3",
            "4", "5", "6",
            "7", "8", "9",
            KEYPAD_CLEAR, "0", KEYPAD_ENTER,
        ]
        for i, token in enumerate(order):
            r, c = divmod(i, 3)
            page[r * cols + c] = digit.get(token) or ks[f"keypad_{token}"]
        page[cols - 1] = cancel
        return [page]

    if len(full) <= key_count:
        # Single page, reading order, padded with blanks.
        page = list(full)
        page += [None] * (key_count - len(page))
        return [page]

    # Too small for the whole pad: paginate with a wrapping page-cycle key in
    # the last slot of every page.
    usable = key_count - 1
    pages: list[list[Optional[ActionSpec]]] = []
    for start in range(0, len(full), usable):
        chunk = full[start : start + usable]
        page = list(chunk)
        page += [None] * (usable - len(page))
        page.append(ACTIONS["page_next"])
        pages.append(page)
    return pages


def _specs(names: list[str]) -> list[ActionSpec]:
    return [ACTIONS[n] for n in names if n in ACTIONS]


def _to_slot(name: str) -> Optional[ActionSpec]:
    if name == "blank":
        return None
    return ACTIONS.get(name)


def build_pages(
    action_names: list[str], key_count: int
) -> list[list[Optional[ActionSpec]]]:
    """Split action names into deck-sized pages.

    With a single page everything fits and no key is sacrificed for paging.
    When more actions are configured than fit, the final key of every page
    becomes a wrapping "More" key and the remaining actions continue on the
    next page. An explicit "blank" name produces an empty slot in place,
    preserving the positions of the keys around it.
    """
    if key_count < 1:
        raise ValueError("key_count must be positive")
    # Keep known actions and explicit blanks, preserving order/position.
    slots = [(_to_slot(n)) for n in action_names if n == "blank" or n in ACTIONS]
    if len(slots) <= key_count:
        page = list(slots) + [None] * (key_count - len(slots))
        return [page]
    usable = key_count - 1
    pages = []
    for start in range(0, len(slots), usable):
        chunk = slots[start:start + usable]
        page = list(chunk) + [None] * (usable - len(chunk))
        page.append(ACTIONS["page_next"])
        pages.append(page)
    return pages
