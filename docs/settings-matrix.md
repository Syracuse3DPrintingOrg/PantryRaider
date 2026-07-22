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
| `scanner_uart_enabled` | | Editable | Editable | Device-local |
| `scanner_uart_port` | | Editable | Editable | Device-local |
| `scanner_uart_baud` | | Editable | Editable | Device-local |
| `barcode_global_capture` | | Editable | Editable | Device-local |
| `extra_api_key_names` | | Editable | Editable | Device-local |
| `barcode_enrichment` | | Editable | Editable | Inherited (read-only) |
| `barcode_llm_fallback` | | Editable | Editable | Inherited (read-only) |
| `barcode_autocheck_shopping` | | Editable | Editable | Inherited (read-only) |
| `llm_expiry_enabled` | | Editable | Editable | Inherited (read-only) |
| `enrich_provider` | | Editable | Editable | Inherited (read-only) |
| `enrich_model` | | Editable | Editable | Inherited (read-only) |

`scanner_uart_enabled`, `scanner_uart_port`, and `scanner_uart_baud` (Settings,
Scanning & AI) read a serial (UART) barcode scanner wired to the device, for
example one on a Pi's GPIO serial pins. They stay device-local because the
scanner is physically attached to one machine, so each device sets its own port
(`/dev/serial0` by default) and baud rate (9600 by default).

## Community shelf life

Both switches live on the Inventory & Storage pane of a main install. They
are not offered on a Pi Remote at all: pending scans, expiry suggestions, and
commits all happen on the main server, so the server's setting is the one
that matters. What they do (and exactly what is and is not shared) is on
[Community shelf life and privacy](community-shelf-life.md).

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `use_community_expiry` | | Editable | Editable | Not present |
| `share_expiry_learning` | | Editable (opt-in, off by default) | Editable (opt-in, off by default) | Not present |

## Grocy (inventory backend)

A satellite talks to the server's Grocy directly, so it inherits these.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `grocy_base_url` | | Editable | Editable | Inherited (read-only) |
| `grocy_api_key` | Secret | Editable | Editable | Inherited (read-only) |
| `grocy_public_url` | | Editable | Editable | Inherited (read-only) |
| `grocy_admin_password` | Secret | Generated on first run | Generated on first run | Not present |

When a fresh Grocy still answers to its stock sign-in, Pantry Raider sets it
up by itself on first run: it creates its own API key and replaces the stock
admin password with a generated one, stored in `grocy_admin_password` and
revealed from the Inventory pane if you ever want to sign in to Grocy
directly.

## Mealie (optional connector)

Recipes, the meal plan, and the shopping list are built into Pantry Raider;
these settings only matter if you connect a Mealie you already use.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `mealie_base_url` | | Editable | Editable | Inherited (read-only) |
| `mealie_api_key` | Secret | Editable | Editable | Inherited (read-only) |
| `mealie_public_url` | | Editable | Editable | Inherited (read-only) |
| `mealie_admin_password` | Secret | Generated on first run | Generated on first run | Not present |

If you run a Mealie and it still answers to its stock sign-in, Pantry Raider
can connect itself to it: it creates its own API token, secures the account
with a generated password (stored in `mealie_admin_password`, revealed from
the Recipes pane), and adds a Groceries shopping list.

## Recipe sources and suggestion tuning

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `recipe_source` | | Editable | Editable | Inherited (read-only) |
| `recipes_backend` | | Editable | Editable | Inherited (read-only) |
| `shopping_backend` | | Editable | Editable | Inherited (read-only) |
| `themealdb_api_key` | Secret | Editable | Editable | Inherited (read-only) |
| `spoonacular_api_key` | Secret | Editable | Editable | Inherited (read-only) |
| `staple_items` | | Editable | Editable | Inherited (read-only) |
| `cook_ai_context` | | Editable | Editable | Inherited (read-only) |
| `kitchen_appliances` | | Editable | Editable | Inherited (read-only) |
| `perishable_days` | | Editable | Editable | Inherited (read-only) |
| `expiring_soon_days` | | Editable | Editable | Inherited (read-only) |
| `suggest_per_tier` | | Editable | Editable | Inherited (read-only) |
| `custom_storage_categories` | | Editable | Editable | Inherited (read-only) |

