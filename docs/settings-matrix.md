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
| `cloud_base_url` | | Device-local | Device-local | Device-local |
| `cloud_instance_token` | Secret | Editable | Editable | Editable |

Note: `ai_extra_keys` is device-local. It is in `_SAVEABLE` and is a secret, but
it is not in `SATELLITE_PULL_FIELDS`, so each device keeps its own spare keys.

Note: the Forager link is per-device on purpose, and is a main-install
feature (server and pi_hosted): a satellite forwards AI from its main server
and has no local app of its own to sign in or expose, so it shows no Forager
page. On a main install each device signs in with the account email and
password (or a pairing code under the card's Advanced toggle, or Continue
with Google when Forager offers it) on the Forager page in Settings and holds
its own instance token, so it shows up as its own instance on the cloud
account. The password is forwarded
to the cloud during sign-in and never stored. A sign-in on a fresh install
also sets `vision_provider` and `enrich_provider` to `cloud` (an install with
a working provider keeps it), and `qr_public_url` follows the kitchen's
public web address whenever the platform supplies one (absent that, the
stored value is untouched). `cloud_base_url` is the cloud service address;
it has no settings
control and is only changed with the `CLOUD_BASE_URL` environment variable.

## Barcode scanning and enrichment

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `scanner_type` | | Editable | Editable | Device-local |
| `barcode_global_capture` | | Editable | Editable | Device-local |
| `extra_api_key_names` | | Editable | Editable | Device-local |
| `barcode_enrichment` | | Editable | Editable | Inherited (read-only) |
| `barcode_llm_fallback` | | Editable | Editable | Inherited (read-only) |
| `barcode_autocheck_shopping` | | Editable | Editable | Inherited (read-only) |
| `llm_expiry_enabled` | | Editable | Editable | Inherited (read-only) |
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
| `qr_url_mode` | | Editable | Editable | Device-local |
| `qr_public_url` | | Editable | Editable | Device-local |
| `convert_custom_rows` | | Editable | Editable | Device-local |

Note: `convert_custom_rows` (the Conversions cheat-sheet rows) is intentionally
left device-local so each kiosk keeps its own reference list.

`qr_url_mode` picks which address the "Add items from your phone" QR code
encodes: `auto` (the default) uses the device's own network address, so a
phone on the same network can open it even when the kiosk browses at
localhost; `public` encodes `qr_public_url` (or the active tunnel URL when
that field is empty). Both stay device-local so every kiosk's QR code points
at the device that shows it.

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
| `wake_on_motion` | | Not applicable | Editable | Device-local |
| `screensaver_minutes` | | Editable | Editable | Device-local |
| `screensaver_speed` | | Editable | Editable | Device-local |
| `screensaver_pill_scale` | | Editable | Editable | Device-local |
| `screensaver_photo_seconds` | | Editable | Editable | Device-local |
| `screensaver_ken_burns` | | Editable | Editable | Device-local |
| `screensaver_mode` | | Editable | Editable | Device-local |
| `screensaver_all_clients` | | Editable | Editable | Device-local |
| `osk_enabled` | | Editable | Editable | Device-local |
| `streamdeck_idle_timeout` | | Not applicable | Editable | Device-local |
| `streamdeck_logo_when_display_off` | | Not applicable | Editable | Device-local |

The kiosk display cards (Settings, Personalization, Screen & Sleep) and the
Stream Deck editor (Settings, Personalization, Start Page & Stream Deck) are
shown only on the Pi modes (the `peripherals` feature flag is Pi-only).
`device_hostname` is offered on every mode (Settings, Devices & Fleet)
because it controls how browser links are built.

`display_idle_timeout` switches the panel itself off after the idle period;
`screensaver_minutes` is the softer on-screen layer (the page dims to a
floating clock, a touch brings it back) for panels that should stay powered.
`screensaver_mode` picks what that layer shows: the bouncing logo (the
default) or a photo slideshow from an attached USB drive's photos folder,
which falls back to the logo when no drive or no photos are present.
`screensaver_all_clients` widens where the saver runs: off (the default)
keeps the idle behaviour on kiosk browsers only, on lets every browser
viewing the install (a desktop or a phone included) dim to the screensaver
after the same idle minutes. Because of that wider reach, the screensaver
settings are also offered on a server install, not just the Pi modes.
`wake_on_motion` wakes a sleeping panel when the device is moved or bumped,
read from the LSM6DSOX accelerometer on kits that include one; `auto` (the
default) enables it exactly when the sensor is present. A screen touch or a
Stream Deck button press always wakes the display regardless of this setting.

