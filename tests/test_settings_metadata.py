"""Per-setting metadata in config.py (FoodAssistant-7lhgm).

Each setting declares once, on its Settings field, whether it is saved, posted
by the settings form, pulled by a satellite, withheld by a relaying server,
cosmetic, or secret. The lists the rest of the app reads are derived from
those fields. These tests pin the derived lists to the exact contents of the
hand-written lists they replaced, plus the deliberate fixes listed since, so
no membership changes by accident, and keep docs/settings-matrix.md covering
every saved setting.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

from app import config  # noqa: E402
from app.config import Settings, setting_meta  # noqa: E402
from app.routers.setup import SetupPayload  # noqa: E402


# The hand-written lists as they stood before the metadata move.
OLD_SAVEABLE = {
    "ai_extra_keys", "ai_token_budget", "anthropic_api_key", "anthropic_model",
    "api_key", "auth_password", "auth_required", "auto_update",
    "background_image_url", "background_opacity", "barcode_autocheck_shopping",
    "barcode_enrichment", "barcode_global_capture", "barcode_llm_fallback",
    "beszel_enabled", "beszel_url", "button_devices", "buttons_enabled",
    "check_for_updates", "clock_format", "cloud_base_url",
    "cloud_instance_token", "cloud_link_session", "contact_devices",
    "contacts_enabled", "convert_custom_rows", "cook_ai_context",
    "cub_alerts_take_over", "cub_auto_update", "cub_ble_advertise",
    "cub_ble_relay", "cub_default_view", "cub_poll_seconds",
    "cub_probes_take_over", "cub_rotate_seconds", "cub_rotation",
    "cub_timers_take_over", "custom_nav_tabs", "custom_storage_categories",
    "custom_theme_accent", "custom_theme_base", "custom_theme_bg",
    "custom_theme_primary", "custom_theme_surface", "custom_theme_text",
    "custom_themes", "debug_logging", "deployment_mode", "device_hostname",
    "device_id", "display_idle_timeout", "display_margin_bottom",
    "display_margin_left", "display_margin_right", "display_margin_top",
    "display_rotation", "display_touch", "display_type", "document_color_mode",
    "document_duplex", "document_page_size", "document_printer_queue",
    "enrich_model", "enrich_provider", "expiring_soon_days",
    "extra_api_key_names", "extra_api_keys", "first_run_revealed",
    "fleet_document_printer_queue", "fleet_label_printer_queue", "fleet_relay",
    "floating_nav_autohide_streamdeck", "floating_nav_orientation",
    "floating_nav_position", "forager_recipes_enabled", "gadget_devices",
    "gadget_esp_devices", "gadget_esp_enabled", "gadget_ha_enabled",
    "gadget_ha_entities", "gadget_ha_hygrometers", "gadgets_enabled",
    "gemini_api_key", "gemini_model", "grocy_admin_password", "grocy_api_key",
    "grocy_base_url", "grocy_public_url", "ha_camera_popup_seconds",
    "ha_events_enabled", "ha_key_policy_version", "ha_token_admin",
    "ha_token_expires", "ha_token_kind", "has_streamdeck", "home_host_names",
    "hosted_config_snapshot", "hosted_stack_parked", "hygrometer_devices",
    "hygrometers_enabled", "immich_album_id", "immich_api_key",
    "immich_base_url", "kiosk_auto_home_enabled", "kiosk_auto_home_exempt",
    "kiosk_auto_home_seconds", "kiosk_pin", "kiosk_readonly_when_locked",
    "kiosk_screen_is_admin", "kiosk_split_enabled", "kiosk_split_lock_primary",
    "kiosk_split_primary", "kiosk_split_secondary", "kitchen_appliances",
    "label_dpi", "label_height_in", "label_layout_json",
    "label_layout_presets", "label_printer_queue", "label_shape",
    "label_show_logo", "label_width_in", "lan_cidrs", "lan_scan_cidr",
    "legacy_key_auto_retire", "legacy_mirror", "llm_expiry_enabled",
    "local_device_pairing_enabled", "local_totp_enabled",
    "local_totp_recovery", "local_totp_secret", "mealie_admin_password",
    "mealie_api_key", "mealie_base_url", "mealie_connected",
    "mealie_public_url", "nav_blocked_notified", "nav_hidden", "nav_order",
    "nav_parents", "nav_visibility", "neokey_rest_color", "neokey_timer_color",
    "ollama_base_url", "ollama_model", "openai_api_key", "openai_model",
    "osk_enabled", "perishable_days", "photo_folder", "photo_source",
    "photo_urls", "presence_indicator_enabled", "printing_enabled",
    "proxy_path_policy", "qr_public_url", "qr_url_mode", "quiet_mode",
    "rclone_remote", "rclone_schedule_hours", "recipe_source",
    "recipes_backend", "relay_gadgets_upstream", "remote_server_host",
    "remote_server_ip", "remote_server_url", "require_key_families",
    "satellite_last_sync", "satellite_legacy_credentials",
    "satellite_sync_minutes", "scanner_type", "scanner_uart_baud",
    "scanner_uart_enabled", "scanner_uart_port", "scheduled_reboot_day",
    "scheduled_reboot_frequency", "scheduled_reboot_time",
    "screensaver_all_clients", "screensaver_ken_burns",
    "screensaver_ken_burns_speed", "screensaver_minutes", "screensaver_mode",
    "screensaver_photo_seconds", "screensaver_pill_scale", "screensaver_speed",
    "secret_key", "share_expiry_learning", "shopping_backend",
    "spoonacular_api_key", "staple_items", "start_page_enabled",
    "start_page_keys", "start_page_layout", "start_page_mode",
    "start_page_suggestions_dismissed", "stemma_devices", "stemma_enabled",
    "streamdeck_cameras", "streamdeck_ha_base_url", "streamdeck_ha_slots",
    "streamdeck_ha_token", "streamdeck_icon_color", "streamdeck_idle_timeout",
    "streamdeck_key_count", "streamdeck_key_overrides", "streamdeck_key_style",
    "streamdeck_logo_when_display_off", "streamdeck_rotation",
    "streamdeck_weather_location", "streamdeck_weather_units",
    "suggest_per_tier", "themealdb_api_key", "timer_chips", "timezone",
    "totp_secret", "tunnel_enabled", "tunnel_mode", "tunnel_token",
    "tunnel_url", "ui_scale", "ui_theme", "update_channel",
    "update_last_available", "update_last_checked", "update_last_latest",
    "upstream_api_key", "upstream_credential_id", "upstream_gadget_config",
    "upstream_ha_policy_version", "usb_backup_interval_hours",
    "usb_backup_last", "use_community_expiry", "viewer_password",
    "vision_provider", "wake_on_motion", "wake_on_presence",
    "weather_api_base",
}

OLD_SATELLITE_PULL_FIELDS = {
    "anthropic_api_key", "anthropic_model", "auto_update",
    "barcode_autocheck_shopping", "barcode_enrichment", "barcode_llm_fallback",
    "beszel_enabled", "beszel_url", "clock_format", "cook_ai_context",
    "custom_storage_categories", "enrich_model", "enrich_provider",
    "expiring_soon_days", "fleet_document_printer_queue",
    "fleet_label_printer_queue", "gemini_api_key", "gemini_model",
    "grocy_base_url", "grocy_public_url", "ha_camera_popup_seconds",
    "ha_events_enabled", "kitchen_appliances", "label_layout_json",
    "label_show_logo", "llm_expiry_enabled", "mealie_base_url",
    "mealie_connected", "mealie_public_url", "ollama_base_url", "ollama_model",
    "openai_api_key", "openai_model", "perishable_days", "recipe_source",
    "recipes_backend", "shopping_backend", "spoonacular_api_key",
    "staple_items", "streamdeck_cameras", "streamdeck_ha_base_url",
    "streamdeck_ha_slots", "streamdeck_ha_token", "streamdeck_key_overrides",
    "streamdeck_weather_location", "streamdeck_weather_units",
    "suggest_per_tier", "themealdb_api_key", "timezone", "ui_theme",
    "update_channel", "vision_provider",
}

OLD_SATELLITE_READONLY_FIELDS = {
    "anthropic_api_key", "anthropic_model", "auto_update",
    "barcode_autocheck_shopping", "barcode_enrichment", "barcode_llm_fallback",
    "beszel_enabled", "beszel_url", "clock_format", "cook_ai_context",
    "custom_storage_categories", "enrich_model", "enrich_provider",
    "expiring_soon_days", "fleet_document_printer_queue",
    "fleet_label_printer_queue", "gemini_api_key", "gemini_model",
    "grocy_api_key", "grocy_base_url", "grocy_public_url",
    "ha_camera_popup_seconds", "ha_events_enabled", "kitchen_appliances",
    "label_layout_json", "label_show_logo", "llm_expiry_enabled",
    "mealie_api_key", "mealie_base_url", "mealie_connected",
    "mealie_public_url", "ollama_base_url", "ollama_model", "openai_api_key",
    "openai_model", "perishable_days", "recipe_source", "recipes_backend",
    "shopping_backend", "spoonacular_api_key", "staple_items",
    "streamdeck_cameras", "streamdeck_ha_base_url", "streamdeck_ha_slots",
    "streamdeck_ha_token", "streamdeck_key_overrides",
    "streamdeck_weather_location", "streamdeck_weather_units",
    "suggest_per_tier", "themealdb_api_key", "timezone", "ui_theme",
    "update_channel", "vision_provider",
}

OLD_SATELLITE_WITHHELD_FIELDS = {
    "anthropic_api_key", "gemini_api_key", "grocy_api_key", "mealie_api_key",
    "ollama_base_url", "openai_api_key", "spoonacular_api_key",
    "streamdeck_ha_token", "themealdb_api_key",
}

OLD_UNSIGNED_COSMETIC_FIELDS = {
    "clock_format", "expiring_soon_days", "perishable_days",
    "streamdeck_weather_location", "streamdeck_weather_units",
    "suggest_per_tier", "ui_theme",
}

OLD_SECRET_SETTING_KEYS = {
    "ai_extra_keys", "anthropic_api_key", "api_key", "auth_password",
    "cloud_instance_token", "extra_api_keys", "gemini_api_key",
    "grocy_admin_password", "grocy_api_key", "hosted_config_snapshot",
    "immich_api_key", "kiosk_pin", "local_totp_recovery", "local_totp_secret",
    "mealie_admin_password", "mealie_api_key", "openai_api_key", "secret_key",
    "spoonacular_api_key", "streamdeck_ha_token", "themealdb_api_key",
    "totp_secret", "tunnel_token", "upstream_api_key", "viewer_password",
}

OLD_SETUP_PAYLOAD = {
    'ai_extra_keys': ('dict[str, list[str]] | None', None),
    'ai_token_budget': ('int', 0),
    'anthropic_api_key': ('str', ''),
    'anthropic_model': ('str', 'claude-opus-4-8'),
    'api_key': ('str', ''),
    'auth_password': ('str', ''),
    'auth_required': ('bool', True),
    'auto_update': ('bool', True),
    'background_image_url': ('str', ''),
    'background_opacity': ('int', 40),
    'barcode_autocheck_shopping': ('bool', False),
    'barcode_enrichment': ('str', 'llm'),
    'barcode_global_capture': ('bool', True),
    'barcode_llm_fallback': ('bool', False),
    'check_for_updates': ('bool', True),
    'clock_format': ('str', ''),
    'contacts_enabled': ('bool', False),
    'convert_custom_rows': ('list', []),
    'cook_ai_context': ('str', ''),
    'cub_alerts_take_over': ('bool | None', None),
    'cub_auto_update': ('bool | None', None),
    'cub_ble_advertise': ('bool | None', None),
    'cub_default_view': ('str | None', None),
    'cub_poll_seconds': ('int | None', None),
    'cub_probes_take_over': ('bool | None', None),
    'cub_rotate_seconds': ('int | None', None),
    'cub_rotation': ('list[str] | None', None),
    'cub_timers_take_over': ('bool | None', None),
    'current_password': ('str', ''),
    'custom_nav_tabs': ('list[dict] | None', None),
    'custom_theme_accent': ('str', '#34d399'),
    'custom_theme_base': ('str', 'dark'),
    'custom_theme_bg': ('str', '#0d1117'),
    'custom_theme_primary': ('str', '#4f9dff'),
    'custom_theme_surface': ('str', '#161b22'),
    'custom_theme_text': ('str', '#e6edf3'),
    'deployment_mode': ('str', ''),
    'device_hostname': ('str', ''),
    'display_idle_timeout': ('int', 0),
    'display_rotation': ('int', 0),
    'display_touch': ('bool', False),
    'display_type': ('str', 'generic'),
    'document_printer_queue': ('str', ''),
    'enrich_model': ('str', ''),
    'enrich_provider': ('str', ''),
    'expiring_soon_days': ('int', 5),
    'extra_api_keys': ('list[dict | str] | None', None),
    'fleet_document_printer_queue': ('str', ''),
    'fleet_label_printer_queue': ('str', ''),
    'floating_nav_autohide_streamdeck': ('bool', False),
    'floating_nav_orientation': ('str', ''),
    'floating_nav_position': ('str', ''),
    'gadget_ha_enabled': ('bool', False),
    'gadgets_enabled': ('bool', False),
    'gemini_api_key': ('str', ''),
    'gemini_model': ('str', 'gemini-3.8-flash'),
    'grocy_api_key': ('str', ''),
    'grocy_base_url': ('str', ''),
    'grocy_public_url': ('str', ''),
    'ha_camera_popup_seconds': ('int', 20),
    'ha_events_enabled': ('bool', False),
    'has_streamdeck': ('bool', False),
    'hygrometers_enabled': ('bool', False),
    'immich_album_id': ('str | None', None),
    'immich_api_key': ('str', ''),
    'immich_base_url': ('str | None', None),
    'kiosk_auto_home_enabled': ('bool | None', None),
    'kiosk_auto_home_exempt': ('str | None', None),
    'kiosk_auto_home_seconds': ('int | None', None),
    'kiosk_pin': ('str', ''),
    'kiosk_split_enabled': ('bool | None', None),
    'kiosk_split_lock_primary': ('bool | None', None),
    'kiosk_split_primary': ('str | None', None),
    'kiosk_split_secondary': ('str | None', None),
    'kitchen_appliances': ('list[str] | None', None),
    'label_dpi': ('int', 203),
    'label_height_in': ('float', 1.0),
    'label_printer_queue': ('str', ''),
    'label_width_in': ('float', 2.0),
    'llm_expiry_enabled': ('bool | None', None),
    'local_device_pairing_enabled': ('bool', True),
    'mealie_api_key': ('str', ''),
    'mealie_base_url': ('str', ''),
    'mealie_public_url': ('str', ''),
    'nav_hidden': ('str', ''),
    'nav_order': ('str', ''),
    'nav_parents': ('dict | None', None),
    'nav_visibility': ('str', ''),
    'neokey_rest_color': ('str | None', None),
    'neokey_timer_color': ('str | None', None),
    'ollama_base_url': ('str', ''),
    'ollama_model': ('str', 'gemma4:e4b'),
    'openai_api_key': ('str', ''),
    'openai_model': ('str', 'gpt-6-luna'),
    'osk_enabled': ('bool', True),
    'perishable_days': ('int', 14),
    'photo_folder': ('str | None', None),
    'photo_source': ('str | None', None),
    'photo_urls': ('str | None', None),
    'presence_indicator_enabled': ('bool | None', None),
    'printing_enabled': ('bool', False),
    'qr_public_url': ('str', ''),
    'qr_url_mode': ('str', 'auto'),
    'quiet_mode': ('bool', False),
    'rclone_remote': ('str', ''),
    'rclone_schedule_hours': ('int', 0),
    'recipe_source': ('str', 'themealdb'),
    'relay_gadgets_upstream': ('bool | None', None),
    'remote_server_url': ('str', ''),
    'scanner_type': ('str', ''),
    'scanner_uart_baud': ('int', 9600),
    'scanner_uart_enabled': ('bool', False),
    'scanner_uart_port': ('str', '/dev/serial0'),
    'scheduled_reboot_day': ('int', 0),
    'scheduled_reboot_frequency': ('str', ''),
    'scheduled_reboot_time': ('str', ''),
    'screensaver_all_clients': ('bool', False),
    'screensaver_ken_burns': ('bool | None', None),
    'screensaver_ken_burns_speed': ('str | None', None),
    'screensaver_minutes': ('int', 0),
    'screensaver_mode': ('str | None', None),
    'screensaver_photo_seconds': ('int | None', None),
    'screensaver_pill_scale': ('str | None', None),
    'screensaver_speed': ('str | None', None),
    'share_expiry_learning': ('bool', False),
    'shopping_backend': ('str', ''),
    'spoonacular_api_key': ('str', ''),
    'staple_items': ('str', ''),
    'start_custom_defs': ('list | None', None),
    'start_loaded_ids': ('list | None', None),
    'start_page_enabled': ('bool', False),
    'start_page_keys': ('int', 15),
    'start_page_layout': ('list | None', None),
    'start_page_mode': ('str | None', None),
    'stemma_devices': ('list | None', None),
    'stemma_enabled': ('bool', False),
    'streamdeck_cameras': ('list', []),
    'streamdeck_ha_base_url': ('str', ''),
    'streamdeck_ha_slots': ('list', []),
    'streamdeck_ha_token': ('str', ''),
    'streamdeck_icon_color': ('str', ''),
    'streamdeck_idle_timeout': ('int', 0),
    'streamdeck_key_count': ('int', 0),
    'streamdeck_key_overrides': ('list', []),
    'streamdeck_key_style': ('str', ''),
    'streamdeck_logo_when_display_off': ('bool', True),
    'streamdeck_rotation': ('int', 0),
    'streamdeck_weather_location': ('str', ''),
    'streamdeck_weather_units': ('str', 'f'),
    'suggest_per_tier': ('int', 8),
    'themealdb_api_key': ('str', ''),
    'timer_chips': ('str', ''),
    'timezone': ('str', ''),
    'ui_scale': ('str', 'normal'),
    'ui_theme': ('str', 'pantryraider'),
    'update_channel': ('str', 'stable'),
    'upstream_api_key': ('str', ''),
    'usb_backup_interval_hours': ('int', 0),
    'use_community_expiry': ('bool', True),
    'viewer_password': ('str', ''),
    'vision_provider': ('str', 'gemini'),
    'wake_on_motion': ('str', 'auto'),
    'wake_on_presence': ('str', 'auto'),
    'weather_api_base': ('str', ''),
}

# Deliberate changes since the snapshot above. Each entry is a fix, not drift.
#
# FoodAssistant-35zqz: fields the settings panes post to /setup/save that the
# request model used to drop. None means "not posted, keep the stored value".
ADDED_TO_SETUP_PAYLOAD = {
    'display_margin_top': ('int | None', None),
    'display_margin_right': ('int | None', None),
    'display_margin_bottom': ('int | None', None),
    'display_margin_left': ('int | None', None),
    'forager_recipes_enabled': ('bool | None', None),
    'kiosk_readonly_when_locked': ('bool | None', None),
    'label_shape': ('str | None', None),
    'label_show_logo': ('bool | None', None),
    'document_page_size': ('str | None', None),
    'document_color_mode': ('str | None', None),
    'document_duplex': ('str | None', None),
    'buttons_enabled': ('bool | None', None),
    'beszel_enabled': ('bool | None', None),
    'beszel_url': ('str | None', None),
    'tunnel_mode': ('str | None', None),
    # Request-only: the wizard's Remote Access choice (FoodAssistant-mzsci).
    'forager_remote_access': ('bool | None', None),
}
# The pre-setup Forager browser marker is a session-binding value.
ADDED_SECRETS = {"cloud_link_session"}

# The "blank keeps the stored value" fields of /setup/save, as they were kept
# by hand in routers/setup.py.
OLD_SETUP_SECRET_FIELDS = {
    "gemini_api_key", "openai_api_key", "anthropic_api_key",
    "grocy_api_key", "mealie_api_key",
    "themealdb_api_key", "spoonacular_api_key",
    "auth_password", "viewer_password", "api_key", "upstream_api_key", "kiosk_pin",
    "streamdeck_ha_token", "immich_api_key",
}


def _ann(annotation) -> str:
    return annotation.__name__ if isinstance(annotation, type) else str(annotation)


def test_saveable_unchanged():
    assert set(config._SAVEABLE) == OLD_SAVEABLE
    assert len(config._SAVEABLE) == len(set(config._SAVEABLE))


def test_satellite_pull_fields_unchanged():
    assert set(config.SATELLITE_PULL_FIELDS) == OLD_SATELLITE_PULL_FIELDS
    assert len(config.SATELLITE_PULL_FIELDS) == len(set(config.SATELLITE_PULL_FIELDS))


def test_satellite_readonly_fields_unchanged():
    assert set(config.SATELLITE_READONLY_FIELDS) == OLD_SATELLITE_READONLY_FIELDS


def test_satellite_withheld_fields_unchanged():
    assert isinstance(config.SATELLITE_WITHHELD_FIELDS, tuple)
    assert set(config.SATELLITE_WITHHELD_FIELDS) == OLD_SATELLITE_WITHHELD_FIELDS


def test_unsigned_cosmetic_fields_unchanged():
    assert isinstance(config.UNSIGNED_COSMETIC_FIELDS, tuple)
    assert set(config.UNSIGNED_COSMETIC_FIELDS) == OLD_UNSIGNED_COSMETIC_FIELDS


def test_secret_setting_keys_unchanged():
    assert set(config.SECRET_SETTING_KEYS) == OLD_SECRET_SETTING_KEYS | ADDED_SECRETS


def test_setup_secret_fields_unchanged():
    from app.routers.setup import _SECRET_FIELDS
    assert set(_SECRET_FIELDS) == OLD_SETUP_SECRET_FIELDS


def test_setup_payload_unchanged():
    """The derived /setup/save model accepts exactly the fields, types and
    defaults the hand-written one did."""
    fields = SetupPayload.model_fields
    expected = {**OLD_SETUP_PAYLOAD, **ADDED_TO_SETUP_PAYLOAD}
    assert set(fields) == set(expected)
    for name, (ann, default) in expected.items():
        info = fields[name]
        assert _ann(info.annotation) == ann, name
        assert info.get_default(call_default_factory=True) == default, name


def test_setup_payload_keeps_exclude_unset():
    """A bare payload dumps nothing, so a per-section save never clobbers
    fields it did not post."""
    assert SetupPayload().model_dump(exclude_unset=True) == {}
    dumped = SetupPayload(screensaver_mode="photos").model_dump(exclude_unset=True)
    assert dumped == {"screensaver_mode": "photos"}


def test_setup_payload_mutable_defaults_not_shared():
    a, b = SetupPayload(), SetupPayload()
    a.streamdeck_cameras.append("x")
    assert b.streamdeck_cameras == []
    assert Settings.model_fields["streamdeck_cameras"].get_default() == []


def test_metadata_is_consistent():
    """Flags only make sense on saved settings; a pulled list or a secret that
    is never saved would be silently dropped."""
    saved = set(config._SAVEABLE)
    for name, info in Settings.model_fields.items():
        meta = setting_meta(info)
        if meta.get("save", True):
            continue
        for flag in ("form", "pull", "withheld", "cosmetic", "secret"):
            assert not meta.get(flag), f"{name} has {flag} but is not saved"
    assert set(config.SATELLITE_PULL_FIELDS) <= saved
    assert set(config.SECRET_SETTING_KEYS) <= saved
    assert set(config.UNSIGNED_COSMETIC_FIELDS) <= set(config.SATELLITE_PULL_FIELDS)
    assert set(config.SATELLITE_WITHHELD_FIELDS) <= saved
    assert set(SetupPayload.model_fields) - {
        "current_password", "start_custom_defs", "start_loaded_ids",
        "forager_remote_access"} <= saved


def test_env_only_settings_are_not_saveable():
    # Set only by the host environment; a settings save must never reach them.
    assert "data_dir" not in config._SAVEABLE
    assert "demo_mode" not in config._SAVEABLE
    assert Settings.model_fields["demo_mode"].validation_alias is not None


def test_settings_matrix_lists_every_saved_setting():
    """docs/settings-matrix.md has a table row for every saved setting, so the
    page cannot quietly fall behind config.py."""
    doc = (ROOT / "docs" / "settings-matrix.md").read_text()
    rows = set(re.findall(r"^\|\s*`([a-z0-9_]+)`", doc, re.M))
    missing = [k for k in config._SAVEABLE if k not in rows]
    assert not missing, f"add these to docs/settings-matrix.md: {missing}"
