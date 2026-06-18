"""Key layout and paging for whatever deck happens to be plugged in.

A deck has a fixed number of keys (Mini 6, Original/MK.2 15, XL 32). When the
configured action list is longer than the deck, the last key becomes a page
cycle and the rest of the actions spill onto further pages. This module turns
a flat list of action names into one or more pages, each a fixed-length list
of slots where a slot is an ActionSpec or None for a blank key.
"""
from __future__ import annotations

from typing import Optional

from .actions import ACTIONS, ActionSpec

# Physical grid for each known deck size, handy for docs and previews.
GRID: dict[int, tuple[int, int]] = {
    6: (3, 2),    # Stream Deck Mini / Module 6
    15: (5, 3),   # Stream Deck / MK.2 / Module 15
    32: (8, 4),   # Stream Deck XL / Module 32
}


def supported_key_counts() -> tuple[int, ...]:
    return tuple(sorted(GRID))


def _specs(names: list[str]) -> list[ActionSpec]:
    return [ACTIONS[n] for n in names if n in ACTIONS]


def build_pages(
    action_names: list[str], key_count: int
) -> list[list[Optional[ActionSpec]]]:
    """Split action names into deck-sized pages.

    With a single page everything fits and no key is sacrificed for paging.
    When more actions are configured than fit, the final key of every page
    becomes a wrapping "More" key and the remaining actions continue on the
    next page.
    """
    if key_count < 1:
        raise ValueError("key_count must be positive")

    specs = _specs(action_names)

    if len(specs) <= key_count:
        page: list[Optional[ActionSpec]] = list(specs)
        page += [None] * (key_count - len(page))
        return [page]

    usable = key_count - 1  # last slot is the page-cycle key
    pages: list[list[Optional[ActionSpec]]] = []
    for start in range(0, len(specs), usable):
        chunk = specs[start : start + usable]
        page = list(chunk)
        page += [None] * (usable - len(page))
        page.append(ACTIONS["page_next"])
        pages.append(page)
    return pages