`recipes_backend` says where the recipe library lives: empty means automatic
(an install with Mealie connected keeps using it until you copy the recipes
over; everything else uses Pantry Raider's built-in store). `shopping_backend`
says where the shopping list lives: empty means automatic (the list stays
next to your inventory in Grocy, except while your recipes still come from
Mealie, which keeps the Mealie list you already use). A satellite inherits
both, so the whole fleet reads and writes the same library and list.

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
| `start_page_mode` | | Editable | Editable | Device-local |
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

`start_page_mode` is the Home style switch (Settings, Start Page): `glance`
(the default) builds the home screen automatically from the pages in your
navigation, with live count pills for Review, Alerts, and Expiring; `custom`
keeps the hand-arranged launcher built from `start_page_keys` and
`start_page_layout`. It stays device-local so a kiosk can open on Glance while
another device keeps its custom grid.

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
| `streamdeck_rotation` | | Not applicable | Editable | Device-local |
| `display_touch` | | Not applicable | Editable | Device-local |
| `display_margin_top` | | Not applicable | Editable | Device-local |
| `display_margin_right` | | Not applicable | Editable | Device-local |
| `display_margin_bottom` | | Not applicable | Editable | Device-local |
| `display_margin_left` | | Not applicable | Editable | Device-local |
| `display_idle_timeout` | | Not applicable | Editable | Device-local |
| `wake_on_motion` | | Not applicable | Editable | Device-local |
| `wake_on_presence` | | Not applicable | Editable | Device-local |
| `presence_indicator_enabled` | | Not applicable | Editable | Device-local |
| `screensaver_minutes` | | Editable | Editable | Device-local |
| `screensaver_speed` | | Editable | Editable | Device-local |
| `screensaver_pill_scale` | | Editable | Editable | Device-local |
| `screensaver_photo_seconds` | | Editable | Editable | Device-local |
| `screensaver_ken_burns` | | Editable | Editable | Device-local |
| `screensaver_mode` | | Editable | Editable | Device-local |
| `photo_source` | | Editable | Editable | Device-local |
| `photo_folder` | | Editable | Editable | Device-local |
| `photo_urls` | | Editable | Editable | Device-local |
| `immich_base_url` | | Editable | Editable | Device-local |
| `immich_api_key` | Secret | Editable | Editable | Device-local |
| `immich_album_id` | | Editable | Editable | Device-local |
| `screensaver_all_clients` | | Editable | Editable | Device-local |
| `osk_enabled` | | Editable | Editable | Device-local |
| `kiosk_auto_home_enabled` | | Editable | Editable | Device-local |
| `kiosk_auto_home_seconds` | | Editable | Editable | Device-local |
| `kiosk_auto_home_exempt` | | Editable | Editable | Device-local |
| `streamdeck_idle_timeout` | | Not applicable | Editable | Device-local |
| `streamdeck_logo_when_display_off` | | Not applicable | Editable | Device-local |

The kiosk display cards (Settings, Display & Sleep) and the
Stream Deck editor (Settings, Stream Deck) are
shown only on the Pi modes (the `peripherals` feature flag is Pi-only).
`device_hostname` is offered on every mode (Settings, Network)
because it controls how browser links are built.

`display_margin_top` / `display_margin_right` / `display_margin_bottom` /
`display_margin_left` are the Advanced display safe-area insets (pixels the
kiosk holds back from each edge). They stay device-local because every panel's
visible area differs: a rotated DSI panel can draw wider than it shows, and some
HDMI panels hide a rim behind the bezel. The kiosk already corrects the common
overscan on its own, so these usually stay 0 and are only dialed in for a panel
that still clips. Each is clamped to 0-200 px.

`display_idle_timeout` switches the panel itself off after the idle period;
`screensaver_minutes` is the softer on-screen layer (the page dims to a
floating clock, a touch brings it back) for panels that should stay powered.
`screensaver_mode` picks what that layer shows: the bouncing logo (the
default), a photo slideshow, or a retro canvas saver. `photo_source` picks
where the slideshow's pictures come from: the built-in USB drive folder, a
folder on the server (`photo_folder`, blank meaning the photos folder inside
the app's data directory), an Immich album (`immich_base_url`,
`immich_api_key`, `immich_album_id`), or a plain list of direct image links
(`photo_urls`). Google Photos and iCloud do not offer reliable access for
third-party apps, which is why they are not on that list. An empty or
unreachable source falls back to the logo, so the setting is always safe.
`screensaver_all_clients` widens where the saver runs: off (the default)
keeps the idle behaviour on kiosk browsers only, on lets every browser
viewing the install (a desktop or a phone included) dim to the screensaver
after the same idle minutes. Because of that wider reach, the screensaver
settings are also offered on a server install, not just the Pi modes.
`wake_on_presence` wakes the panel when the optional mmWave presence sensor
sees someone walk up (see the presence sensor hardware guide); auto turns it
on once the sensor has triggered at least once.

`presence_indicator_enabled` shows a small icon in the top corner of the
kiosk display when a presence sensor is fitted: faint while the room is
empty, lit while the sensor sees someone, so the sensor can be checked just
by walking up to the screen. On by default, and safe everywhere: a display
without a readable presence sensor never shows the icon at all.

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

`kiosk_auto_home_enabled` is "Return to home when idle" (Settings, Display & Sleep):
after `kiosk_auto_home_seconds` without a touch, a kiosk drifts back to its
home page so the next person starts fresh. Pages named in
`kiosk_auto_home_exempt` (by default `cook,current_recipe,weather,camera,timers`,
the pages you actively watch) are left alone so the screen never jumps away
mid-cook. Off by default, and all three stay device-local so each screen keeps
its own idle behaviour. Like the screensaver, it is offered on every mode.

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
| `streamdeck_ha_slots` (legacy) | | Kept, no editor | Kept, no editor | Inherited (read-only) |

`streamdeck_ha_slots` is legacy: the fixed HA 1 to HA 5 slot editor was retired
in favor of custom Home Assistant keys built with the entity picker. Slots a
device already has keep working and still sync to satellites; there is just no
way to create new ones.

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
also set once on the main server and inherited by every Pi Remote. The scheduled reboot (Settings, Display
& Sleep) applies only to a Pi appliance; the frequency can be Off,
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
| `local_device_pairing_enabled` | | Editable | Editable | Not applicable |

`secret_key` is auto-generated on first run on every mode and persisted so
sessions survive a restart.

`local_device_pairing_enabled` (default on) lets a new Pi Remote on the same
network request its own API key from this server during its setup wizard: the
device and the server both display a short code, and nothing is issued until a
signed-in user confirms the codes match under Settings, Bandit Remotes. Requests are
only accepted from private (LAN) addresses. Turn it off to require creating
and pasting keys by hand. Not applicable on a satellite, which hands out no
keys of its own.

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

Every device on the network can see every other device's shared printer, so a
printer attached to one device is usable from all of them (the print stack turns
on CUPS sharing and runs cups-browsed, which makes a peer's shared printer show
up as a local queue). `printing_enabled` is the master switch, off by default and
device-local: each device decides whether it prints, and a device without a
printer keeps printing off no matter what the server does.

Printers are set once on the main server: `label_printer_queue` and
`document_printer_queue` are the server's (and a standalone Pi Hosted box's)
own printer choices. A Pi Remote does not pick its own printer; it always
prints through the server's queues, and if it has a printer attached (over
Bluetooth or its network) that printer is shared up and chosen on the server
instead. `label_width_in`, `label_height_in`, `label_dpi`, `label_shape`
(rectangle, square, or round stock), `label_layout_presets` (named saved
designs), and the `document_page_size` / `document_color_mode` /
`document_duplex` document-printer options all describe a printer's label
stock and print settings, so they are set and shown only on the server (and a
standalone Pi Hosted box); the Printing pane hides the Label size and label
designer sections entirely on a Pi Remote.

