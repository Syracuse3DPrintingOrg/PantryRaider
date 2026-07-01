# Settings Visibility Matrix

This page shows, for every persisted setting, where it can be edited and how it
behaves across the three deployment modes: **Server hosted**, **Pi Hosted**, and
**Pi Remote** (a satellite).

It is derived directly from `service/app/config.py`, so it can be regenerated
when the lists there change. The three sources are:

- `_SAVEABLE` lists every setting that is persisted to `settings.json`.
- `SATELLITE_PULL_FIELDS` lists the settings a Pi Remote pulls from its main
  server and mirrors locally. These are read-only on the satellite: edit them on
  the server and the satellite picks them up on its next sync.
- `SECRET_SETTING_KEYS` lists the settings that hold credentials. These are
  redacted from backups unless the operator opts in, and are never rendered back
  into the setup page.

## How to read the behaviour column

- **Editable** means the setting is editable in the setup wizard or Settings page
  on that mode.
- **Inherited (read-only)** means the value is pulled from the main server on a
  Pi Remote and cannot be changed locally. Change it on the server.
- **Device-local** means the setting is in `_SAVEABLE` but not in
  `SATELLITE_PULL_FIELDS`, so it is editable on every mode and is never synced
  between devices. Each device keeps its own value.
- **Satellite-only** means the setting only applies on a Pi Remote (it configures
  the link to the main server) and is not used on the other modes.
- **Secret** marks a setting that holds a credential and is redacted from
  backups by default.

A setting that is device-local behaves identically on all three modes.

## AI and vision providers

These configure the vision/LLM provider used for photo recognition, barcode
enrichment, and cook suggestions. All are pulled by a satellite from the server.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `vision_provider` | | Editable | Editable | Inherited (read-only) |
| `gemini_api_key` | Secret | Editable | Editable | Inherited (read-only) |
| `gemini_model` | | Editable | Editable | Inherited (read-only) |
| `ollama_base_url` | | Editable | Editable | Inherited (read-only) |
| `ollama_model` | | Editable | Editable | Inherited (read-only) |
| `openai_api_key` | Secret | Editable | Editable | Inherited (read-only) |
| `openai_model` | | Editable | Editable | Inherited (read-only) |
| `anthropic_api_key` | Secret | Editable | Editable | Inherited (read-only) |
| `anthropic_model` | | Editable | Editable | Inherited (read-only) |
| `ai_extra_keys` | Secret | Editable | Editable | Device-local |
| `ai_token_budget` | | Editable | Editable | Device-local |

Note: `ai_extra_keys` is device-local. It is in `_SAVEABLE` and is a secret, but
it is not in `SATELLITE_PULL_FIELDS`, so each device keeps its own spare keys.

## Barcode scanning and enrichment

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `scanner_type` | | Editable | Editable | Device-local |
| `barcode_global_capture` | | Editable | Editable | Device-local |
| `extra_api_key_names` | | Editable | Editable | Device-local |
| `barcode_enrichment` | | Editable | Editable | Inherited (read-only) |
| `barcode_llm_fallback` | | Editable | Editable | Inherited (read-only) |
| `barcode_autocheck_shopping` | | Editable | Editable | Inherited (read-only) |
| `enrich_provider` | | Editable | Editable | Inherited (read-only) |
| `enrich_model` | | Editable | Editable | Inherited (read-only) |

## Grocy (inventory backend)

A satellite talks to the server's Grocy directly, so it inherits these.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `grocy_base_url` | | Editable | Editable | Inherited (read-only) |
| `grocy_api_key` | Secret | Editable | Editable | Inherited (read-only) |
| `grocy_public_url` | | Editable | Editable | Inherited (read-only) |

## Mealie (recipes, meal plan, shopping)

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `mealie_base_url` | | Editable | Editable | Inherited (read-only) |
| `mealie_api_key` | Secret | Editable | Editable | Inherited (read-only) |
| `mealie_public_url` | | Editable | Editable | Inherited (read-only) |

