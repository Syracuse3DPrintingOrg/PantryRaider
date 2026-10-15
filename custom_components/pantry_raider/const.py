"""Constants shared across the Pantry Raider integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "pantry_raider"

# The app listens on 9284 by default; a satellite answers /ha/state on plain
# port 80, which the coordinator handles by URL, not by this constant.
DEFAULT_PORT = 9284

# Poll cadence in seconds. The app's /ha/state is cheap, but a kitchen box may
# be small and fronting several kiosks, so 30s is a gentle default the user can
# raise in the options flow.
DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 600

CONF_SCAN_INTERVAL = "scan_interval"

# Kept in entry data when the entry was added without a key because pairing
# was turned off on the install, so setup can ask for it to be turned on.
CONF_PAIRING_OFF = "pairing_off"

# Options-flow toggle: after setup, mint a Home Assistant long-lived token and
# hand it back to a primary install so its cameras and Stream Deck keys reach HA
# without the user pasting anything. On by default; a user can turn it off.
CONF_CONNECT_BACK = "connect_back"
# One-shot marker (kept in entry options) so the token is minted once, not on
# every reload. Toggling connect_back off then on clears it to force a re-mint.
OPT_CONNECT_DONE = "connect_done"

# The name older versions of this integration gave the owner token they
# handed to every install. It is removed only once every install that ever
# received a token from Home Assistant uses its own limited account.
CONNECT_BACK_CLIENT_NAME = "Pantry Raider"

# Bookkeeping kept in each entry's options for the account and tokens this
# entry made. Only tokens recorded here are ever removed by this entry.
OPT_USER_ID = "ha_user_id"
OPT_TOKEN_ID = "ha_token_id"
OPT_TOKEN_CREATED = "ha_token_created"
OPT_TOKEN_KIND = "ha_token_kind"
OPT_TOKEN_GEN = "ha_token_gen"
OPT_OWNER_TOKEN_ID = "ha_owner_token_id"
# [{"token_id": ..., "after": ISO time}] tokens to remove once the install has
# had an hour to pass the new one on to its satellites.
OPT_PENDING_REMOVALS = "ha_pending_removals"
OPT_LEGACY_REMOVE_AFTER = "ha_legacy_remove_after"
# Short digests of the tokens this entry removed (helpers.token_ref_for_id),
# so an install found still holding one is given a new token.
OPT_REMOVED_REFS = "ha_removed_refs"

# The limited account's token lives 400 days and is replaced after a year.
DEDICATED_TOKEN_DAYS = 400
ROTATE_AFTER_DAYS = 365
# How long a replaced token keeps working after the install accepted its
# successor.
OLD_TOKEN_GRACE_SECONDS = 3600

# Repairs issue ids (each created as "<id>_<entry_id>").
ISSUE_PAIR_NEEDED = "pair_needed"
ISSUE_PAIRING_OFF = "pairing_off"
ISSUE_APP_UPDATE = "app_update_needed"
ISSUE_OPEN_GRACE = "open_install_grace"
ISSUE_OPEN_NAME = "open_install_name"
ISSUE_MANUAL_TOKEN = "manual_token"

# Sent as X-PR-Caps: this integration understands the pairing answer that
# repeats the key until its first use.
CLIENT_CAPS = "pair1"

# The zeroconf service installs advertise. The service port is the app port.
ZEROCONF_SERVICE_TYPE = "_pantry-raider._tcp.local."

# The settings the number/select entities may write back through POST
# /ha/settings. Kept as names here so the write path and the app agree.
ATTR_DISPLAY_IDLE_TIMEOUT = "display_idle_timeout"
ATTR_SCREENSAVER_MINUTES = "screensaver_minutes"
ATTR_SCREENSAVER_MODE = "screensaver_mode"
ATTR_WAKE_ON_PRESENCE = "wake_on_presence"

MANUFACTURER = "Pantry Raider"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NOTIFY,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
]

# Integration services (registered once for the domain, not per entry).
SERVICE_CAMERA_POPUP = "camera_popup"
SERVICE_CAMERA_DETECT = "camera_detect"
SERVICE_PROBE_ALERT = "probe_alert"

# The event the probe_alert service fires (the one automations already use).
EVENT_PROBE_ALERT = "pantry_raider_probe_alert"
