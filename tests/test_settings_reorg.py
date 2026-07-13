"""Settings information architecture (FoodAssistant-y78w, -s6q, -42n4, -kjk8).

The Settings page is one grouped menu, no Personalization/Settings toggle:
pills sit under four plain-language headings (Kitchen, This Device,
Connections, System). Wi-Fi and the two hostname fields live on a dedicated
Network pane under This Device, and Home Assistant has its own pane under
Connections. These tests guard that structure:

* every pane pill renders for the shapes it applies to, under one grouped menu,
* no form control from the pre-reorg page was lost in the moves (the id lists
  below were collected from the old template, per deployment shape),
* old ``#pane-*`` deep links resolve through the ``PANE_HASH_ALIASES`` map,
* the Start Page & Stream Deck pane keeps its two-editor toggle.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app.config import settings  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import app

    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "auth_password", "")
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _render(client, monkeypatch, *, mode: str, is_pi: bool) -> str:
    monkeypatch.setattr(settings, "deployment_mode", mode)
    with patch.object(type(settings), "is_configured", lambda self: True), \
         patch("app.routers.setup.is_raspberry_pi", return_value=is_pi), \
         patch("app.templating.is_raspberry_pi", return_value=is_pi):
        r = client.get("/setup")
    assert r.status_code == 200
    return r.text


SHAPES = [("server", False), ("pi_hosted", True), ("pi_remote", True)]

# Menu membership: pane -> shapes that get the pill. After the IA reorg
# (FoodAssistant-s6q, -42n4, -kjk8) there is one grouped menu, no data-mgroup
# toggle. Grouping is by plain-language heading; membership is what matters.
MENU_PILLS = {
    # Status: a health dashboard, right below Overview, shown in every mode
    # (FoodAssistant-w00b).
    "pane-status": {"server", "pi_hosted", "pi_remote"},
    # Kitchen.
    "pane-inventory": {"server", "pi_hosted"},
    "pane-personalization-recipes": {"server", "pi_hosted", "pi_remote"},
    # Recipe suggestions: the taste tuning split off from the connection
    # settings (FoodAssistant-ysj1). Renders in every mode (read-only on a
    # satellite, like the Recipes pane).
    "pane-recipe-tuning": {"server", "pi_hosted", "pi_remote"},
    "pane-scanning": {"server", "pi_hosted", "pi_remote"},
    # This Device.
    "pane-screen": {"server", "pi_hosted", "pi_remote"},
    "pane-network": {"server", "pi_hosted", "pi_remote"},
    "pane-start-page": {"server", "pi_hosted", "pi_remote"},
    "pane-appearance": {"server", "pi_hosted", "pi_remote"},
    # Connections.
    "pane-home-assistant": {"server", "pi_hosted", "pi_remote"},
    "pane-connections": {"server", "pi_hosted", "pi_remote"},
    # Forager (account sign-in + remote access) is main-install only.
    "pane-forager": {"server", "pi_hosted"},
    # System.
    "pane-devices": {"server", "pi_hosted", "pi_remote"},
    "pane-security": {"server", "pi_hosted", "pi_remote"},
    "pane-backups": {"server", "pi_hosted", "pi_remote"},
    "pane-advanced": {"server", "pi_hosted", "pi_remote"},
}

# Every form-control id the settings side (below the side menu) rendered
# before the reorganization, per deployment shape. The reorganization moves
# markup between panes; it must never drop a control. Collected from the
# pre-reorg template render.
_COMMON = [
    "anthropic_api_key", "anthropic_model", "anthropic_model_sel",
    # api_key removed: the legacy primary-key row renders only when a key is
    # already stored; new installs use named keys (FoodAssistant-f8kp).
    "auth_password", "auth_required", "auto_update",
    "background_file", "background_image_url", "background_opacity",
    "backup_include_secrets", "barcode_autocheck_shopping",
    "barcode_enrichment", "barcode_global_capture", "barcode_llm_fallback",
    "cook_ai_context",
    "custom-heading-icon", "custom-heading-label",
    "custom-tab-icon", "custom-tab-label", "custom-tab-url",
    "custom_theme_accent", "custom_theme_base", "custom_theme_bg",
    "custom_theme_name", "custom_theme_primary", "custom_theme_surface",
    "custom_theme_text",
    "debug_logging", "device_hostname",
    "enrich_model", "enrich_model_sel", "enrich_provider",
    "expiring_soon_days",
    "floating_nav_autohide_streamdeck", "floating_nav_position",
    "gemini_api_key", "gemini_model", "gemini_model_sel",
    "grocy_api_key", "grocy_base_url", "grocy_public_url",
    "ha_camera_popup_seconds", "ha_events_device", "ha_events_enabled",
    "mealie_api_key", "mealie_base_url", "mealie_public_url",
    "nav_visibility",
    "ollama_base_url", "ollama_model", "ollama_model_sel",
    "openai_api_key", "openai_model", "openai_model_sel",
    "perishable_days", "qr_public_url", "qr_url_mode", "quiet_mode",
    "rclone_remote", "rclone_schedule_hours", "recipe_source",
    "restore-file", "scanner-test-input", "scanner_type",
    "settings-search", "spoonacular_api_key", "staple_items",
    "start_icon_color", "start_key_style", "start_page_enabled",
    "start_page_keys",
    "streamdeck_ha_base_url", "streamdeck_ha_token",
    "streamdeck_weather_location", "streamdeck_weather_units",
    "suggest_per_tier", "themealdb_api_key", "totp-code",
    "ui_theme", "usb_backup_interval_hours", "vision_provider",
    "appliance_stand_mixer", "appliance_air_fryer", "appliance_oven",
]
_PI_COMMON = [
    "display_idle_timeout", "display_touch", "display_type",
    "full-restore-source", "has_streamdeck", "kms_rotation", "new_hostname",
    "scheduled_reboot_time", "screensaver_minutes", "screensaver_mode",
    "screensaver_speed", "sd-profile-name-input", "sd-profile-select",
    "streamdeck_brightness", "streamdeck_icon_color",
    "streamdeck_idle_timeout", "streamdeck_key_count",
    "streamdeck_key_style", "streamdeck_rotation",
    "ui_scale", "wake_on_motion", "wifi_password", "wifi_ssid",
]
EXPECTED_IDS = {
    "server": _COMMON + [
        "ai_token_budget",
        "cam-ip-host", "cam-ip-name", "cam-ip-pass", "cam-ip-path",
        "cam-ip-port", "cam-ip-preset", "cam-ip-user", "cam-scan-cidr",
        "scan_cidr", "timezone",
        "tunnel_mode_cloudflare", "tunnel_mode_off",
        "tunnel_mode_forager", "tunnel_token",
    ],
    "pi_hosted": _COMMON + _PI_COMMON + [
        "ai_token_budget",
        "cam-ip-host", "cam-ip-name", "cam-ip-pass", "cam-ip-path",
        "cam-ip-port", "cam-ip-preset", "cam-ip-user", "cam-scan-cidr",
        "scan_cidr", "timezone",
        "tunnel_mode_cloudflare", "tunnel_mode_off",
        "tunnel_mode_forager", "tunnel_token",
        "switch_server_url", "switch_upstream_api_key",
    ],
    "pi_remote": _COMMON + _PI_COMMON + [
        "kiosk_pin", "kiosk_readonly_when_locked",
        "remote_server_url", "upstream_api_key",
    ],
}


def test_two_menus_with_grouped_pills(client, monkeypatch):
    for mode, is_pi in SHAPES:
        html = _render(client, monkeypatch, mode=mode, is_pi=is_pi)
        for pane, shapes in MENU_PILLS.items():
            pill = re.search(
                rf'data-bs-toggle="pill" data-bs-target="#{pane}"',
                html,
            )
            if mode in shapes:
                assert pill, (mode, pane, "pill missing")
            else:
                assert not pill, (mode, pane, "unexpected pill")
        # The dissolved Recipes & Meals pane has no pill and no pane div.
        assert 'data-bs-target="#pane-recipes"' not in html
        assert 'id="pane-recipes"' not in html
        # One grouped menu, no two-menu toggle: the four headings frame it.
        for heading in ("Kitchen", "This Device", "Connections", "System"):
            assert f'class="menu-heading">{heading}<' in html
        assert 'id="menu-toggle-p"' not in html
        assert 'id="menu-toggle-s"' not in html
        assert "showSettingsMenu(" not in html


def test_no_setting_lost_in_reorg(client, monkeypatch):
    """Every form control from the pre-reorg settings page still renders."""
    for mode, is_pi in SHAPES:
        html = _render(client, monkeypatch, mode=mode, is_pi=is_pi)
        region = html.split('<div class="side-menu">', 1)[1]
        found = set(
            m.group(2)
            for m in re.finditer(
                r'<(input|select|textarea)\b[^>]*\bid="([^"]+)"', region
            )
        )
        missing = [i for i in EXPECTED_IDS[mode] if i not in found]
        assert not missing, (mode, missing)


def test_old_pane_hashes_have_aliases(client, monkeypatch):
    html = _render(client, monkeypatch, mode="server", is_pi=False)
    # PANE_HASH_ALIASES lives in the setup menu module, not inline in the page.
    menu_js = client.get("static/js/setup/menu.js").text
    for old, new in {
        # Original (pre-reorg) anchors.
        "pane-theme": "pane-appearance",
        "pane-navigation": "pane-appearance",
        "pane-display": "pane-screen",
        "pane-ai": "pane-scanning",
        "pane-hardware": "pane-scanning",
        "pane-personalization-storage": "pane-inventory",
        # Home Assistant got its own pane (FoodAssistant-s6q); cameras keep the
        # (relabelled) Connections pane.
        "pane-homeassistant": "pane-home-assistant",
        "pane-cameras": "pane-connections",
        "pane-tunnel": "pane-forager",
        "pane-upstream": "pane-devices",
        "pane-data": "pane-backups",
        "pane-streamdeck": "pane-start-page",
        # Mealie + the recipe sources moved to the Recipes pane under Kitchen.
        "pane-recipes": "pane-personalization-recipes",
    }.items():
        assert f"'{old}': '{new}'," in menu_js, f"missing alias {old} -> {new}"
    # Every live anchor still lands: as a live pane div, or via alias.
    for anchor in ("pane-appearance", "pane-screen", "pane-scanning",
                   "pane-inventory", "pane-connections", "pane-devices",
                   "pane-security", "pane-backups", "pane-advanced",
                   "pane-start-page", "pane-network", "pane-home-assistant"):
        assert f'id="{anchor}"' in html, f"anchor lost: {anchor}"
    # Now-live panes must not shadow themselves in the alias map.
    assert "'pane-personalization-recipes':" not in menu_js
    assert "'pane-start-page':" not in menu_js
    assert "'pane-network':" not in menu_js
    assert "'pane-home-assistant':" not in menu_js
    # Dissolved/renamed panes leave no dead pane divs behind.
    for gone in ("pane-theme", "pane-navigation", "pane-display", "pane-ai",
                 "pane-hardware", "pane-personalization-storage",
                 "pane-homeassistant", "pane-cameras", "pane-tunnel",
                 "pane-upstream", "pane-data", "pane-recipes"):
        assert f'id="{gone}"' not in html, f"stale pane div: {gone}"


def test_start_deck_pane_sub_toggle(client, monkeypatch):
    # On a Pi the pill offers both editors through the pane toggle; off-Pi
    # there is no deck, so the Start Page stands alone with no toggle.
    pi = _render(client, monkeypatch, mode="pi_hosted", is_pi=True)
    assert "showDeckStart('start')" in pi
    assert "showDeckStart('deck')" in pi
    assert 'id="pane-start-page"' in pi and 'id="pane-streamdeck"' in pi
    assert "Start Page &amp; Stream Deck" in pi
    srv = _render(client, monkeypatch, mode="server", is_pi=False)
    assert 'id="pane-start-page"' in srv
    assert 'id="pane-streamdeck"' not in srv
    assert 'class="btn-group mb-3 ds-toggle"' not in srv
    # The old three-way This Device toggle is gone everywhere.
    assert "showDeckStart('devices')" not in pi
    assert "showDeckStart('devices')" not in srv


def test_recipe_split_between_menus(client, monkeypatch):
    """Recipe settings split across two Kitchen panes (FoodAssistant-ysj1): the
    Recipes pane holds only the Mealie + external-source connection cards
    (savePaneRecipes), while the taste tuning lives in its own Recipe
    suggestions pane (savePaneRecipePrefs). Keeping the tuning in a separate
    pane and save scope means it can never overwrite the connection fields.
    The Cameras pane still carries no Mealie fields."""
    html = _render(client, monkeypatch, mode="server", is_pi=False)
    conn = html.split('id="pane-personalization-recipes"', 1)[1] \
               .split('id="pane-', 1)[0]
    tune = html.split('id="pane-recipe-tuning"', 1)[1] \
               .split('id="pane-', 1)[0]
    for field in ("mealie_base_url", "mealie_api_key", "recipe_source",
                  "themealdb_api_key", "spoonacular_api_key"):
        assert field in conn, f"{field} not in Recipes pane"
        assert field not in tune, f"{field} leaked into tuning pane"
    for field in ("staple_items", "cook_ai_context", "kitchen-appliances",
                  "perishable_days", "expiring_soon_days", "suggest_per_tier"):
        assert field in tune, f"{field} not in Recipe suggestions pane"
        assert field not in conn, f"{field} leaked into Recipes pane"
    assert 'onclick="savePaneRecipes(this)"' in conn
    assert 'onclick="savePaneRecipePrefs(this)"' not in conn
    assert 'onclick="savePaneRecipePrefs(this)"' in tune
    assert 'onclick="savePaneRecipes(this)"' not in tune
    cam = html.split('id="pane-connections"', 1)[1].split('id="pane-', 1)[0]
    assert "mealie_base_url" not in cam


def test_satellite_devices_pane_holds_main_server(client, monkeypatch):
    sat = _render(client, monkeypatch, mode="pi_remote", is_pi=True)
    dev = sat.split('id="pane-devices"', 1)[1].split('id="pane-', 1)[0]
    assert "remote_server_url" in dev
    assert "syncFromUpstream" in dev
    # The kiosk PIN stays in Security & Access.
    sec = sat.split('id="pane-security"', 1)[1].split('id="pane-', 1)[0]
    assert "kiosk_pin" in sec
    assert "kiosk_readonly_when_locked" in sec


def test_scheduled_reboot_frequency_controls(client, monkeypatch):
    """The scheduled reboot offers Off/Nightly/Weekly with a day picker on the
    Pi appliance shapes, and a pre-frequency install whose only stored value is
    a reboot time renders as Nightly, so its behaviour is unchanged
    (FoodAssistant-8x4u)."""
    monkeypatch.setattr(settings, "scheduled_reboot_time", "", raising=False)
    monkeypatch.setattr(settings, "scheduled_reboot_frequency", "", raising=False)
    for mode, is_pi in SHAPES:
        html = _render(client, monkeypatch, mode=mode, is_pi=is_pi)
        present = 'id="scheduled_reboot_frequency"' in html
        assert present == (mode in ("pi_hosted", "pi_remote")), mode
        if present:
            assert 'id="scheduled_reboot_day"' in html
            # Nothing stored: the schedule renders as Off.
            assert re.search(r'value="off"\s+selected', html)
    # Legacy install: a stored time with no frequency stays nightly.
    monkeypatch.setattr(settings, "scheduled_reboot_time", "03:30", raising=False)
    html = _render(client, monkeypatch, mode="pi_hosted", is_pi=True)
    assert re.search(r'value="nightly"\s+selected', html)


def test_scheduled_reboot_save_validation(client, monkeypatch):
    """/setup/save keeps only sane reboot frequency/day values; junk keeps the
    stored setting (FoodAssistant-8x4u)."""
    # Server mode so the save never tries to reach a host bridge.
    monkeypatch.setattr(settings, "deployment_mode", "server")
    saved = {}
    monkeypatch.setattr(type(settings), "save", lambda self, d: saved.update(d))
    r = client.post("/setup/save", json={
        "scheduled_reboot_frequency": "weekly", "scheduled_reboot_day": 3})
    assert r.status_code == 200
    assert saved["scheduled_reboot_frequency"] == "weekly"
    assert saved["scheduled_reboot_day"] == 3
    saved.clear()
    r = client.post("/setup/save", json={
        "scheduled_reboot_frequency": "sometimes", "scheduled_reboot_day": 9})
    assert r.status_code == 200
    assert "scheduled_reboot_frequency" not in saved
    assert "scheduled_reboot_day" not in saved