## Recipe sources and suggestion tuning

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `recipe_source` | | Editable | Editable | Inherited (read-only) |
| `themealdb_api_key` | Secret | Editable | Editable | Inherited (read-only) |
| `spoonacular_api_key` | Secret | Editable | Editable | Inherited (read-only) |
| `staple_items` | | Editable | Editable | Inherited (read-only) |
| `cook_ai_context` | | Editable | Editable | Inherited (read-only) |
| `kitchen_appliances` | | Editable | Editable | Inherited (read-only) |
| `perishable_days` | | Editable | Editable | Inherited (read-only) |
| `expiring_soon_days` | | Editable | Editable | Inherited (read-only) |
| `suggest_per_tier` | | Editable | Editable | Inherited (read-only) |
| `custom_storage_categories` | | Editable | Editable | Inherited (read-only) |

## Navigation and custom tabs

Navigation order, hidden tabs, parent grouping, and custom tabs are device-local,
so each device can arrange its own menu. The theme is inherited so the fleet
shares one look.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `nav_order` | | Editable | Editable | Device-local |
| `nav_hidden` | | Editable | Editable | Device-local |
| `custom_nav_tabs` | | Editable | Editable | Device-local |
| `nav_parents` | | Editable | Editable | Device-local |

## Theme and interface

`ui_theme` is inherited so the fleet matches; the custom theme swatches, UI
scale, and display rotation are device-local hardware/look choices.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `ui_theme` | | Editable | Editable | Inherited (read-only) |
| `custom_theme_base` | | Editable | Editable | Device-local |
| `custom_theme_primary` | | Editable | Editable | Device-local |
| `custom_theme_accent` | | Editable | Editable | Device-local |
| `custom_theme_bg` | | Editable | Editable | Device-local |
| `custom_theme_surface` | | Editable | Editable | Device-local |
| `custom_theme_text` | | Editable | Editable | Device-local |
| `custom_themes` | | Editable | Editable | Device-local |
| `background_image_url` | | Editable | Editable | Device-local |
| `background_opacity` | | Editable | Editable | Device-local |
| `start_page_enabled` | | Editable | Editable | Device-local |
| `start_page_keys` | | Editable | Editable | Device-local |
| `start_page_layout` | | Editable | Editable | Device-local |
| `ui_scale` | | Editable | Editable | Device-local |
| `display_rotation` | | Editable | Editable | Device-local |
| `display_type` | | Editable | Editable | Device-local |
| `quiet_mode` | | Editable | Editable | Device-local |
| `convert_custom_rows` | | Editable | Editable | Device-local |

Note: `convert_custom_rows` (the Conversions cheat-sheet rows) is intentionally
left device-local so each kiosk keeps its own reference list.

## Display and peripherals

These describe the hardware attached to a Pi (display panel, touch, Stream Deck)
and are device-local. They do not apply to a server install.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `device_hostname` | | Editable | Editable | Device-local |
| `lan_scan_cidr` | | Editable | Editable | Device-local |
| `has_streamdeck` | | Not applicable | Editable | Device-local |
| `streamdeck_key_count` | | Not applicable | Editable | Device-local |
| `display_touch` | | Not applicable | Editable | Device-local |
| `display_idle_timeout` | | Not applicable | Editable | Device-local |
| `streamdeck_idle_timeout` | | Not applicable | Editable | Device-local |

The display and Stream Deck panes are shown only on the Pi modes (the
`peripherals` feature flag is Pi-only). `device_hostname` is offered on every
mode because it controls how browser links are built.

## Stream Deck