`osk_enabled` controls the on-screen keyboard: in kiosk mode a touch
keyboard slides up from the bottom of the screen whenever a text field is
tapped, so names, barcodes, and searches can be typed without a physical
keyboard. On by default; turn it off on a kiosk with a keyboard attached.
Like the screensaver, it is offered on server installs too, because any
browser can be put in kiosk (touch) mode.

`streamdeck_logo_when_display_off` puts the Pantry Raider logo across the
Stream Deck keys while the display is asleep, so the deck reads as resting
rather than showing stale buttons. Pressing any key or touching the screen
brings both surfaces back. On by default; the deck's own
`streamdeck_idle_timeout` still blanks the keys fully after its idle period.

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

## Floating navigation bar and timer chips

The on-screen navigation bar position and orientation are device-local server
defaults; a drag on the device overrides them per-device via localStorage.
`timer_chips` controls the floating per-timer countdown chips shown on every
page while timers run: on, off, or auto (auto hides them at large and
extra-large interface scale, resolved per device the same way `nav_visibility`
is).

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `floating_nav_position` | | Editable | Editable | Device-local |
| `floating_nav_orientation` | | Editable | Editable | Device-local |
| `floating_nav_autohide_streamdeck` | | Editable | Editable | Device-local |
| `nav_visibility` | | Editable | Editable | Device-local |
| `timer_chips` | | Editable | Editable | Device-local |

## Timezone, clock format, scheduled reboot, and update bookkeeping

`timezone` sets how timestamps read across the fleet: set it once on the main
server (or a standalone install) and a Pi Remote inherits it and applies it to
its own clock on each sync. `clock_format` rides with it: Auto, 12-hour, or
24-hour reading for the screensaver clock, the weather page, and timestamps,
also set once on the main server and inherited by every Pi Remote. The scheduled reboot (Settings, Personalization,
Screen & Sleep) applies only to a Pi appliance; the frequency can be Off,
Nightly, or Weekly with a day-of-week picker. The `update_last_*` fields record
the most recent update check and are maintained by the app.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `timezone` | | Editable | Editable | Inherited (read-only) |
| `clock_format` | | Editable | Editable | Inherited (read-only) |
| `scheduled_reboot_time` | | Not applicable | Editable | Device-local |
| `scheduled_reboot_frequency` | | Not applicable | Editable | Device-local |
| `scheduled_reboot_day` | | Not applicable | Editable | Device-local |
| `update_last_checked` | | Auto | Auto | Device-local (bookkeeping) |
| `update_last_latest` | | Auto | Auto | Device-local (bookkeeping) |
| `update_last_available` | | Auto | Auto | Device-local (bookkeeping) |

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
| `hosted_stack_parked` | | Not applicable | Written automatically | Written automatically |
| `hosted_config_snapshot` | Secret | Not applicable | Written automatically | Written automatically |
| `device_id` | | Auto-generated | Auto-generated | Auto-generated |

`device_id` is generated once on first run on every mode and persisted so the
device keeps a stable identity. `remote_server_ip`, `remote_server_host`, and
`satellite_last_sync` are written by the sync process, not edited by hand.

`hosted_stack_parked` and `hosted_config_snapshot` back the mode switch on a Pi
Hosted appliance (Settings, Advanced, "Run as a satellite"). Switching
pauses the local Grocy/Mealie containers (data kept on the device), snapshots
the backend settings the satellite sync will overwrite, and flips the mode to
`pi_remote`. On a switched device the Advanced section offers "Switch back to
full stack", which restarts the paused stack and restores the snapshot. A
device flashed as a plain Pi Remote never has either field set and cannot be
switched to hosting.

## Authentication and security

Auth is device-local on purpose: the main server owns access control, and a
satellite usually runs with the UI password off behind a PIN. The secret key,
password, TOTP secret, and API keys are never synced.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `auth_required` | | Editable | Editable | Device-local |
| `auth_password` | Secret | Editable | Editable | Device-local |
| `viewer_password` | Secret | Editable | Editable | Device-local |
| `totp_secret` | Secret | Editable | Editable | Device-local |
| `local_totp_secret` | Secret | Editable | Editable | Device-local |
| `local_totp_enabled` | Toggle | Editable | Editable | Device-local |
| `local_totp_recovery` | Secret | Editable | Editable | Device-local |
| `api_key` | Secret | Editable | Editable | Device-local |
| `extra_api_keys` | Secret | Editable | Editable | Device-local |
| `secret_key` | Secret | Auto-generated | Auto-generated | Auto-generated |

