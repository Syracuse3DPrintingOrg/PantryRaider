"""Setup page refactor guard (FoodAssistant-rbyy).

setup.html was split into setup/ partials plus static/js/setup/ modules. The
rendered page must stay byte-similar in the parts that matter: every form
control id and every menu pill target, per deployment shape. The PINNED lists
below were collected by rendering the page at the pre-refactor commit with the
page's script elements stripped, so they describe the real DOM, not strings
inside the inline script.

If a later change intentionally adds or removes a control, update the pinned
list in the same commit and say so; this test exists to catch silent losses.
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

_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.S)

# Shape name -> (deployment_mode, is_pi, configured)
SHAPES = {
    "server": ("server", False, True),
    "pi_hosted": ("pi_hosted", True, True),
    "pi_remote": ("pi_remote", True, True),
    "wizard": ("server", False, False),
}

PINNED = {
    "server": {
        "ids": [
            'ai_token_budget', 'anthropic_api_key', 'anthropic_model', 'anthropic_model_sel',
            'api_key', 'appliance_air_fryer', 'appliance_blender', 'appliance_bread_machine',
            'appliance_bun_steamer', 'appliance_cast_iron', 'appliance_deep_fryer', 'appliance_dehydrator',
            'appliance_dishwasher', 'appliance_dutch_oven', 'appliance_food_processor', 'appliance_freezer',
            'appliance_garlic_press', 'appliance_griddle', 'appliance_grill', 'appliance_hand_mixer',
            'appliance_ice_cream_maker', 'appliance_immersion_blender', 'appliance_kitchen_scale', 'appliance_mandoline',
            'appliance_meat_thermometer', 'appliance_microplane', 'appliance_microwave', 'appliance_mortar_pestle',
            'appliance_oven', 'appliance_panini_press', 'appliance_pasta_extruder', 'appliance_pasta_roller',
            'appliance_pastry_bag', 'appliance_pizza_stone', 'appliance_pressure_cooker', 'appliance_refrigerator',
            'appliance_rice_cooker', 'appliance_rolling_pin', 'appliance_slow_cooker', 'appliance_sm_food_processor',
            'appliance_sm_grain_mill', 'appliance_sm_ice_cream_maker', 'appliance_sm_meat_grinder', 'appliance_sm_pasta_roller_cutter',
            'appliance_sm_sausage_stuffer', 'appliance_sm_spiralizer', 'appliance_smoker', 'appliance_sous_vide',
            'appliance_spiralizer', 'appliance_stand_mixer', 'appliance_stove', 'appliance_toaster',
            'appliance_toaster_oven', 'appliance_torch', 'appliance_waffle_iron', 'appliance_wok',
            'auth_password', 'auth_required', 'auto_update', 'update_channel', 'background_file',
            'background_image_url', 'background_opacity', 'backup_include_secrets', 'barcode_autocheck_shopping',
            'barcode_enrichment', 'barcode_global_capture', 'barcode_llm_fallback', 'cam-ip-host',
            'cam-ip-name', 'cam-ip-pass', 'cam-ip-path', 'cam-ip-port',
            'cam-ip-preset', 'cam-ip-user', 'cam-scan-cidr',
            # Added after the pin: the Pantry Raider Cloud pairing input
            # (FoodAssistant-2nd1).
            'cloud_pairing_code',
            'cook_ai_context',
            'custom-heading-icon', 'custom-heading-label', 'custom-tab-icon', 'custom-tab-label',
            'custom-tab-url', 'custom_theme_accent', 'custom_theme_base', 'custom_theme_bg',
            'custom_theme_name', 'custom_theme_primary', 'custom_theme_surface', 'custom_theme_text',
            'debug_logging', 'device_hostname', 'enrich_model', 'enrich_model_sel',
            'enrich_provider', 'expiring_soon_days', 'floating_nav_autohide_streamdeck', 'floating_nav_position',
            'gemini_api_key', 'gemini_model', 'gemini_model_sel', 'grocy_api_key',
            'grocy_base_url', 'grocy_public_url', 'ha_camera_popup_seconds', 'ha_events_device',
            'ha_events_enabled', 'mealie_api_key', 'mealie_base_url', 'mealie_public_url',
            'nav_visibility', 'ollama_base_url', 'ollama_model', 'ollama_model_sel',
            'openai_api_key', 'openai_model', 'openai_model_sel', 'osk_enabled',
            'perishable_days', 'qr_public_url', 'qr_url_mode', 'quiet_mode',
            'rclone_remote', 'rclone_schedule_hours', 'recipe_source', 'restore-file',
            'scan_cidr', 'scanner-test-input', 'scanner_type', 'screensaver_all_clients',
            'screensaver_minutes', 'screensaver_mode', 'screensaver_speed', 'settings-search',
            'spoonacular_api_key', 'staple_items', 'start_icon_color', 'start_key_style',
            'start_page_enabled', 'start_page_keys', 'streamdeck_ha_base_url', 'streamdeck_ha_token',
            'streamdeck_weather_location', 'streamdeck_weather_units', 'suggest_per_tier', 'themealdb_api_key',
            'timezone', 'totp-code', 'tunnel_mode_cloudflare', 'tunnel_mode_off',
            'tunnel_mode_subscription', 'tunnel_token', 'ui_theme', 'usb_backup_interval_hours',
            'vision_provider',
        ],
        "targets": [
            '#pane-advanced', '#pane-appearance', '#pane-backups',
            '#pane-connections', '#pane-devices', '#pane-inventory',
            '#pane-personalization-recipes', '#pane-scanning', '#pane-screen',
            '#pane-security', '#pane-start-page',
        ],
    },
    "pi_hosted": {
        "ids": [
            'ai_token_budget', 'anthropic_api_key', 'anthropic_model', 'anthropic_model_sel',
            'api_key', 'appliance_air_fryer', 'appliance_blender', 'appliance_bread_machine',
            'appliance_bun_steamer', 'appliance_cast_iron', 'appliance_deep_fryer', 'appliance_dehydrator',
            'appliance_dishwasher', 'appliance_dutch_oven', 'appliance_food_processor', 'appliance_freezer',
            'appliance_garlic_press', 'appliance_griddle', 'appliance_grill', 'appliance_hand_mixer',
            'appliance_ice_cream_maker', 'appliance_immersion_blender', 'appliance_kitchen_scale', 'appliance_mandoline',
            'appliance_meat_thermometer', 'appliance_microplane', 'appliance_microwave', 'appliance_mortar_pestle',
            'appliance_oven', 'appliance_panini_press', 'appliance_pasta_extruder', 'appliance_pasta_roller',
            'appliance_pastry_bag', 'appliance_pizza_stone', 'appliance_pressure_cooker', 'appliance_refrigerator',
            'appliance_rice_cooker', 'appliance_rolling_pin', 'appliance_slow_cooker', 'appliance_sm_food_processor',
            'appliance_sm_grain_mill', 'appliance_sm_ice_cream_maker', 'appliance_sm_meat_grinder', 'appliance_sm_pasta_roller_cutter',
            'appliance_sm_sausage_stuffer', 'appliance_sm_spiralizer', 'appliance_smoker', 'appliance_sous_vide',
            'appliance_spiralizer', 'appliance_stand_mixer', 'appliance_stove', 'appliance_toaster',
            'appliance_toaster_oven', 'appliance_torch', 'appliance_waffle_iron', 'appliance_wok',
            'auth_password', 'auth_required', 'auto_update', 'update_channel', 'background_file',
            'background_image_url', 'background_opacity', 'backup_include_secrets', 'barcode_autocheck_shopping',
            'barcode_enrichment', 'barcode_global_capture', 'barcode_llm_fallback', 'cam-ip-host',
            'cam-ip-name', 'cam-ip-pass', 'cam-ip-path', 'cam-ip-port',
            'cam-ip-preset', 'cam-ip-user', 'cam-scan-cidr',
            # Added after the pin: the Pantry Raider Cloud pairing input
            # (FoodAssistant-2nd1).
            'cloud_pairing_code',
            'cook_ai_context',
            'custom-heading-icon', 'custom-heading-label', 'custom-tab-icon', 'custom-tab-label',
            'custom-tab-url', 'custom_theme_accent', 'custom_theme_base', 'custom_theme_bg',
            'custom_theme_name', 'custom_theme_primary', 'custom_theme_surface', 'custom_theme_text',
            'debug_logging', 'device_hostname', 'display_idle_timeout', 'display_touch',
            'display_type', 'enrich_model', 'enrich_model_sel', 'enrich_provider',
            'expiring_soon_days', 'floating_nav_autohide_streamdeck', 'floating_nav_position', 'full-restore-source',
            'gemini_api_key', 'gemini_model', 'gemini_model_sel', 'grocy_api_key',
            'grocy_base_url', 'grocy_public_url', 'ha_camera_popup_seconds', 'ha_events_device',
            'ha_events_enabled', 'has_streamdeck', 'kms_rotation', 'mealie_api_key',
            'mealie_base_url', 'mealie_public_url', 'nav_visibility', 'new_hostname',
            'ollama_base_url', 'ollama_model', 'ollama_model_sel', 'openai_api_key',
            'openai_model', 'openai_model_sel', 'osk_enabled', 'perishable_days',
            'qr_public_url', 'qr_url_mode', 'quiet_mode', 'rclone_remote',
            'rclone_schedule_hours', 'recipe_source', 'restore-file', 'scan_cidr',
            'scanner-test-input', 'scanner_type', 'scheduled_reboot_day', 'scheduled_reboot_frequency',
            'scheduled_reboot_time', 'screensaver_all_clients', 'screensaver_minutes', 'screensaver_mode',
            'screensaver_speed', 'sd-profile-name-input', 'sd-profile-select', 'settings-search',
            'spoonacular_api_key', 'staple_items', 'start_icon_color', 'start_key_style',
            'start_page_enabled', 'start_page_keys', 'streamdeck_brightness', 'streamdeck_ha_base_url',
            'streamdeck_ha_token', 'streamdeck_icon_color', 'streamdeck_idle_timeout', 'streamdeck_key_count',
            'streamdeck_key_style', 'streamdeck_logo_when_display_off', 'streamdeck_rotation', 'streamdeck_weather_location',
            'streamdeck_weather_units', 'suggest_per_tier', 'switch_server_url', 'switch_upstream_api_key',
            'themealdb_api_key', 'timezone', 'totp-code', 'tunnel_mode_cloudflare',
            'tunnel_mode_off', 'tunnel_mode_subscription', 'tunnel_token', 'ui_scale',
            'ui_theme', 'usb_backup_interval_hours', 'vision_provider', 'wake_on_motion',
            'wifi_password', 'wifi_ssid',
        ],
        "targets": [
            '#pane-advanced', '#pane-appearance', '#pane-backups',
            '#pane-connections', '#pane-devices', '#pane-inventory',
            '#pane-personalization-recipes', '#pane-scanning', '#pane-screen',
            '#pane-security', '#pane-start-page',
        ],
    },
    "pi_remote": {
        "ids": [
            'anthropic_api_key', 'anthropic_model', 'anthropic_model_sel', 'api_key',
            'appliance_air_fryer', 'appliance_blender', 'appliance_bread_machine', 'appliance_bun_steamer',
            'appliance_cast_iron', 'appliance_deep_fryer', 'appliance_dehydrator', 'appliance_dishwasher',
            'appliance_dutch_oven', 'appliance_food_processor', 'appliance_freezer', 'appliance_garlic_press',
            'appliance_griddle', 'appliance_grill', 'appliance_hand_mixer', 'appliance_ice_cream_maker',
            'appliance_immersion_blender', 'appliance_kitchen_scale', 'appliance_mandoline', 'appliance_meat_thermometer',
            'appliance_microplane', 'appliance_microwave', 'appliance_mortar_pestle', 'appliance_oven',
            'appliance_panini_press', 'appliance_pasta_extruder', 'appliance_pasta_roller', 'appliance_pastry_bag',
            'appliance_pizza_stone', 'appliance_pressure_cooker', 'appliance_refrigerator', 'appliance_rice_cooker',
            'appliance_rolling_pin', 'appliance_slow_cooker', 'appliance_sm_food_processor', 'appliance_sm_grain_mill',
            'appliance_sm_ice_cream_maker', 'appliance_sm_meat_grinder', 'appliance_sm_pasta_roller_cutter', 'appliance_sm_sausage_stuffer',
            'appliance_sm_spiralizer', 'appliance_smoker', 'appliance_sous_vide', 'appliance_spiralizer',
            'appliance_stand_mixer', 'appliance_stove', 'appliance_toaster', 'appliance_toaster_oven',
            'appliance_torch', 'appliance_waffle_iron', 'appliance_wok', 'auth_password',
            'auth_required', 'auto_update', 'update_channel', 'background_file', 'background_image_url',
            'background_opacity', 'backup_include_secrets', 'barcode_autocheck_shopping', 'barcode_enrichment',
            'barcode_global_capture', 'barcode_llm_fallback',
            # Added after the pin: the Pantry Raider Cloud pairing input. Shown
            # on a satellite too, since every install pairs itself
            # (FoodAssistant-2nd1).
            'cloud_pairing_code',
            'cook_ai_context', 'custom-heading-icon',
            'custom-heading-label', 'custom-tab-icon', 'custom-tab-label', 'custom-tab-url',
            'custom_theme_accent', 'custom_theme_base', 'custom_theme_bg', 'custom_theme_name',
            'custom_theme_primary', 'custom_theme_surface', 'custom_theme_text', 'debug_logging',
            'device_hostname', 'display_idle_timeout', 'display_touch', 'display_type',
            'enrich_model', 'enrich_model_sel', 'enrich_provider', 'expiring_soon_days',
            'floating_nav_autohide_streamdeck', 'floating_nav_position', 'full-restore-source', 'gemini_api_key',
            'gemini_model', 'gemini_model_sel', 'grocy_api_key', 'grocy_base_url',
            'grocy_public_url', 'ha_camera_popup_seconds', 'ha_events_device', 'ha_events_enabled',
            'has_streamdeck', 'kiosk_pin', 'kiosk_readonly_when_locked', 'kms_rotation',
            'mealie_api_key', 'mealie_base_url', 'mealie_public_url', 'nav_visibility',
            'new_hostname', 'ollama_base_url', 'ollama_model', 'ollama_model_sel',
            'openai_api_key', 'openai_model', 'openai_model_sel', 'osk_enabled',
            'perishable_days', 'qr_public_url', 'qr_url_mode', 'quiet_mode',
            'rclone_remote', 'rclone_schedule_hours', 'recipe_source', 'remote_server_url',
            'restore-file', 'scanner-test-input', 'scanner_type', 'scheduled_reboot_day',
            'scheduled_reboot_frequency', 'scheduled_reboot_time', 'screensaver_all_clients', 'screensaver_minutes',
            'screensaver_mode', 'screensaver_speed', 'sd-profile-name-input', 'sd-profile-select',
            'settings-search', 'spoonacular_api_key', 'staple_items', 'start_icon_color',
            'start_key_style', 'start_page_enabled', 'start_page_keys', 'streamdeck_brightness',
            'streamdeck_ha_base_url', 'streamdeck_ha_token', 'streamdeck_icon_color', 'streamdeck_idle_timeout',
            'streamdeck_key_count', 'streamdeck_key_style', 'streamdeck_logo_when_display_off', 'streamdeck_rotation',
            'streamdeck_weather_location', 'streamdeck_weather_units', 'suggest_per_tier', 'themealdb_api_key',
            'totp-code', 'ui_scale', 'ui_theme', 'upstream_api_key',
            'usb_backup_interval_hours', 'vision_provider', 'wake_on_motion', 'wifi_password',
            'wifi_ssid',
        ],
        "targets": [
            '#pane-advanced', '#pane-appearance', '#pane-backups',
            '#pane-connections', '#pane-devices', '#pane-personalization-recipes',
            '#pane-scanning', '#pane-screen', '#pane-security',
            '#pane-start-page',
        ],
    },
    "wizard": {
        "ids": [
            'anthropic_api_key', 'anthropic_model', 'anthropic_model_sel', 'api_key',
            'auth_password', 'auth_required', 'display_rotation_wiz', 'display_touch',
            'display_type_wiz', 'gemini_api_key', 'gemini_model', 'gemini_model_sel',
            'grocy_api_key', 'grocy_base_url', 'grocy_public_url', 'has_streamdeck',
            'kiosk_pin', 'mealie_api_key', 'mealie_base_url', 'mealie_public_url',
            'ollama_base_url', 'ollama_model', 'ollama_model_sel', 'openai_api_key',
            'openai_model', 'openai_model_sel', 'recipe_source_wiz', 'remote_server_url',
            'spoonacular_api_key_wiz', 'streamdeck_key_count', 'ui_scale_wiz', 'upstream_api_key',
            'vision_provider', 'wiz-scanner-test-input', 'wiz-scanner_type', 'wiz_has_display',
        ],
        "targets": [
            '#mealie-collapse', '#recipes-collapse', '#scanner-collapse',
            '#tunnel-wiz-collapse',
        ],
    },
}


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


def _render(client, monkeypatch, *, mode: str, is_pi: bool, configured: bool) -> str:
    monkeypatch.setattr(settings, "deployment_mode", mode)
    with patch.object(type(settings), "is_configured", lambda self: configured), \
         patch("app.routers.setup.is_raspberry_pi", return_value=is_pi), \
         patch("app.templating.is_raspberry_pi", return_value=is_pi):
        r = client.get("/setup")
    assert r.status_code == 200
    return r.text


def _dom_features(html: str) -> tuple[set, set]:
    """Form-control ids and pill targets from the page markup, scripts stripped."""
    dom = _SCRIPT_RE.sub("", html)
    ids = set(re.findall(r'<(?:input|select|textarea)\b[^>]*\bid="([^"]+)"', dom))
    targets = set(re.findall(r'data-bs-target="([^"]+)"', dom))
    return ids, targets


def test_rendered_controls_match_pre_refactor_pin(client, monkeypatch):
    for shape, (mode, is_pi, configured) in SHAPES.items():
        html = _render(client, monkeypatch, mode=mode, is_pi=is_pi,
                       configured=configured)
        ids, targets = _dom_features(html)
        want_ids = set(PINNED[shape]["ids"])
        want_targets = set(PINNED[shape]["targets"])
        assert ids == want_ids, (
            shape,
            "missing", sorted(want_ids - ids),
            "unexpected", sorted(ids - want_ids),
        )
        assert targets == want_targets, (
            shape,
            "missing", sorted(want_targets - targets),
            "unexpected", sorted(targets - want_targets),
        )