The label design itself travels with the fleet: `label_layout_json` (the
saved design) and `label_show_logo` are designed once on the main server and
pulled by every satellite, so a satellite that prints (on its own attached
printer or the fleet queue) prints the layout you set up centrally instead of
a plain fallback. The design stores its field positions proportionally and
re-fits itself to whatever label stock the printing device is loaded with, so
the physical label size (`label_width_in`, `label_height_in`, `label_dpi`)
stays with each device and is never synced.

The main server also picks a fleet default with `fleet_label_printer_queue`
and `fleet_document_printer_queue`, which is what a satellite falls back to
for identifying its own resolved printer before it forwards a print job to
the server.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `printing_enabled` | | Device-local | Device-local | Device-local |
| `label_printer_queue` | | Editable | Editable | Not shown (prints through the server) |
| `document_printer_queue` | | Editable | Editable | Not shown (prints through the server) |
| `fleet_label_printer_queue` | | Set here (fleet default) | Set here (fleet default) | Inherited from server |
| `fleet_document_printer_queue` | | Set here (fleet default) | Set here (fleet default) | Inherited from server |
| `label_width_in` | | Editable | Editable | Device-local (no editor; not synced) |
| `label_height_in` | | Editable | Editable | Device-local (no editor; not synced) |
| `label_dpi` | | Editable | Editable | Device-local (no editor; not synced) |
| `label_shape` | | Editable | Editable | Not shown (server-only) |
| `label_layout_json` | | Editable | Editable | Inherited (read-only) |
| `label_layout_presets` | | Editable | Editable | Not shown (server-only) |
| `label_show_logo` | | Editable | Editable | Inherited (read-only) |
| `document_page_size` | | Editable | Editable | Not shown (server-only) |
| `document_color_mode` | | Editable | Editable | Not shown (server-only) |
| `document_duplex` | | Editable | Editable | Not shown (server-only) |

