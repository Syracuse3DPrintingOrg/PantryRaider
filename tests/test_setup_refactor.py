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
            'auth_password',
            # Added after the pin: the optional viewer password (kitchen-only
            # login, security review Jul 2026).
            'viewer_password',
            # Added after the pin: the confirm-new-password input in the
            # change-password block (FoodAssistant-tdz3). Client-side check only,
            # not a saveable setting.
            'auth_password_confirm',
            'auth_required', 'auto_update', 'update_channel', 'background_file',
            'background_image_url', 'background_opacity', 'backup_include_secrets', 'barcode_autocheck_shopping',
            'barcode_enrichment', 'barcode_global_capture', 'barcode_llm_fallback', 'cam-ip-host',
            'cam-ip-name', 'cam-ip-pass', 'cam-ip-path', 'cam-ip-port',
            'cam-ip-preset', 'cam-ip-user', 'cam-scan-cidr',
            # Added after the pin: Add from Frigate discovery (FoodAssistant-7ror)
            # and the Add a Reolink camera form (FoodAssistant-qft4), both on the
            # Connections pane, main-install only.
            'cam-frigate-url',
            'cam-reo-name', 'cam-reo-host', 'cam-reo-port', 'cam-reo-channel',
            'cam-reo-user', 'cam-reo-pass', 'cam-reo-quality',
            # Added after the pin: the Pantry Raider Cloud pairing input
            # (FoodAssistant-2nd1).
            'cloud_pairing_code',
            # Added after the pin: the Forager account sign-in fields
            # (FoodAssistant-t6ab).
            'cloud_email',
            'cloud_kitchen_name',
            'cloud_password',
            # Added after the pin: the 2FA code field, revealed when a Forager
            # account with two-factor sign-in asks for it (FoodAssistant-nbu9).
            'cloud_totp',
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
            'rclone_remote', 'rclone_schedule_hours', 'recipe_source',
            'shopping_backend',  # post-pin: shopping-list backend (FoodAssistant-g0fd)
            'restore-file',
            'scan_cidr', 'scanner-test-input', 'scanner_type', 'screensaver_all_clients',
            'screensaver_minutes', 'screensaver_mode', 'screensaver_speed',
            'screensaver_pill_scale',
            'screensaver_photo_seconds',  # post-pin: photo interval
            'screensaver_ken_burns',  # post-pin: ken burns toggle  # post-pin addition (pill size setting)
            'screensaver_ken_burns_speed',  # post-pin: ken burns pan/zoom speed (FoodAssistant-nz62)
            # Added after the pin: the photo screensaver source fields
            # (folder, web addresses, or an Immich album).
            'photo_source', 'photo_folder', 'photo_urls',
            'immich_base_url', 'immich_api_key', 'immich_album_id',
            'settings-search',
            'spoonacular_api_key', 'staple_items', 'start_icon_color', 'start_key_style',
            'start_page_enabled', 'start_page_keys', 'streamdeck_ha_base_url', 'streamdeck_ha_token',
            'streamdeck_weather_location', 'streamdeck_weather_units', 'suggest_per_tier', 'themealdb_api_key', 'timer_chips',
            # Added after the pin: the 12/24-hour clock format select in the
            # Date & time card (FoodAssistant-v3ui).
            'clock_format',
            'timezone', 'totp-code', 'tunnel_mode_cloudflare', 'tunnel_mode_off',
            'tunnel_token', 'ui_theme', 'usb_backup_interval_hours',
            'vision_provider',
            # Added after the pin: the chosen Forager web address (subdomain)
            # input in the Remote access card (FoodAssistant-w2mv).
            'tunnel_subdomain',
            # Renamed after the pin: the remote-access radios now offer a
            # Forager mode in place of the disabled "Pantry Raider Cloud"
            # (subscription) placeholder, moved to the Forager pane.
            'tunnel_mode_forager',
            # Added after the pin: the Printing pane (FoodAssistant-fb8x):
            # master toggle, printer queues, label size, decorative-label text.
            # A main install (server / Pi Hosted) picks the FLEET default queues
            # every device inherits (FoodAssistant-7u7z), so the selectors here are
            # the fleet_* ids, not this device's local override.
            'printing_enabled', 'fleet_label_printer_queue', 'fleet_document_printer_queue',
            'label_width_in', 'label_height_in', 'label_dpi', 'decorative-text',
            # Label designer + format chooser (FoodAssistant-or5e / -bwl1).
            'label_format', 'ld-insp-text', 'ld-insp-size', 'ld-insp-bold',
            'ld-insp-upper',
            # Added after the pin: the optional black-and-white logo on printed
            # food labels (FoodAssistant-yglw), and the advanced document
            # printer settings (page size, color, duplex; FoodAssistant-7xo5).
            # Both live with the label size/design panel, device-local like it.
            'label_show_logo',
            'document_page_size', 'document_color_mode', 'document_duplex',
            # Added after the pin: the Thermometers pane (FoodAssistant-mnks):
            # the feature toggle, the add-by-address form, and the Home
            # Assistant source (toggle + entity picker). Device-local, so the
            # same controls render in every mode.
            'gadgets_enabled', 'gadget-add-id', 'gadget-add-name',
            'gadget-add-protocol', 'gadget_ha_enabled', 'gadget-ha-entity',
            # Added after the pin: the "only show sensors with a current
            # reading" filter checkbox for the HA entity picker (FoodAssistant-yryl).
            'gadget-ha-hide-empty',
        ],
        "targets": [
            '#pane-advanced', '#pane-appearance', '#pane-backups',
            '#pane-connections', '#pane-devices', '#pane-inventory',
            '#pane-personalization-recipes', '#pane-scanning', '#pane-screen',
            '#pane-security', '#pane-start-page',
            # Added after the pin: the Printing pane (FoodAssistant-fb8x).
            '#pane-printing',
            # Added after the pin: the Thermometers pane (FoodAssistant-mnks).
            '#pane-gadgets',
            # Added after the pin: the Forager card's Advanced (pairing code)
            # toggle (FoodAssistant-t6ab).
            '#cloud-advanced-collapse',
            # Added after the pin: the dedicated Forager pane (sign-in + remote
            # access), main-install only.
            '#pane-forager',
            # Added after the pin: the settings IA reorg (FoodAssistant-s6q,
            # -42n4) gave Home Assistant and Network their own panes.
            '#pane-home-assistant',
            '#pane-network',
            # Added after the pin: the Stripe-style Settings rebuild
            # (FoodAssistant-jcnh) added the Overview landing pane + its menu pill.
            '#pane-overview',
            # Added after the pin: the Status health dashboard pane + its menu
            # pill (FoodAssistant-w00b), right below Overview.
            '#pane-status',
            # Added after the pin: the Resources pane of live hardware
            # readings (CPU, memory, storage, temperature, power), right
            # below Status (FoodAssistant-do2u).
            '#pane-resources',
            # Added after the pin: the recipe taste tuning split into its own
            # Recipe suggestions pane, apart from the Recipes connection
            # settings, so saving tastes cannot overwrite them
            # (FoodAssistant-ysj1).
            '#pane-recipe-tuning',
            # Added after the pin: advanced/command-line material moved behind
            # collapse disclosures (FoodAssistant-btep). The command-line update
            # block is server-only (Pi appliances update in-app); Maintenance
            # and Diagnostics render in every mode.
            '#update-commands-collapse',
            '#maintenance-collapse',
            '#diagnostics-collapse',
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
            'auth_password',
            # Added after the pin: the optional viewer password (kitchen-only
            # login, security review Jul 2026).
            'viewer_password',
            # Added after the pin: the confirm-new-password input in the
            # change-password block (FoodAssistant-tdz3). Client-side check only,
            # not a saveable setting.
            'auth_password_confirm',
            'auth_required', 'auto_update', 'update_channel', 'background_file',
            'background_image_url', 'background_opacity', 'backup_include_secrets', 'barcode_autocheck_shopping',
            'barcode_enrichment', 'barcode_global_capture', 'barcode_llm_fallback', 'cam-ip-host',
            'cam-ip-name', 'cam-ip-pass', 'cam-ip-path', 'cam-ip-port',
            'cam-ip-preset', 'cam-ip-user', 'cam-scan-cidr',
            # Added after the pin: Add from Frigate discovery (FoodAssistant-7ror)
            # and the Add a Reolink camera form (FoodAssistant-qft4), both on the
            # Connections pane, main-install only.
            'cam-frigate-url',
            'cam-reo-name', 'cam-reo-host', 'cam-reo-port', 'cam-reo-channel',
            'cam-reo-user', 'cam-reo-pass', 'cam-reo-quality',
            # Added after the pin: the Pantry Raider Cloud pairing input
            # (FoodAssistant-2nd1).
            'cloud_pairing_code',
            # Added after the pin: the Forager account sign-in fields
            # (FoodAssistant-t6ab).
            'cloud_email',
            'cloud_kitchen_name',
            'cloud_password',
            # Added after the pin: the 2FA code field, revealed when a Forager
            # account with two-factor sign-in asks for it (FoodAssistant-nbu9).
            'cloud_totp',
            'cook_ai_context',
            'custom-heading-icon', 'custom-heading-label', 'custom-tab-icon', 'custom-tab-label',
            'custom-tab-url', 'custom_theme_accent', 'custom_theme_base', 'custom_theme_bg',
            'custom_theme_name', 'custom_theme_primary', 'custom_theme_surface', 'custom_theme_text',
            'debug_logging', 'device_hostname', 'display_idle_timeout', 'display_touch',
            'display_type', 'enrich_model', 'enrich_model_sel', 'enrich_provider',
            'expiring_soon_days', 'floating_nav_autohide_streamdeck', 'floating_nav_position', 'full-restore-source',
            'gemini_api_key', 'gemini_model', 'gemini_model_sel', 'grocy_api_key',
            'grocy_base_url', 'grocy_public_url', 'ha_camera_popup_seconds', 'ha_events_device',
            'ha_events_enabled', 'has_streamdeck',
            # Added after the pin: the hardware-preset selector on the Screen &
            # Sleep pane (FoodAssistant-kl5n)
            'hw_preset',
            'kms_rotation', 'mealie_api_key',
            'mealie_base_url', 'mealie_public_url', 'nav_visibility', 'new_hostname',
            'ollama_base_url', 'ollama_model', 'ollama_model_sel', 'openai_api_key',
            'openai_model', 'openai_model_sel', 'osk_enabled', 'perishable_days',
            'qr_public_url', 'qr_url_mode', 'quiet_mode', 'rclone_remote',
            'rclone_schedule_hours', 'recipe_source',
            'shopping_backend',  # post-pin: shopping-list backend (FoodAssistant-g0fd)
            'restore-file', 'scan_cidr',
            'scanner-test-input', 'scanner_type', 'scheduled_reboot_day', 'scheduled_reboot_frequency',
            'scheduled_reboot_time', 'screensaver_all_clients', 'screensaver_minutes', 'screensaver_mode',
            'screensaver_speed',
            'screensaver_pill_scale',
            'screensaver_photo_seconds',  # post-pin: photo interval
            'screensaver_ken_burns',  # post-pin: ken burns toggle  # post-pin addition (pill size setting)
            'screensaver_ken_burns_speed',  # post-pin: ken burns pan/zoom speed (FoodAssistant-nz62)
            # Added after the pin: the photo screensaver source fields
            # (folder, web addresses, or an Immich album).
            'photo_source', 'photo_folder', 'photo_urls',
            'immich_base_url', 'immich_api_key', 'immich_album_id',
            'sd-profile-name-input', 'sd-profile-select', 'settings-search',
            'spoonacular_api_key', 'staple_items', 'start_icon_color', 'start_key_style',
            'start_page_enabled', 'start_page_keys', 'streamdeck_brightness', 'streamdeck_ha_base_url',
            'streamdeck_ha_token', 'streamdeck_icon_color', 'streamdeck_idle_timeout', 'streamdeck_key_count',
            'streamdeck_key_style', 'streamdeck_logo_when_display_off', 'streamdeck_rotation', 'streamdeck_weather_location',
            'streamdeck_weather_units', 'suggest_per_tier', 'switch_server_url', 'switch_upstream_api_key',
            # Added after the pin: the 12/24-hour clock format select in the
            # Date & time card (FoodAssistant-v3ui).
            'clock_format',
            'themealdb_api_key', 'timer_chips', 'timezone', 'totp-code', 'tunnel_mode_cloudflare',
            'tunnel_mode_off', 'tunnel_token', 'ui_scale',
            # Added after the pin: the chosen Forager web address (subdomain)
            # input in the Remote access card (FoodAssistant-w2mv).
            'tunnel_subdomain',
            'ui_theme', 'usb_backup_interval_hours', 'vision_provider', 'wake_on_motion',
            'wifi_password', 'wifi_ssid',
            # Renamed after the pin: the remote-access Forager radio replaces the
            # disabled "Pantry Raider Cloud" (subscription) placeholder, moved to
            # the Forager pane.
            'tunnel_mode_forager',
            # Added after the pin: the Printing pane (FoodAssistant-fb8x). Pi
            # Hosted is a main server for its satellites, so it picks the FLEET
            # default queues too (FoodAssistant-7u7z): the fleet_* ids.
            'printing_enabled', 'fleet_label_printer_queue', 'fleet_document_printer_queue',
            'label_width_in', 'label_height_in', 'label_dpi', 'decorative-text',
            # Label designer + format chooser (FoodAssistant-or5e / -bwl1).
            'label_format', 'ld-insp-text', 'ld-insp-size', 'ld-insp-bold',
            'ld-insp-upper',
            # Added after the pin: the optional black-and-white logo on printed
            # food labels (FoodAssistant-yglw), and the advanced document
            # printer settings (page size, color, duplex; FoodAssistant-7xo5).
            # Both live with the label size/design panel, device-local like it.
            'label_show_logo',
            'document_page_size', 'document_color_mode', 'document_duplex',
            # Added after the pin: the Thermometers pane (FoodAssistant-mnks):
            # the feature toggle, the add-by-address form, and the Home
            # Assistant source (toggle + entity picker). Device-local, so the
            # same controls render in every mode.
            'gadgets_enabled', 'gadget-add-id', 'gadget-add-name',
            'gadget-add-protocol', 'gadget_ha_enabled', 'gadget-ha-entity',
            # Added after the pin: the "only show sensors with a current
            # reading" filter checkbox for the HA entity picker (FoodAssistant-yryl).
            'gadget-ha-hide-empty',
        ],
        "targets": [
            '#pane-advanced', '#pane-appearance', '#pane-backups',
            '#pane-connections', '#pane-devices', '#pane-inventory',
            '#pane-personalization-recipes', '#pane-scanning', '#pane-screen',
            '#pane-security', '#pane-start-page',
            # Added after the pin: the Printing pane (FoodAssistant-fb8x).
            '#pane-printing',
            # Added after the pin: the Thermometers pane (FoodAssistant-mnks).
            '#pane-gadgets',
            # Added after the pin: the Forager card's Advanced (pairing code)
            # toggle (FoodAssistant-t6ab).
            '#cloud-advanced-collapse',
            # Added after the pin: the dedicated Forager pane (sign-in + remote
            # access), main-install only.
            '#pane-forager',
            # Added after the pin: the settings IA reorg (FoodAssistant-s6q,
            # -42n4) gave Home Assistant and Network their own panes.
            '#pane-home-assistant',
            '#pane-network',
            # Added after the pin: the Stripe-style Settings rebuild
            # (FoodAssistant-jcnh) added the Overview landing pane + its menu pill.
            '#pane-overview',
            # Added after the pin: the Status health dashboard pane + its menu
            # pill (FoodAssistant-w00b), right below Overview.
            '#pane-status',
            # Added after the pin: the Resources pane of live hardware
            # readings (CPU, memory, storage, temperature, power), right
            # below Status (FoodAssistant-do2u).
            '#pane-resources',
            # Added after the pin: the recipe taste tuning split into its own
            # Recipe suggestions pane, apart from the Recipes connection
            # settings, so saving tastes cannot overwrite them
            # (FoodAssistant-ysj1).
            '#pane-recipe-tuning',
            # Added after the pin: advanced/command-line disclosures
            # (FoodAssistant-btep). A Pi Hosted box also gets the satellite-mode
            # switch panel, likewise collapsed.
            '#satellite-switch-collapse',
            '#maintenance-collapse',
            '#diagnostics-collapse',
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
            # Added after the pin: the optional viewer password (kitchen-only
            # login, security review Jul 2026).
            'viewer_password',
            # Added after the pin: the confirm-new-password input in the
            # change-password block (FoodAssistant-tdz3). Client-side check only,
            # not a saveable setting.
            'auth_password_confirm',
            'auth_required', 'auto_update', 'update_channel', 'background_file', 'background_image_url',
            'background_opacity', 'backup_include_secrets', 'barcode_autocheck_shopping', 'barcode_enrichment',
            'barcode_global_capture', 'barcode_llm_fallback',
            # The Forager sign-in fields (cloud_email/password/kitchen_name) and
            # the pairing input (cloud_pairing_code) moved to the dedicated
            # Forager pane, which is main-install only: a satellite forwards AI
            # from the main server and has no local app of its own to sign in or
            # expose, so none of them render here now.
            'cook_ai_context', 'custom-heading-icon',
            'custom-heading-label', 'custom-tab-icon', 'custom-tab-label', 'custom-tab-url',
            'custom_theme_accent', 'custom_theme_base', 'custom_theme_bg', 'custom_theme_name',
            'custom_theme_primary', 'custom_theme_surface', 'custom_theme_text', 'debug_logging',
            'device_hostname', 'display_idle_timeout', 'display_touch', 'display_type',
            'enrich_model', 'enrich_model_sel', 'enrich_provider', 'expiring_soon_days',
            'floating_nav_autohide_streamdeck', 'floating_nav_position', 'full-restore-source', 'gemini_api_key',
            'gemini_model', 'gemini_model_sel', 'grocy_api_key', 'grocy_base_url',
            'grocy_public_url', 'ha_camera_popup_seconds', 'ha_events_device', 'ha_events_enabled',
            'has_streamdeck',
            # Added after the pin: the hardware-preset selector on the Screen &
            # Sleep pane (FoodAssistant-kl5n)
            'hw_preset',
            'kiosk_pin', 'kiosk_readonly_when_locked', 'kms_rotation',
            'mealie_api_key', 'mealie_base_url', 'mealie_public_url', 'nav_visibility',
            'new_hostname', 'ollama_base_url', 'ollama_model', 'ollama_model_sel',
            'openai_api_key', 'openai_model', 'openai_model_sel', 'osk_enabled',
            'perishable_days', 'qr_public_url', 'qr_url_mode', 'quiet_mode',
            'rclone_remote', 'rclone_schedule_hours', 'recipe_source',
            'shopping_backend',  # post-pin: shopping-list backend (FoodAssistant-g0fd)
            'remote_server_url',
            'restore-file', 'scanner-test-input', 'scanner_type', 'scheduled_reboot_day',
            'scheduled_reboot_frequency', 'scheduled_reboot_time', 'screensaver_all_clients', 'screensaver_minutes',
            'screensaver_mode', 'screensaver_speed',
            'screensaver_pill_scale',
            'screensaver_photo_seconds',  # post-pin: photo interval
            'screensaver_ken_burns',  # post-pin: ken burns toggle  # post-pin addition (pill size setting)
            'screensaver_ken_burns_speed',  # post-pin: ken burns pan/zoom speed (FoodAssistant-nz62)
            # Added after the pin: the photo screensaver source fields
            # (folder, web addresses, or an Immich album).
            'photo_source', 'photo_folder', 'photo_urls',
            'immich_base_url', 'immich_api_key', 'immich_album_id',
            'sd-profile-name-input', 'sd-profile-select',
            'settings-search', 'spoonacular_api_key', 'staple_items', 'start_icon_color',
            'start_key_style', 'start_page_enabled', 'start_page_keys', 'streamdeck_brightness',
            'streamdeck_ha_base_url', 'streamdeck_ha_token', 'streamdeck_icon_color', 'streamdeck_idle_timeout',
            'streamdeck_key_count', 'streamdeck_key_style', 'streamdeck_logo_when_display_off', 'streamdeck_rotation',
            'streamdeck_weather_location', 'streamdeck_weather_units', 'suggest_per_tier', 'themealdb_api_key',
            'streamdeck_key_count', 'streamdeck_key_style', 'streamdeck_rotation',
            'streamdeck_weather_location', 'streamdeck_weather_units', 'suggest_per_tier', 'themealdb_api_key', 'timer_chips',
            'totp-code', 'ui_scale', 'ui_theme', 'upstream_api_key',
            'usb_backup_interval_hours', 'vision_provider', 'wake_on_motion', 'wifi_password',
            'wifi_ssid',
            # Added after the pin: the Printing pane (FoodAssistant-fb8x). On a
            # satellite the printer is system-level (chosen on the main server),
            # so there are no per-device printer selects, and label size + the
            # label designer live on the server too (FoodAssistant-eml9). The
            # satellite keeps the feature toggle and the decorative-label print
            # box (a print action, which relays to the server).
            'printing_enabled', 'decorative-text',
            # Added after the pin: the Thermometers pane (FoodAssistant-mnks):
            # the feature toggle, the add-by-address form, and the Home
            # Assistant source (toggle + entity picker). Device-local, so the
            # same controls render in every mode.
            'gadgets_enabled', 'gadget-add-id', 'gadget-add-name',
            'gadget-add-protocol', 'gadget_ha_enabled', 'gadget-ha-entity',
            # Added after the pin: the "only show sensors with a current
            # reading" filter checkbox for the HA entity picker (FoodAssistant-yryl).
            'gadget-ha-hide-empty',
        ],
        "targets": [
            '#pane-advanced', '#pane-appearance', '#pane-backups',
            '#pane-connections', '#pane-devices', '#pane-personalization-recipes',
            '#pane-scanning', '#pane-screen', '#pane-security',
            '#pane-start-page',
            # Added after the pin: the Printing pane (FoodAssistant-fb8x).
            '#pane-printing',
            # Added after the pin: the Thermometers pane (FoodAssistant-mnks).
            '#pane-gadgets',
            # Added after the pin: the settings IA reorg (FoodAssistant-s6q,
            # -42n4) gave Home Assistant and Network their own panes. Both
            # render on a satellite (HA read-only synced; Network holds its
            # Wi-Fi, hostname, and link name).
            '#pane-home-assistant',
            '#pane-network',
            # Added after the pin: the Stripe-style Settings rebuild
            # (FoodAssistant-jcnh) added the Overview landing pane + its menu pill.
            '#pane-overview',
            # Added after the pin: the Status health dashboard pane + its menu
            # pill (FoodAssistant-w00b), right below Overview.
            '#pane-status',
            # Added after the pin: the Resources pane of live hardware
            # readings (CPU, memory, storage, temperature, power), right
            # below Status (FoodAssistant-do2u).
            '#pane-resources',
            # Added after the pin: the recipe taste tuning split into its own
            # Recipe suggestions pane (FoodAssistant-ysj1). It renders on a
            # satellite too, read-only, like the Recipes pane.
            '#pane-recipe-tuning',
            # The Forager pane (and its #cloud-advanced-collapse toggle) is
            # main-install only, so a satellite renders neither.
            # Added after the pin: advanced/command-line disclosures
            # (FoodAssistant-btep). Maintenance and Diagnostics render here too;
            # the command-line update block does not (a satellite updates
            # in-app), and Return to full stack only shows when a parked stack
            # exists, which the test render has not.
            '#maintenance-collapse',
            '#diagnostics-collapse',
        ],
    },
    "wizard": {
        "ids": [
            'anthropic_api_key', 'anthropic_model', 'anthropic_model_sel', 'api_key',
            'auth_password', 'auth_required', 'display_rotation_wiz', 'display_touch',
            'display_type_wiz', 'gemini_api_key', 'gemini_model', 'gemini_model_sel',
            'grocy_api_key', 'grocy_base_url', 'grocy_public_url', 'has_streamdeck',
            # Added after the pin: the wizard hardware step's preset selector
            # (FoodAssistant-kl5n)
            'hw_preset_wiz',
            'kiosk_pin', 'mealie_api_key', 'mealie_base_url', 'mealie_public_url',
            'ollama_base_url', 'ollama_model', 'ollama_model_sel', 'openai_api_key',
            'openai_model', 'openai_model_sel', 'recipe_source_wiz', 'remote_server_url',
            'spoonacular_api_key_wiz', 'streamdeck_key_count', 'ui_scale_wiz', 'upstream_api_key',
            'vision_provider', 'wiz-scanner-test-input', 'wiz-scanner_type', 'wiz_has_display',
            # Added after the pin: the wizard's Forager sign-in fields
            # (FoodAssistant-t6ab).
            'wiz_cloud_email',
            'wiz_cloud_kitchen_name',
            'wiz_cloud_password',
            # Added after the pin: the wizard 2FA code field, revealed when a
            # Forager account with two-factor sign-in asks for it
            # (FoodAssistant-nbu9).
            'wiz_cloud_totp',
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


def test_confirm_new_password_renders_in_change_block(client, monkeypatch):
    """The change-password block carries a Confirm new password input next to
    the new-password field (FoodAssistant-tdz3)."""
    html = _render(client, monkeypatch, mode="server", is_pi=False,
                   configured=True)
    block = html.split('id="change-password-block"', 1)
    assert len(block) == 2, "change-password block missing"
    # The confirm input sits inside the change block, right after auth_password.
    assert 'id="auth_password_confirm"' in block[1]
    ap = block[1].index('id="auth_password"')
    apc = block[1].index('id="auth_password_confirm"')
    assert ap < apc, "confirm field should follow the new-password field"


def test_confirm_field_is_not_a_saveable_setting():
    """auth_password_confirm is a client-side check only: it is not accepted by
    the save payload and is not a persistable setting (FoodAssistant-tdz3)."""
    from app.routers.setup import SetupPayload
    from app.config import _SAVEABLE

    assert "auth_password_confirm" not in SetupPayload.model_fields
    assert "auth_password_confirm" not in _SAVEABLE
    # A stray post of the confirm field is dropped by the model (extra ignored),
    # so it can never reach the settings store.
    data = SetupPayload(auth_password_confirm="typo").model_dump(exclude_unset=True)
    assert "auth_password_confirm" not in data
