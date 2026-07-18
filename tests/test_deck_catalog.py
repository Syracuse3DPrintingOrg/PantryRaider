"""The bundled deck catalog (service/app/data/deck_catalog.json) must stay in
sync with the live action registry in the Stream Deck controller package, and
the app must actually serve the full palette off-Pi (FoodAssistant-zx66)."""
import json
from pathlib import Path

import pytest

from foodassistant_streamdeck.actions import catalog

from app.services import start_page

BUNDLED_PATH = (
    Path(__file__).resolve().parent.parent
    / "service" / "app" / "data" / "deck_catalog.json"
)

# Keys Dan's Korolev screenshot was missing versus his pi_remote: the whole
# point of the bundled catalog is that these show up in the off-Pi palette.
EXPECTED_KEYS = {
    "ready", "cooked", "scan_mode", "shopping_check",
    "timer_eggs", "timer_pasta", "timer_rice",
    "meal_today", "clock",
    "scale_half", "scale_1x", "scale_2x",
    "camera", "camera_full",
    "convert", "timers_view",
    "screen_off", "screen_on",
}


def _bundled() -> list[dict]:
    return json.loads(BUNDLED_PATH.read_text(encoding="utf-8"))


def test_bundled_catalog_matches_live_registry():
    """Regenerate the export in-memory and compare it to the checked-in JSON."""
    assert _bundled() == catalog(), (
        "service/app/data/deck_catalog.json is stale: the Stream Deck action "
        "registry changed. Run scripts/gen-deck-catalog.py and commit the "
        "regenerated JSON."
    )


def test_bundled_catalog_has_the_full_palette():
    names = {a["name"] for a in _bundled()}
    missing = EXPECTED_KEYS - names
    assert not missing, f"bundled catalog is missing keys: {sorted(missing)}"


def test_bundled_add_key_is_labelled_pantry():
    add = next(a for a in _bundled() if a["name"] == "add")
    assert add["label"] == "Pantry"


def test_bundled_catalog_loader_reads_the_shipped_file():
    assert start_page.bundled_catalog() == _bundled()


@pytest.mark.anyio
async def test_fetch_deck_catalog_returns_bundled_off_pi(monkeypatch):
    from app import hardware
    monkeypatch.setattr(hardware, "is_raspberry_pi", lambda: False)
    got = await start_page.fetch_deck_catalog()
    assert got == _bundled()


def test_action_href_covers_new_navigation_keys():
    """Newly exposed navigation-capable keys open a page on the Start Page;
    hardware/info keys deliberately stay unmapped (deck-only)."""
    hrefs = start_page.ACTION_HREF
    assert hrefs["ready"] == "ui/cook"
    assert hrefs["scan_mode"] == "ui/add"
    assert hrefs["shopping_check"] == "ui/shopping"
    for key in ("cooked", "scale_half", "scale_1x", "scale_2x"):
        assert hrefs[key] == "ui/current-recipe"
    assert hrefs["health"] == "setup"
    for deck_only in ("clock", "screen_off", "screen_on", "brightness",
                      "page_next", "page_prev", "pin", "kiosk_restart",
                      "update", "reboot"):
        assert deck_only not in hrefs
    # Every mapped key must exist in the catalog (no dangling hrefs), except
    # the historic aliases kept for saved layouts.
    names = {a["name"] for a in _bundled()}
    dangling = {k for k in hrefs if k not in names}
    assert dangling <= {"guide", "nutrition", "audit", "shop", "settings"}, (
        f"ACTION_HREF has entries missing from the catalog: {sorted(dangling)}"
    )
