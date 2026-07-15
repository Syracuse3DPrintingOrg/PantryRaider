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

# Options-flow toggle: after setup, mint a Home Assistant long-lived token and
# hand it back to a primary install so its cameras and Stream Deck keys reach HA
# without the user pasting anything. On by default; a user can turn it off.
CONF_CONNECT_BACK = "connect_back"
# One-shot marker (kept in entry options) so the token is minted once, not on
# every reload. Toggling connect_back off then on clears it to force a re-mint.
OPT_CONNECT_DONE = "connect_done"

# Name stamped on the refresh token we create for the owner so it is easy to
# find (and revoke) in the owner's profile, and so we can reuse-or-recreate it.
CONNECT_BACK_CLIENT_NAME = "Pantry Raider"

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