The print stack itself (CUPS, Bluetooth, and printer drivers) is off by default
and installed only when you ask for it: choose it during install, or press
Install now under Settings, Printing on a device without a printer set up yet.
That step writes two markers into the stack's environment file (not the settings
above): `CUPS_SERVER`, which points the app at the local print server, and
`PRINTING_ENABLED`, which the update process reads so an update keeps printing
working. Both are device-local and managed for you; you do not edit them by hand.

See [Printing labels and documents](printing.md) for the everyday walkthrough
and [Label printing hardware](hardware/label-printing.md) for the in-app
Bluetooth printer setup panel.

## Bluetooth thermometers (probes)

Bluetooth kitchen thermometers (Inkbird, ThermoPro including the TempSpike,
Combustion, ThermoWorks BlueDOT, and Govee grill thermometers) show live
probe temperatures on the Timers page and raise on-screen alerts, plus a
matching Home Assistant event, when a probe reaches its target. Settings,
Thermometers is the management surface in every mode (server, Pi Hosted, Pi
Remote): the feature toggle, the reader status, adding, renaming, and
removing thermometers, per-probe role overrides, and the Home Assistant
source (including Discover grills, which groups a grill's several HA
entities into one device) all live there; day-to-day temperatures, doneness
presets, and targets stay on the Timers page.

Readings can come from two sources, together or alone:

- **The Bluetooth reader.** The radio belongs to the host, not the app
  container, so a small host-side reader service reads the thermometers and
  reports to the app. On a Pi appliance the Thermometers & Sensors pane sets it up in
  one click (Set up for me, through the host bridge); it can also be chosen
  at provision time (`ENABLE_GADGETS=true`) or installed by hand
  (`sudo foodassistant-gadgets-setup`). On a plain server the reader is a
  host-side install: run `scripts/image-build/foodassistant-gadgets-setup`
  on a host with a Bluetooth radio (see `gadgets/README.md`); the pane
  shows those steps. A satellite (Pi Remote) with its own radio reads the
  same way and forwards what it finds to the main server, so those probes
  show up on both the server's Timers page and the satellite's own kiosk.
- **Home Assistant.** If Home Assistant already sees the thermometers
  (directly or through Bluetooth proxies), the app reads their temperature
  entities over the Home Assistant connection it already stores, so a
  server with no Bluetooth radio still gets probes and target alerts.

`gadgets_enabled` is the master switch, off by default; adding your first
thermometer (from Timers or Settings) turns it on. `gadget_devices` holds the
added thermometers, their names, per-probe role overrides, and per-probe
targets. `gadget_ha_enabled` turns the Home Assistant source on (adding an
entity does it for you) and `gadget_ha_entities` lists the entity ids read as
probes. `gadget_esp_enabled` turns the ESPHome source on (adding a device does
it for you) and `gadget_esp_devices` lists the ESP devices polled over WiFi,
each an entry with a host, a sensor id, and a name. All are device-local: a
thermometer lives near one device's radio (or is forwarded from a satellite
that owns the radio), and the Home Assistant entities and ESP devices are
polled by the device that lists them.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `gadgets_enabled` | | Device-local | Device-local | Device-local |
| `gadget_devices` | | Device-local | Device-local | Device-local |
| `gadget_ha_enabled` | | Device-local | Device-local | Device-local |
| `gadget_ha_entities` | | Device-local | Device-local | Device-local |
| `gadget_esp_enabled` | | Device-local | Device-local | Device-local |
| `gadget_esp_devices` | | Device-local | Device-local | Device-local |