The Stream Deck weather widget, custom keys, cameras, and Home Assistant
credentials are pulled from the server so a custom button or camera built once
appears on every deck in the fleet. The per-deck visual style
(`streamdeck_key_style`, `streamdeck_icon_color`) is deliberately device-local so
each deck can pick its own look and keep it across syncs.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `streamdeck_key_overrides` | | Editable | Editable | Inherited (read-only) |
| `streamdeck_weather_location` | | Editable | Editable | Inherited (read-only) |
| `streamdeck_weather_units` | | Editable | Editable | Inherited (read-only) |
| `weather_api_base` | | Editable | Editable | Device-local |
| `streamdeck_key_style` | | Editable | Editable | Device-local |
| `streamdeck_icon_color` | | Editable | Editable | Device-local |
| `streamdeck_cameras` | | Editable | Editable | Inherited (read-only) |
| `streamdeck_ha_base_url` | | Editable | Editable | Inherited (read-only) |
| `streamdeck_ha_token` | Secret | Editable | Editable | Inherited (read-only) |
| `streamdeck_ha_slots` | | Editable | Editable | Inherited (read-only) |

## On-screen Home Assistant events

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `ha_events_enabled` | | Editable | Editable | Inherited (read-only) |
| `ha_camera_popup_seconds` | | Editable | Editable | Inherited (read-only) |

## Floating navigation bar

The on-screen navigation bar position and orientation are device-local server
defaults; a drag on the device overrides them per-device via localStorage.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `floating_nav_position` | | Editable | Editable | Device-local |
| `floating_nav_orientation` | | Editable | Editable | Device-local |
| `floating_nav_autohide_streamdeck` | | Editable | Editable | Device-local |

## Deployment and the satellite link

These pick the deployment mode and, on a satellite, wire it to its main server.
The upstream link fields apply only on a Pi Remote.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `deployment_mode` | | Editable | Editable | Editable |
| `remote_server_url` | | Not applicable | Not applicable | Editable (satellite-only) |
| `remote_server_ip` | | Not applicable | Not applicable | Cached automatically (satellite-only) |
| `remote_server_host` | | Not applicable | Not applicable | Cached automatically (satellite-only) |
| `upstream_api_key` | | Not applicable | Not applicable | Editable (satellite-only) |
| `kiosk_pin` | Secret | Not applicable | Not applicable | Editable (satellite-only) |
| `kiosk_readonly_when_locked` | | Not applicable | Not applicable | Editable (satellite-only) |
| `satellite_sync_minutes` | | Not applicable | Not applicable | Editable (satellite-only) |
| `satellite_last_sync` | | Not applicable | Not applicable | Written automatically (satellite-only) |
| `device_id` | | Auto-generated | Auto-generated | Auto-generated |

`device_id` is generated once on first run on every mode and persisted so the
device keeps a stable identity. `remote_server_ip`, `remote_server_host`, and
`satellite_last_sync` are written by the sync process, not edited by hand.

## Authentication and security

Auth is device-local on purpose: the main server owns access control, and a
satellite usually runs with the UI password off behind a PIN. The secret key,
password, TOTP secret, and API keys are never synced.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `auth_required` | | Editable | Editable | Device-local |
| `auth_password` | Secret | Editable | Editable | Device-local |
| `totp_secret` | Secret | Editable | Editable | Device-local |
| `api_key` | Secret | Editable | Editable | Device-local |
| `extra_api_keys` | Secret | Editable | Editable | Device-local |
| `secret_key` | Secret | Auto-generated | Auto-generated | Auto-generated |

`secret_key` is auto-generated on first run on every mode and persisted so
sessions survive a restart.

## Backups (rclone)

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `rclone_remote` | | Editable | Editable | Device-local |
| `rclone_schedule_hours` | | Editable | Editable | Device-local |

## Remote access tunnel

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `tunnel_mode` | | Editable | Editable | Device-local |
| `tunnel_token` | | Editable | Editable | Device-local |
| `tunnel_url` | | Editable | Editable | Device-local |

## Logging and updates

`debug_logging` is a per-device support toggle. `auto_update` is a fleet-wide
flag: it is pulled by satellites so a main server and its remotes update (or
hold) together.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `debug_logging` | | Editable | Editable | Device-local |
| `auto_update` | | Editable | Editable | Inherited (read-only) |