`secret_key` is auto-generated on first run on every mode and persisted so
sessions survive a restart.

`viewer_password` is an optional second password for the household: it logs in
to a session that can use every kitchen page (inventory, timers, scanning,
recipes) but not Settings, backups, or updates, which stay behind the main
password. Leaving it blank turns the feature off. Like the main password it is
stored hashed and never synced between devices.

`local_totp_secret`, `local_totp_enabled`, and `local_totp_recovery` are this
device's own two-factor authentication for the login password (the counterpart
to a Forager account's 2FA). The secret and the hashed recovery codes are
device-local secrets, never shown back to the browser and redacted from the
support bundle; `totp_secret` is the earlier single-secret form, still honoured
so an install that turned 2FA on before this feature keeps working.

## Backups (rclone and USB drive)

`usb_backup_interval_hours` schedules backups to an attached USB flash drive
(0 turns it off). Each device backs up its own data: a Pi Hosted box saves a
full stack snapshot, a Pi Remote saves its device config, and a server saves
the app-data zip. `usb_backup_last` records the last successful run and is
maintained by the app, not edited directly.

`rclone_remote` must be a plain rclone destination, either `remote:path` (for
example `s3:mybucket/pantry`) or an absolute path; anything else is rejected
on save.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `rclone_remote` | | Editable | Editable | Device-local |
| `rclone_schedule_hours` | | Editable | Editable | Device-local |
| `usb_backup_interval_hours` | | Editable | Editable | Device-local |
| `usb_backup_last` | | Auto-maintained | Auto-maintained | Auto-maintained |

## Printing (labels and recipes)

Printing is device-local: a printer is attached to whatever device you are
standing at, so every setting here is Device-local in all three modes (a
satellite prints to its own printer, not the main server's). `printing_enabled`
is the master switch, off by default; nothing prints and no print button shows
until it is on. `label_printer_queue` and `document_printer_queue` are CUPS
queue names read from the device. `label_width_in`, `label_height_in`, and
`label_dpi` describe the label stock (a 2x1 inch thermal label at 203 dpi by
default).

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `printing_enabled` | | Device-local | Device-local | Device-local |
| `label_printer_queue` | | Device-local | Device-local | Device-local |
| `document_printer_queue` | | Device-local | Device-local | Device-local |
| `label_width_in` | | Device-local | Device-local | Device-local |
| `label_height_in` | | Device-local | Device-local | Device-local |
| `label_dpi` | | Device-local | Device-local | Device-local |

The print stack itself (CUPS, Bluetooth, and printer drivers) is off by default
and installed only when you ask for it: choose it during install, or press
Install now under Settings, Printing on a device without a printer set up yet.
That step writes two markers into the stack's environment file (not the settings
above): `CUPS_SERVER`, which points the app at the local print server, and
`PRINTING_ENABLED`, which the update process reads so an update keeps printing
working. Both are device-local and managed for you; you do not edit them by hand.

## Remote access tunnel

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `tunnel_mode` | | Editable | Editable | Device-local |
| `tunnel_token` | | Editable | Editable | Device-local |
| `tunnel_url` | | Editable | Editable | Device-local |
| `tunnel_enabled` | | Editable | Editable | Device-local |

`tunnel_mode` is the single source of truth for remote access, chosen on the
Forager page in Settings (a main-install feature): `""` (off), `"cloudflare"`
(the cloudflared container, keyed by `tunnel_token`), or `"forager"` (the
WireGuard hub tunnel). A legacy stored `"subscription"` reads as `"forager"`.
`tunnel_enabled` tracks the Forager (WireGuard) tunnel and moves in step with
`tunnel_mode == "forager"`. That tunnel is offered only on a Pi appliance,
which runs the host bridge that owns the WireGuard endpoint; the private key
stays on the device, so nothing secret is kept app-side.

## Logging and updates

`debug_logging` is a per-device support toggle. `auto_update` and
`update_channel` are fleet-wide: both are pulled by satellites so a main server
and its remotes update (or hold) together, from the same source. The channel is
`main` (every change) or `stable` (releases only); `main` is the default for
now, with `stable` the recommended choice from release 0.8.0 on.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `debug_logging` | | Editable | Editable | Device-local |
| `auto_update` | | Editable | Editable | Inherited (read-only) |
| `update_channel` | | Editable | Editable | Inherited (read-only) |