Hygrometers (fridge, freezer, pantry, and room temperature + humidity
sensors: Govee H5075-class, Xiaomi LYWSD03MMC on the community ATC firmware,
SwitchBot Meter, Inkbird IBS-TH) are a separate device class read by the same
Bluetooth reader, with their own section in Settings, Thermometers & Sensors and their
own block on the Time & Temp page. `hygrometers_enabled` is the class's own
switch, off by default; adding your first hygrometer turns it on.
`hygrometer_devices` holds the added sensors with their names, location
labels, min/max alarm ranges, and alarm timing (the out-of-range grace
period and the optional stopped-reporting window; a reading outside its
range for longer than the grace raises an on-screen alarm that clears on
recovery). `gadget_ha_hygrometers` lists Home Assistant temperature +
humidity entity pairs read as hygrometers (under the same
`gadget_ha_enabled` toggle). Door contact sensors (Shelly BLU Door/Window
and other unencrypted BTHome v2 broadcasters, the SwitchBot Contact Sensor,
unencrypted Xiaomi door sensors) are their own class with the same shape:
`contacts_enabled` is the switch (adding the first sensor turns it on) and
`contact_devices` holds each sensor's name, location, and how long its door
may stay open before the alarm. All device-local, like the thermometer
fields.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `hygrometers_enabled` | | Device-local | Device-local | Device-local |
| `hygrometer_devices` | | Device-local | Device-local | Device-local |
| `gadget_ha_hygrometers` | | Device-local | Device-local | Device-local |
| `contacts_enabled` | | Device-local | Device-local | Device-local |
| `contact_devices` | | Device-local | Device-local | Device-local |

Shelf buttons (stick-anywhere BLE push buttons: BTHome v2 devices like the
Shelly BLU Button1, unencrypted Xiaomi MiBeacon switches) are read by the
same Bluetooth reader, with their own Shelf buttons section in Settings,
Thermometers. `buttons_enabled` is the class's own switch, off by default;
adding your first button turns it on. `button_devices` holds the added
buttons with their names and the per-press-type mappings (single, double,
and long press each mapped to a shopping-list product or an action token).
Both device-local, like the thermometer fields.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `buttons_enabled` | | Device-local | Device-local | Device-local |
| `button_devices` | | Device-local | Device-local | Device-local |

Plug-in accessories (STEMMA QT / Qwiic boards that connect straight to a Pi
appliance, starting with the NeoKey 1x4 scan-mode selector) have their own
Accessories section in Settings, Thermometers & Sensors. `stemma_enabled` is
the class's own switch, off by default; adding your first accessory turns it
on. `stemma_devices` holds the added boards, each keyed by its bus and
address (`i2c:1:0x30`), with a name and per-kind options (a NeoKey carries
its four key-to-mode mappings and its LED brightness). Both are device-local
and, unlike the Bluetooth classes, are never pulled from or mirrored to a
main server: a board is plugged into one device, so it belongs to that
device. A server has nothing to plug a board into, so the section renders but
reports no connection.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `stemma_enabled` | | Device-local (no bus) | Device-local | Device-local |
| `stemma_devices` | | Device-local (no bus) | Device-local | Device-local |
| `neokey_rest_color` | | Device-local (no bus) | Device-local | Device-local |
| `neokey_timer_color` | | Device-local (no bus) | Device-local | Device-local |

The two colors are what the keys wear: `neokey_rest_color` (default the brand
pink, `#F2006E`) away from the Manage screen, and `neokey_timer_color`
(default red, `#FF0000`) for the timer countdown bar and its finished-timer
flash. Both are picked on the device the pad is plugged into; a satellite
sends its own choice along with the LED poll, so the server never overrides
it.

A satellite usually stands in the kitchen with the Bluetooth radio while the
main server sits in a closet with none, so by default a satellite hands every
reading it hears to its server: the server then owns the sensor list, the
alarms, and the button actions for the whole house, and the satellite's own
screen shows the server's alarms too. `relay_gadgets_upstream` (on by default)
is the satellite's opt-out; it only renders on a Pi Remote, since nothing else
has a main server to relay to. `upstream_gadget_config` is the copy of the
server's sensor lists that the satellite mirrors on each sync, written by the
sync process rather than edited.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `relay_gadgets_upstream` | | Not shown | Not shown | Editable (satellite-only) |
| `upstream_gadget_config` | | Not applicable | Not applicable | Written automatically (satellite-only) |

