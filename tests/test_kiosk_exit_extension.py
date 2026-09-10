"""The kiosk return extension (FoodAssistant-wn8w).

The kiosk browser is chromeless, so an external page (an Amazon listing from
the Shop page) used to strand the display with no way back. A tiny local
Chromium extension overlays a floating return button and a top-edge swipe on
every page that is not the app itself. These tests pin the checked-in
extension's safety properties (no permissions, fail-closed placeholder, the
navigation target is the baked constant and nothing a page could influence)
and that both provisioning paths (firstboot and foodassistant-update) install
it and guard the browser flags on a verified bake.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXT = REPO / "scripts" / "image-build" / "kiosk-exit-extension"
FIRSTBOOT = REPO / "scripts" / "image-build" / "firstboot.sh"
UPDATE = REPO / "scripts" / "image-build" / "foodassistant-update"

PLACEHOLDER = "__PR_KIOSK_HOME_URL__"


# -- the manifest -------------------------------------------------------------

def _manifest() -> dict:
    return json.loads((EXT / "manifest.json").read_text())


def test_manifest_is_mv3_with_no_permissions_at_all():
    m = _manifest()
    assert m["manifest_version"] == 3
    # The extension needs nothing beyond content-script injection. Any
    # permission grant here widens what a compromised page could reach.
    assert "permissions" not in m
    assert "host_permissions" not in m
    assert "background" not in m


def test_manifest_injects_origin_then_content_on_http_pages_only():
    cs = _manifest()["content_scripts"]
    assert len(cs) == 1
    assert cs[0]["matches"] == ["http://*/*", "https://*/*"]
    # origin.js must load first so content.js sees the baked URL.
    assert cs[0]["js"] == ["origin.js", "content.js"]
    assert cs[0]["all_frames"] is False


# -- the scripts themselves ---------------------------------------------------

def test_origin_carries_the_placeholder_exactly_once():
    text = (EXT / "origin.js").read_text()
    assert text.count(PLACEHOLDER) == 1
    # The repo copy must never ship a baked device URL.
    assert "http://" not in text.replace("http://*", "")
    assert "https://" not in text


def test_content_fails_closed_on_an_unbaked_placeholder():
    text = (EXT / "content.js").read_text()
    assert '.indexOf("__PR_KIOSK_HOME") !== -1) return' in text


def test_content_navigates_only_to_the_baked_constant():
    text = (EXT / "content.js").read_text()
    # One navigation primitive, aimed at the constant captured at startup.
    assert "location.assign(HOME_HREF)" in text
    # Nothing a page could steer: no open(), no href assignment, no reading
    # attacker-controlled locations back into a navigation.
    assert "window.open" not in text
    assert "location.href =" not in text
    assert "location.hash" not in text
    assert "document.referrer" not in text


def test_content_never_touches_the_apps_own_pages():
    text = (EXT / "content.js").read_text()
    assert "location.origin === home.origin) return" in text


def test_content_touch_listeners_are_passive():
    text = (EXT / "content.js").read_text()
    for ev in ("touchstart", "touchmove", "touchend"):
        # Every touch hook is passive so external pages keep smooth scrolling.
        for m in re.finditer(r'addEventListener\("%s"' % ev, text):
            tail = text[m.start():m.start() + 400]
            assert "passive: true" in tail, f"{ev} listener is not passive"


def test_content_pins_the_button_id_and_swipe_thresholds():
    text = (EXT / "content.js").read_text()
    assert 'BTN_ID = "pantry-raider-return"' in text
    assert "EDGE_PX = 24" in text
    assert "TRAVEL_PX = 80" in text


# -- provisioning: firstboot --------------------------------------------------

def test_firstboot_installs_and_bakes_the_extension():
    text = FIRSTBOOT.read_text()
    assert "kiosk-exit-extension" in text
    assert PLACEHOLDER in text
    # The flags reach the unit only after the bake is verified: the guard
    # greps for the placeholder still being present and skips the flags.
    assert re.search(r'grep -q "%s" .*origin\.js' % PLACEHOLDER, text)
    assert "--load-extension=$ext_dir --disable-extensions-except=$ext_dir" in text
    # And the unit line actually carries the (possibly empty) flags variable.
    assert "$ext_flags" in text


# -- provisioning: the update path (deployed devices) --------------------------

def test_update_refreshes_bakes_and_guards_the_flags():
    text = UPDATE.read_text()
    assert "kiosk-exit-extension" in text
    assert PLACEHOLDER in text
    # Adds the flags once, only on a verified bake.
    assert "--load-extension=$KIOSK_EXT_DIR --disable-extensions-except=$KIOSK_EXT_DIR" in text
    # And strips them again if a later run finds the bake broken, so the
    # kiosk never launches pointing at a dead extension directory.
    assert re.search(r'sed -i -E "s\| --load-extension=', text)
