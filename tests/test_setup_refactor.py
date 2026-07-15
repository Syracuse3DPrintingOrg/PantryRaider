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
            # Added after the pin: kiosk auto-return-to-home (FoodAssistant-6e5m),
            # on the Screen pane in every mode (Pi and non-Pi kiosks alike).
            'kiosk_auto_home_enabled', 'kiosk_auto_home_seconds',
            'ai_token_budget', 'anthropic_api_key', 'anthropic_model', 'anthropic_model_sel',
            # Removed after the pin: the legacy primary API key row renders only
            # when an api_key is already stored; new installs use named keys
            # (FoodAssistant-f8kp), and the pin fixture has no stored key.
            'appliance_air_fryer', 'appliance_blender', 'appliance_bread_machine',
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
            'auth_required', 'auto_update', 'update_channel',
            # Added after the pin: the device-local off switch for the
            # automatic/background update check (FoodAssistant-31v4).
            'check_for_updates',
            'background_file',
            'background_image_url', 'background_opacity', 'backup_include_secrets', 'barcode_autocheck_shopping',
            'barcode_enrichment', 'barcode_global_capture', 'barcode_llm_fallback',
            # Added after the pin: the optional Beszel monitoring hub link on the
            # Resources pane (FoodAssistant-4kz2).
            'beszel_enabled', 'beszel_url',
            'cam-ip-host',
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
            # Added after the pin: the Bandit Cubs section in the Devices pane
            # (FoodAssistant-bzqj): fleet-wide Cub content settings, main
            # installs only (a satellite's Cubs pair with the main server).
            'cub_default_view', 'cub_timers_take_over', 'cub_probes_take_over',
            'cub_rotate_seconds', 'cub_poll_seconds',
            # Added after the pin: the alerts takeover toggle (FoodAssistant-5c61).
            'cub_alerts_take_over',
            # Added after the pin: the Bluetooth status beacon toggle
            # (FoodAssistant-yl6u, UI landed with FoodAssistant-guwc). Shown in
            # every main-install mode: on a radio-less server the flag is what
            # a Bandit's reader broadcasts with.
            'cub_ble_advertise',
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
            # Added after the pin: the LAN device pairing toggle on the Security
            # pane (FoodAssistant-4box). Main installs only: a satellite hands
            # out no keys, so the control does not render there.
            'local_device_pairing_enabled',
            'restore-file',
            # Added after the pin: the Community shelf life panel on the
            # Inventory pane (FoodAssistant-ezkh): the use-community-estimates
            # toggle and the anonymous sharing opt-in. Main installs only.
            'share_expiry_learning', 'use_community_expiry',
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
            # start_page_mode: the Glance/Custom home-style select (FoodAssistant-gg33).
            'start_page_mode',
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
            # Added after the pin: the round/square label shape selector
            # in the label size panel (FoodAssistant-bprm).
            'label_shape',
            'document_page_size', 'document_color_mode', 'document_duplex',
            # Added after the pin: outline/border toggle and saved layout
            # presets in the field designer (FoodAssistant-rhqa), plus a
            # decorative-label symbol/outline pair, an icon element and QR
            # payload kind in the designer, and the decorative label's own
            # symbol picker (FoodAssistant-nxr8). The designer controls are
            # server/pi_hosted only (label size + designer are hidden on
            # pi_remote, FoodAssistant-eml9); the decorative-label ones render
            # everywhere printing does.
            'ld-insp-outline', 'ld-preset-select', 'ld-preset-name',
            'ld-insp-icon', 'ld-insp-qr-kind', 'ld-insp-qr-note',
            'decorative-icon', 'decorative-outline',
            # Added after the pin: the Thermometers pane (FoodAssistant-mnks):
            # the feature toggle, the add-by-address form, and the Home
            # Assistant source (toggle + entity picker). Device-local, so the
            # same controls render in every mode.
            'gadgets_enabled', 'gadget-add-id', 'gadget-add-name',
            'gadget-add-protocol', 'gadget_ha_enabled', 'gadget-ha-entity',
            # Added after the pin: the "only show sensors with a current
            # reading" filter checkbox for the HA entity picker (FoodAssistant-yryl).
            'gadget-ha-hide-empty',
            # Added after the pin: the ESPHome WiFi sensor source, a toggle plus
            # the device address, sensor id, and name inputs (FoodAssistant-0oq3).
            'gadget_esp_enabled', 'gadget-esp-host', 'gadget-esp-sensor',
            'gadget-esp-name',
            # Added after the pin: the Hygrometers section (FoodAssistant-q97i):
            # the class toggle, the add-by-address form, and the Home Assistant
            # temperature + humidity pair picker. Device-local, so it renders
            # in every mode like the rest of the pane.
            'hygrometers_enabled', 'hygro-add-id', 'hygro-add-name',
            'hygro-add-location', 'hygro-add-protocol',
            'hygro-ha-temp', 'hygro-ha-hum', 'hygro-ha-name',
            # Added after the pin: the Door sensors section (FoodAssistant-5c61):
            # the class toggle and the add-by-address form for fridge/freezer
            # door contact sensors. Device-local, like the rest of the pane.
            'contacts_enabled', 'contact-add-id', 'contact-add-name',
            'contact-add-location', 'contact-add-protocol',
            # Added after the pin: the Shelf buttons section (FoodAssistant-771d):
            # the class toggle for BLE push buttons mapped to shopping-list adds
            # and actions. Device-local, like the rest of the pane.
            'buttons_enabled',
            # Added after the pin: the Accessories section (FoodAssistant-etsc):
            # the class toggle for plug-in STEMMA QT / Qwiic boards. The device
            # cards and the NeoKey key-to-mode editor are rendered by panes.js
            # from /gadgets/state, so this toggle is the section's only control
            # in the markup. Device-local, like the rest of the pane.
            'stemma_enabled',
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
            # Added after the pin: kiosk auto-return-to-home (FoodAssistant-6e5m).
            'kiosk_auto_home_enabled', 'kiosk_auto_home_seconds',
            'ai_token_budget', 'anthropic_api_key', 'anthropic_model', 'anthropic_model_sel',
            # Removed after the pin: the legacy primary API key row renders only
            # when an api_key is already stored; new installs use named keys
            # (FoodAssistant-f8kp), and the pin fixture has no stored key.
            'appliance_air_fryer', 'appliance_blender', 'appliance_bread_machine',
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
            'auth_required', 'auto_update', 'update_channel',
            # Added after the pin: the device-local off switch for the
            # automatic/background update check (FoodAssistant-31v4).
            'check_for_updates',
            'background_file',
            'background_image_url', 'background_opacity', 'backup_include_secrets', 'barcode_autocheck_shopping',
            'barcode_enrichment', 'barcode_global_capture', 'barcode_llm_fallback',
            # Added after the pin: the optional Beszel monitoring hub link on the
            # Resources pane (FoodAssistant-4kz2).
            'beszel_enabled', 'beszel_url',
            'cam-ip-host',
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
            # Added after the pin: the Bandit Cubs section in the Devices pane
            # (FoodAssistant-bzqj). Pi Hosted is a main server for its Cubs,
            # so it carries the same fleet-wide content settings.
            'cub_default_view', 'cub_timers_take_over', 'cub_probes_take_over',
            'cub_rotate_seconds', 'cub_poll_seconds',
            # Added after the pin: the alerts takeover toggle (FoodAssistant-5c61).
            'cub_alerts_take_over',
            # Added after the pin: the Bluetooth status beacon toggle
            # (FoodAssistant-yl6u, UI landed with FoodAssistant-guwc). Shown in
            # every main-install mode: on a radio-less server the flag is what
            # a Bandit's reader broadcasts with.
            'cub_ble_advertise',
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
            # Added after the pin: the LAN device pairing toggle on the Security
            # pane (FoodAssistant-4box). Pi Hosted is a main server for its
            # satellites, so it offers pairing too.
            'local_device_pairing_enabled',
            'restore-file',
            # Added after the pin: the Community shelf life panel on the
            # Inventory pane (FoodAssistant-ezkh). Pi Hosted is a main server,
            # so it carries the same controls.
            'share_expiry_learning', 'use_community_expiry',
            'scan_cidr',
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
            # start_page_mode: the Glance/Custom home-style select (FoodAssistant-gg33).
            'start_page_mode',
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
            # Added after the pin: the LD2410C mmWave presence-sensor wake
            # select on the Screen & Sleep pane, next to Wake on motion
            # (FoodAssistant-6z8c).
            'wake_on_presence',
            'wifi_password', 'wifi_ssid',
            # Added after the pin: the Advanced display per-edge safe-area inset
            # inputs in the Kiosk display card, so a panel that clips at an edge
            # can be nudged in until nothing is cut off (Bandit right-edge clip).
            'display_margin_top', 'display_margin_right',
            'display_margin_bottom', 'display_margin_left',
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
            # Added after the pin: the round/square label shape selector
            # in the label size panel (FoodAssistant-bprm).
            'label_shape',
            'document_page_size', 'document_color_mode', 'document_duplex',
            # Added after the pin: outline/border toggle and saved layout
            # presets in the field designer (FoodAssistant-rhqa), plus a
            # decorative-label symbol/outline pair, an icon element and QR
            # payload kind in the designer, and the decorative label's own
            # symbol picker (FoodAssistant-nxr8). The designer controls are
            # server/pi_hosted only (label size + designer are hidden on
            # pi_remote, FoodAssistant-eml9); the decorative-label ones render
            # everywhere printing does.
            'ld-insp-outline', 'ld-preset-select', 'ld-preset-name',
            'ld-insp-icon', 'ld-insp-qr-kind', 'ld-insp-qr-note',
            'decorative-icon', 'decorative-outline',
            # Added after the pin: the Thermometers pane (FoodAssistant-mnks):
            # the feature toggle, the add-by-address form, and the Home
            # Assistant source (toggle + entity picker). Device-local, so the
            # same controls render in every mode.
            'gadgets_enabled', 'gadget-add-id', 'gadget-add-name',
            'gadget-add-protocol', 'gadget_ha_enabled', 'gadget-ha-entity',
            # Added after the pin: the "only show sensors with a current
            # reading" filter checkbox for the HA entity picker (FoodAssistant-yryl).
            'gadget-ha-hide-empty',
            # Added after the pin: the ESPHome WiFi sensor source, a toggle plus
            # the device address, sensor id, and name inputs (FoodAssistant-0oq3).
            'gadget_esp_enabled', 'gadget-esp-host', 'gadget-esp-sensor',
            'gadget-esp-name',
            # Added after the pin: the Hygrometers section (FoodAssistant-q97i):
            # the class toggle, the add-by-address form, and the Home Assistant
            # temperature + humidity pair picker. Device-local, so it renders
            # in every mode like the rest of the pane.
            'hygrometers_enabled', 'hygro-add-id', 'hygro-add-name',
            'hygro-add-location', 'hygro-add-protocol',
            'hygro-ha-temp', 'hygro-ha-hum', 'hygro-ha-name',
            # Added after the pin: the Door sensors section (FoodAssistant-5c61):
            # the class toggle and the add-by-address form for fridge/freezer
            # door contact sensors. Device-local, like the rest of the pane.
            'contacts_enabled', 'contact-add-id', 'contact-add-name',
            'contact-add-location', 'contact-add-protocol',
            # Added after the pin: the Shelf buttons section (FoodAssistant-771d):
            # the class toggle for BLE push buttons mapped to shopping-list adds
            # and actions. Device-local, like the rest of the pane.
            'buttons_enabled',
            # Added after the pin: the Accessories section (FoodAssistant-etsc):
            # the class toggle for plug-in STEMMA QT / Qwiic boards. The device
            # cards and the NeoKey key-to-mode editor are rendered by panes.js
            # from /gadgets/state, so this toggle is the section's only control
            # in the markup. Device-local, like the rest of the pane.
            'stemma_enabled',
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
            # Added after the pin: kiosk auto-return-to-home (FoodAssistant-6e5m).
            'kiosk_auto_home_enabled', 'kiosk_auto_home_seconds',
            # Added after the pin: the sensor relay toggle on the Bandit
            # Remotes pane (FoodAssistant-me3t, UI landed with
            # FoodAssistant-guwc). Satellites only: nothing else has a main
            # server to hand its readings to.
            'relay_gadgets_upstream',
            'anthropic_api_key', 'anthropic_model', 'anthropic_model_sel',
            # api_key removed after the pin: legacy-only row (FoodAssistant-f8kp).
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
            'auth_required', 'auto_update', 'update_channel',
            # Added after the pin: the device-local off switch for the
            # automatic/background update check (FoodAssistant-31v4).
            'check_for_updates',
            'background_file', 'background_image_url',
            'background_opacity', 'backup_include_secrets', 'barcode_autocheck_shopping', 'barcode_enrichment',
            'barcode_global_capture', 'barcode_llm_fallback',
            # Added after the pin: the optional Beszel monitoring hub link on the
            # Resources pane (FoodAssistant-4kz2). Both fields are server-managed
            # (SATELLITE_PULL_FIELDS) and render read-only on a satellite, but the
            # controls themselves still appear in the markup.
            'beszel_enabled', 'beszel_url',
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
            # start_page_mode: the Glance/Custom home-style select (FoodAssistant-gg33).
            'start_key_style', 'start_page_mode', 'start_page_enabled', 'start_page_keys', 'streamdeck_brightness',
            'streamdeck_ha_base_url', 'streamdeck_ha_token', 'streamdeck_icon_color', 'streamdeck_idle_timeout',
            'streamdeck_key_count', 'streamdeck_key_style', 'streamdeck_logo_when_display_off', 'streamdeck_rotation',
            'streamdeck_weather_location', 'streamdeck_weather_units', 'suggest_per_tier', 'themealdb_api_key',
            'streamdeck_key_count', 'streamdeck_key_style', 'streamdeck_rotation',
            'streamdeck_weather_location', 'streamdeck_weather_units', 'suggest_per_tier', 'themealdb_api_key', 'timer_chips',
            'totp-code', 'ui_scale', 'ui_theme', 'upstream_api_key',
            'usb_backup_interval_hours', 'vision_provider', 'wake_on_motion',
            # Added after the pin: the LD2410C mmWave presence-sensor wake
            # select, next to Wake on motion (FoodAssistant-6z8c).
            'wake_on_presence',
            'wifi_password',
            'wifi_ssid',
            # Added after the pin: the Advanced display per-edge safe-area inset
            # inputs in the Kiosk display card (is_pi only, so pi_hosted and
            # pi_remote), to fix a panel that clips at an edge (Bandit clip).
            'display_margin_top', 'display_margin_right',
            'display_margin_bottom', 'display_margin_left',
            # Added after the pin: the Printing pane (FoodAssistant-fb8x). On a
            # satellite the printer is system-level (chosen on the main server),
            # so there are no per-device printer selects, and label size + the
            # label designer live on the server too (FoodAssistant-eml9). The
            # satellite keeps the feature toggle and the decorative-label print
            # box (a print action, which relays to the server).
            'printing_enabled', 'decorative-text',
            # Added after the pin: the decorative label's symbol/outline
            # controls (FoodAssistant-nxr8). Unlike the field designer, the
            # decorative-label print box renders on a satellite too.
            'decorative-icon', 'decorative-outline',
            # Added after the pin: the Thermometers pane (FoodAssistant-mnks):
            # the feature toggle, the add-by-address form, and the Home
            # Assistant source (toggle + entity picker). Device-local, so the
            # same controls render in every mode.
            'gadgets_enabled', 'gadget-add-id', 'gadget-add-name',
            'gadget-add-protocol', 'gadget_ha_enabled', 'gadget-ha-entity',
            # Added after the pin: the "only show sensors with a current
            # reading" filter checkbox for the HA entity picker (FoodAssistant-yryl).
            'gadget-ha-hide-empty',
            # Added after the pin: the ESPHome WiFi sensor source, a toggle plus
            # the device address, sensor id, and name inputs (FoodAssistant-0oq3).
            'gadget_esp_enabled', 'gadget-esp-host', 'gadget-esp-sensor',
            'gadget-esp-name',
            # Added after the pin: the Hygrometers section (FoodAssistant-q97i):
            # the class toggle, the add-by-address form, and the Home Assistant
            # temperature + humidity pair picker. Device-local, so it renders
            # in every mode like the rest of the pane.
            'hygrometers_enabled', 'hygro-add-id', 'hygro-add-name',
            'hygro-add-location', 'hygro-add-protocol',
            'hygro-ha-temp', 'hygro-ha-hum', 'hygro-ha-name',
            # Added after the pin: the Door sensors section (FoodAssistant-5c61):
            # the class toggle and the add-by-address form for fridge/freezer
            # door contact sensors. Device-local, like the rest of the pane.
            'contacts_enabled', 'contact-add-id', 'contact-add-name',
            'contact-add-location', 'contact-add-protocol',
            # Added after the pin: the Shelf buttons section (FoodAssistant-771d):
            # the class toggle for BLE push buttons mapped to shopping-list adds
            # and actions. Device-local, like the rest of the pane.
            'buttons_enabled',
            # Added after the pin: the Accessories section (FoodAssistant-etsc):
            # the class toggle for plug-in STEMMA QT / Qwiic boards. The device
            # cards and the NeoKey key-to-mode editor are rendered by panes.js
            # from /gadgets/state, so this toggle is the section's only control
            # in the markup. Device-local, like the rest of the pane.
            'stemma_enabled',
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
            'anthropic_api_key', 'anthropic_model', 'anthropic_model_sel',
            # Removed after the pin: the wizard no longer asks a new user to save
            # a master API key (FoodAssistant-9mu5); named keys are created later
            # in Settings. Added: the confirm-password field (#1).
            'auth_password', 'auth_password_confirm',
            'auth_required', 'display_rotation_wiz', 'display_touch',
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
            # Added after the pin: the community shelf-life sharing opt-in
            # checkbox on the wizard's Done step (FoodAssistant-ezkh). Off by
            # default; the same setting lives on the Inventory pane later.
            'wiz_share_expiry_learning',
        ],
        "targets": [
            # Removed after the pin: the wizard's Mealie section moved from a
            # Bootstrap collapse card into a plain <details> Advanced
            # disclosure (#5), so it no longer renders a data-bs-target. Its
            # form-control ids (mealie_base_url and friends) are unchanged and
            # still pinned above.
            '#recipes-collapse', '#scanner-collapse',
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


def test_settings_menu_reorg_devices_group(client, monkeypatch):
    """Settings reorg (2026-07-15): a Devices menu group holds Bandit Remotes
    (the pane-devices pill, label rename only), Thermometers & Sensors (the
    pane-gadgets pill), Printing, and (on a Pi) a Stream Deck shortcut; Status
    moves into System next to Advanced; the gadgets pane splits into six
    pill-switched sections with every control id intact (pinned above)."""
    for shape in ("server", "pi_hosted", "pi_remote"):
        mode, is_pi, configured = SHAPES[shape]
        html = _render(client, monkeypatch, mode=mode, is_pi=is_pi,
                       configured=configured)
        # Label rename, ids unchanged: the pill still targets #pane-devices.
        assert "Fleet &amp; Remote Access" not in html
        assert "Fleet & Remote Access" not in html
        assert "Bandit Remotes" in html, shape
        assert 'data-bs-target="#pane-devices"' in html, shape
        assert '<div class="menu-heading">Devices</div>' in html, shape
        assert "Thermometers &amp; Sensors" in html, shape
        # Status now sits in System, after Security/Backups in the menu.
        assert (html.index('data-bs-target="#pane-status"')
                > html.index('data-bs-target="#pane-backups"')), shape
        # The Stream Deck shortcut renders on Pi shapes only.
        assert ("openStreamDeckSettings()" in html) == is_pi, shape
        # Gadgets pane sections: all six exist with a switch pill and a badge
        # slot; Probes shows first, the rest start hidden.
        for sec in ("probes", "wifi", "ha", "hygro", "doors", "buttons"):
            assert f'id="gadget-sec-{sec}"' in html, (shape, sec)
            assert f'id="gsec-badge-{sec}"' in html, (shape, sec)
            assert f"gadgetsShowSection('{sec}')" in html, (shape, sec)
        assert 'class="gadget-section" id="gadget-sec-probes"' in html, shape
        for sec in ("wifi", "ha", "hygro", "doors", "buttons"):
            assert f'class="gadget-section d-none" id="gadget-sec-{sec}"' in html, (shape, sec)


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


def test_kiosk_auto_home_is_saveable_and_accepted():
    """The kiosk auto-return-to-home controls persist and are accepted by the
    save payload (FoodAssistant-6e5m). Guards the full save wiring: a Settings
    field, a _SAVEABLE key, and a SetupPayload field are all needed or the
    control silently no-ops on save."""
    from app.routers.setup import SetupPayload
    from app.config import _SAVEABLE, Settings

    for key in ("kiosk_auto_home_enabled", "kiosk_auto_home_seconds", "kiosk_auto_home_exempt"):
        assert key in Settings.model_fields, f"{key} missing from Settings"
        assert key in _SAVEABLE, f"{key} missing from _SAVEABLE"
        assert key in SetupPayload.model_fields, f"{key} missing from SetupPayload"


def test_wake_on_presence_is_saveable_and_validated():
    """The LD2410C presence-sensor wake select persists and validates like
    wake_on_motion (FoodAssistant-6z8c): a Settings field, a _SAVEABLE key,
    and a SetupPayload field are all needed or the control silently no-ops
    on save, and an unknown mode must be dropped rather than stored."""
    from app.routers.setup import SetupPayload
    from app.config import _SAVEABLE, Settings

    assert "wake_on_presence" in Settings.model_fields
    assert "wake_on_presence" in _SAVEABLE
    assert "wake_on_presence" in SetupPayload.model_fields
    assert Settings.model_fields["wake_on_presence"].default == "auto"

    for mode in ("auto", "on", "off"):
        data = SetupPayload(wake_on_presence=mode).model_dump(exclude_unset=True)
        assert data["wake_on_presence"] == mode


def test_community_shelf_life_is_saveable_and_defaults_safe():
    """The community shelf-life controls persist end to end, and the sharing
    switch is OPT-IN (FoodAssistant-ezkh): default False in Settings and in the
    save payload, so nothing is ever shared unless the user turns it on."""
    from app.routers.setup import SetupPayload
    from app.config import _SAVEABLE, SATELLITE_PULL_FIELDS, Settings

    for key in ("share_expiry_learning", "use_community_expiry"):
        assert key in Settings.model_fields, f"{key} missing from Settings"
        assert key in _SAVEABLE, f"{key} missing from _SAVEABLE"
        assert key in SetupPayload.model_fields, f"{key} missing from SetupPayload"
        # Capture and suggestions happen on the main server (pending forwards
        # there), so neither flag is satellite-synced.
        assert key not in SATELLITE_PULL_FIELDS

    assert Settings.model_fields["share_expiry_learning"].default is False
    assert Settings.model_fields["use_community_expiry"].default is True


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



def test_device_toggles_are_saveable_and_keep_stored_values_on_a_null(client):
    """The Cub Bluetooth beacon and the satellite sensor relay both got their
    toggle with FoodAssistant-guwc. Both ride the "None means the field was not
    submitted" contract, so a per-section save that does not render them (a
    satellite has no Cubs block, a server has no relay row) never writes over
    the stored value."""
    from app.routers.setup import SetupPayload
    from app.config import _SAVEABLE, SATELLITE_PULL_FIELDS, Settings

    for key in ("cub_ble_advertise", "relay_gadgets_upstream"):
        assert key in Settings.model_fields, f"{key} missing from Settings"
        assert key in _SAVEABLE, f"{key} missing from _SAVEABLE"
        assert key in SetupPayload.model_fields, f"{key} missing from SetupPayload"
        # Both are device-local: the relay is this satellite's own choice, and
        # the beacon flag rides the gadget config block, not the settings pull.
        assert key not in SATELLITE_PULL_FIELDS
        # An untouched field is absent, not None, so a section save cannot
        # clobber it.
        assert key not in SetupPayload().model_dump(exclude_unset=True)

    # The beacon is off until asked for; the relay is on so a satellite's
    # sensors reach the server without anyone configuring it.
    assert Settings.model_fields["cub_ble_advertise"].default is False
    assert Settings.model_fields["relay_gadgets_upstream"].default is True

    saved = []
    with patch.object(type(settings), "is_configured", lambda self: True), \
            patch.object(type(settings), "save", lambda self, data: saved.append(dict(data))):
        for key in ("cub_ble_advertise", "relay_gadgets_upstream"):
            saved.clear()
            client.post("/setup/save", json={key: True})
            assert saved and saved[0][key] is True
            saved.clear()
            client.post("/setup/save", json={key: False})
            assert saved and saved[0][key] is False
            # An explicit null is dropped rather than stored over a boolean.
            saved.clear()
            client.post("/setup/save", json={key: None})
            assert saved and key not in saved[0]