## Bandit Cubs

What a Bandit Cub (a small companion display) shows is decided on the
server it polls, never on the device. `cub_default_view` is the idle view;
the takeover switches let running timers, an armed thermometer probe, or a
live fridge/door alarm seize the display (`cub_alerts_take_over`, on by
default, since spoiling groceries outrank everything); `cub_rotation` and
the two second-counts shape the idle rotation and the poll interval. These
live on a main install; a satellite's Cubs pair with the main server, so
the section does not render on Pi Remote.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `cub_default_view` | | Editable | Editable | Not shown |
| `cub_timers_take_over` | | Editable | Editable | Not shown |
| `cub_probes_take_over` | | Editable | Editable | Not shown |
| `cub_alerts_take_over` | | Editable | Editable | Not shown |
| `cub_rotation` | | Editable | Editable | Not shown |
| `cub_rotate_seconds` | | Editable | Editable | Not shown |
| `cub_poll_seconds` | | Editable | Editable | Not shown |
| `cub_auto_update` | | Editable | Editable | Not shown |
| `cub_ble_advertise` | | Editable | Editable | Not shown |
| `cub_ble_relay` | | Not shown (config/env) | Not shown (config/env) | Not shown |

`cub_auto_update` (on by default, matching `auto_update` for the app itself)
lets a Cub keep its own firmware current: it checks the server it is paired
with for the firmware that goes with this install's version and flashes it
when the kitchen is quiet. Turn it off and a Cub only updates when someone
asks it to. Per-Cub overrides work here like the rest of this section, so one
Cub can sit still while the rest of the fleet follows along. The switch lives
in the Bandit Cubs panel of the Bandit Remotes pane, and each Cub's card has a
"Firmware updates" choice that either follows the fleet or overrides it.

`cub_ble_relay` (off by default) is the beacon's mirror image: instead of a
device broadcasting to Cubs, the Cubs listen for the kitchen sensors and
forward what they hear to this server, which decodes it. That is what gives a
server with no Bluetooth radio the fridge and freezer sensors, door sensors,
and shelf buttons standing near a Cub. It needs no radio on the server (the
point of it), but it does need a Cub flashed with the relay build option on,
and the server hands each Cub the list of sensors it can read, so the flag is
the only thing to set. No settings-pane row yet.

`cub_ble_advertise` (off by default) turns on the Bluetooth status beacon: a
device running the thermometer reader broadcasts the Cub summary's numbers
(counts, soonest timer, one probe temperature; never names) for battery
displays in radio range. It takes a Bluetooth radio and the reader to
broadcast, in practice a Pi appliance, but the flag is editable on a Docker
server too: a server's Bandits pull it down with the rest of the gadget
config, so a radio-less server is what turns its kitchen Bandit's beacon on.

## Beszel monitoring hub

`beszel_enabled` and `beszel_url` control the optional link to a
[Beszel](https://github.com/henrygd/beszel) hub (a separate, self-hosted
monitoring dashboard with history and graphs) from the Resources pane, above
the always-available built-in live snapshot; see
[Device resources](device-resources.md). One hub serves the whole fleet, so
both fields are set once on the main server and pulled by satellites, same as
the fleet auto-update flag.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `beszel_enabled` | | Editable | Editable | Inherited (read-only) |
| `beszel_url` | | Editable | Editable | Inherited (read-only) |

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
now, with `stable` the recommended choice from release 0.8.0 on. `check_for_updates`
(FoodAssistant-31v4) is device-local, unlike `auto_update`: it controls only
whether THIS device's passive update-notice poller ever contacts GitHub on its
own. On by default; turning it off never disables the manual "Check for
updates" button, which is always a user-initiated, one-off check.

| Setting | Secret | Server | Pi Hosted | Pi Remote |
| --- | --- | --- | --- | --- |
| `debug_logging` | | Editable | Editable | Device-local |
| `auto_update` | | Editable | Editable | Inherited (read-only) |
| `update_channel` | | Editable | Editable | Inherited (read-only) |
| `check_for_updates` | | Editable | Editable | Device-local |
