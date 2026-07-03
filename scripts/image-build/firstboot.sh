#!/usr/bin/env bash
# FoodAssistant first-boot provisioner
# ====================================
# Runs ONCE on first boot of a freshly flashed device. It:
#   1. Reads appliance config (image/config.env style file).
#   2. Installs Docker + Compose v2 if absent.
#   3. Sets hostname and configures mDNS (avahi) -> <hostname>.local.
#   4. Drops in the appliance compose file + .env and starts the stack
#      (FoodAssistant + Grocy; Mealie/Ollama opt-in).
#   5. Optionally installs a Chromium kiosk if ENABLE_KIOSK=true AND a display
#      is present.
#   6. Optionally installs the Stream Deck controller if ENABLE_STREAMDECK=true.
#
# Design notes
# ------------
# Idempotent: safe to re-run. Each step checks current state before acting, so
# a second run (e.g. after a crash) converges rather than duplicating work.
#
# Logs to LOG_FILE (default /var/log/foodassistant-firstboot.log).
#
# DRY_RUN=1 exercises all decision logic and prints the actions it WOULD take
# without installing packages, writing system files, or touching Docker. This
# is how the tooling is tested in CI / on a dev box (see tests/).
#
# Hardware: targets Raspberry Pi OS Lite (64-bit) on Pi 4 / Pi 5, and degrades
# gracefully on generic ARM64 / x86-64 Debian/Ubuntu. See
# docs/hardware/supported-hardware.md and docs/hardware/sd-image.md.
set -euo pipefail

# Tunables (env-overridable; mostly for tests)
DRY_RUN="${DRY_RUN:-0}"
# STEPS= comma-list of step names to run instead of the full sequence, bypassing
# the done-marker check and skipping mark_done.  Valid names: hostname, timezone,
# mdns, docker, stack, rotation, kiosk, streamdeck, hostbridge.  Leave empty (the
# default) to run all.
# Example: STEPS=streamdeck sudo bash firstboot.sh
STEPS="${STEPS:-}"
LOG_FILE="${LOG_FILE:-/var/log/foodassistant-firstboot.log}"
# Where to look for the appliance config, in priority order. The first that
# exists wins. /boot/firmware is the Pi OS Lite boot partition (user-editable
# from any machine after flashing); /boot is the legacy path.
CONFIG_CANDIDATES="${CONFIG_CANDIDATES:-/boot/firmware/foodassistant.config.env /boot/foodassistant.config.env /etc/foodassistant/config.env}"
# Directory containing the appliance compose file + this script's assets. On a
# baked image these live next to the script under /opt/foodassistant-setup.
ASSET_DIR="${ASSET_DIR:-$(cd "$(dirname "$0")" && pwd)}"
COMPOSE_SRC="${COMPOSE_SRC:-$ASSET_DIR/docker-compose.appliance.yml}"
# Marker so the systemd unit can disable itself after a successful run.
DONE_MARKER="${DONE_MARKER:-/var/lib/foodassistant/firstboot.done}"
# Path to a local clone of the repo. Used as the Docker build context when the
# pre-built GHCR image is unavailable (see deploy_stack). If it is missing when
# a build is needed, the provisioner clones REPO_URL here so a fresh device can
# build from source with no manual steps.
REPO_DIR="${REPO_DIR:-/home/foodassistant/FoodAssistant}"
REPO_URL="${REPO_URL:-https://github.com/Syracuse3DPrintingOrg/PantryRaider.git}"

# Logging helpers
log()  { printf '%s [firstboot] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
warn() { log "WARN: $*" >&2; }
die()  { log "ERROR: $*" >&2; exit 1; }

# run CMD...  — execute, or just announce under DRY_RUN.
run() {
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would run: $*"
    return 0
  fi
  log "run: $*"
  "$@"
}

# Architecture / OS detection (exported for steps + tests).
detect_arch() { uname -m; }
is_debian_like() { [ -f /etc/debian_version ]; }

# Config loading
# Locate and source the config file, then apply defaults for anything unset.
load_config() {
  local f found=""
  # bash auto-populates $HOSTNAME with the running host's name, which would
  # shadow our default. Clear it so the config file (or our default) wins; a
  # HOSTNAME= line in the config will re-set it when sourced below.
  unset HOSTNAME || HOSTNAME=""
  for f in $CONFIG_CANDIDATES; do
    if [ -f "$f" ]; then found="$f"; break; fi
  done
  if [ -n "$found" ]; then
    log "Loading config from $found"
    # shellcheck disable=SC1090
    . "$found"
  else
    warn "No config file found (looked in: $CONFIG_CANDIDATES); using defaults"
  fi

  # Defaults — chosen so a device works with NO config file at all. Display
  # and Stream Deck default to "auto": the provisioner detects the hardware and
  # turns each on when present. Timezone comes from the OS (set by Raspberry Pi
  # Imager), so nothing here needs editing for a turnkey flash-and-boot.
  HOSTNAME="${HOSTNAME:-foodassistant}"
  TZ="${TZ:-$(os_timezone)}"
  # Mealie default depends on the deployment mode, which is not resolved until
  # _load_mode_from_settings below. Leave it unset here and decide afterwards.
  ENABLE_MEALIE="${ENABLE_MEALIE:-}"
  ENABLE_OLLAMA="${ENABLE_OLLAMA:-false}"
  ENABLE_KIOSK="${ENABLE_KIOSK:-auto}"
  ENABLE_STREAMDECK="${ENABLE_STREAMDECK:-auto}"
  # Hide the mouse cursor in the kiosk:
  #   auto  - hide only when no pointer (mouse/touchpad) device is attached at
  #           provision time. A touch-only or Stream-Deck-only appliance then
  #           shows no stray cursor; plug in a mouse and the cursor returns.
  #   true  - always hide the cursor (even with a mouse attached).
  #   false - never hide; leave the normal cursor.
  # Applied by configure_kiosk via a transparent XCursor theme (see below).
  HIDE_CURSOR="${HIDE_CURSOR:-auto}"
  DISPLAY_ROTATION="${DISPLAY_ROTATION:-0}"
  # Touch driver for the kiosk display:
  #   auto     - try to detect (checks for SPI/ADS7846 and existing HID touch)
  #   ads7846  - SPI resistive (Waveshare HDMI displays, many small Pi screens)
  #   usb      - USB HID touch (larger HDMI touch monitors, connects via USB)
  #   none     - no touchscreen; skip all touch config
  # When ads7846 is active, dtoverlay=ads7846 is added to /boot/firmware/config.txt.
  # TOUCH_CALIBRATION_MATRIX can override the libinput coordinate transform:
  #   "1 0 0 0 1 0"    - identity (no transform, default)
  #   "0 1 0 -1 0 1"   - 90 degrees CW
  #   "-1 0 1 0 -1 1"  - 180 degrees
  #   "0 -1 1 1 0 0"   - 270 degrees CW
  # Leave empty to auto-derive from DISPLAY_ROTATION.
  TOUCH_DRIVER="${TOUCH_DRIVER:-auto}"
  TOUCH_CALIBRATION_MATRIX="${TOUCH_CALIBRATION_MATRIX:-}"
  # Type of attached display (mirrors DISPLAY_TYPES in the app's config.py):
  #   generic         - plain HDMI panel or USB HID touch monitor; no panel
  #                     specific overlay (the default; touch via TOUCH_DRIVER).
  #   waveshare_hdmi  - Waveshare HDMI touchscreen HAT. configure_touch then adds
  #                     a Waveshare dtoverlay and a touch udev rule so the panel's
  #                     controller is recognised as an input device.
  #   dsi_7inch       - MIPI DSI 7-inch panel (official Pi 7-inch or 800x480
  #                     clone). configure_touch writes dtoverlay=vc4-kms-dsi-7inch
  #                     so the panel comes up on Bookworm full KMS.
  #   ads7846_hdmi    - resistive HDMI panel with an ADS7846 SPI touch controller
  #                     (Waveshare 3.5-4 inch). configure_touch enables SPI and
  #                     writes the ads7846 overlay so the touch registers.
  # The web wizard writes this to settings.json; config.env can also set it.
  DISPLAY_TYPE="${DISPLAY_TYPE:-generic}"
  FOODASSISTANT_TAG="${FOODASSISTANT_TAG:-latest}"
  INSTALL_DIR="${INSTALL_DIR:-/opt/foodassistant}"

  # Deployment mode (see DEPLOYMENT_MODES in the app's config.py):
  #   pi_remote        - thin client: NO Docker/Grocy/Mealie here, just a kiosk
  #                      and/or Stream Deck pointed at REMOTE_SERVER_URL.
  #   pi_hosted/server - full local stack (the default behaviour).
  # The wizard writes the chosen mode to the app's settings.json; on a Pi Remote
  # box there is no local app to write it, so it comes from config.env instead.
  # An existing settings.json wins (a user can switch modes after first boot).
  DEPLOYMENT_MODE="${DEPLOYMENT_MODE:-}"
  REMOTE_SERVER_URL="${REMOTE_SERVER_URL:-}"
  _load_mode_from_settings

  # Resolve the Mealie default now that the mode is known. A pi_hosted appliance
  # is a full kitchen hub, so recipes and meal planning ship on by default; pull
  # the image during provisioning instead of making the user wait post-setup. A
  # satellite (pi_remote) runs no local backend, so it stays off there.
  if [ -z "$ENABLE_MEALIE" ]; then
    if is_remote_mode; then
      ENABLE_MEALIE="false"
    else
      ENABLE_MEALIE="true"
    fi
  fi

  # The kiosk URL defaults to this device, except in remote mode where it points
  # at the server being controlled. ?kiosk=1 latches kiosk mode in the browser
  # so the attached-display scale/orientation apply (and never affect others).
  if is_remote_mode; then
    # A satellite runs the full app locally on port 80 (it pulls its backend
    # config from the main server). The kiosk shows the LOCAL UI; if the device
    # is not configured yet, the app's setup-redirect sends /ui to /setup, so a
    # fresh box shows "configure me" without any special-casing here.
    KIOSK_URL="${KIOSK_URL:-http://localhost/ui/?kiosk=1}"
  else
    KIOSK_URL="${KIOSK_URL:-http://localhost:9284/ui/?kiosk=1}"
  fi

  log "Config: HOSTNAME=$HOSTNAME TZ=$TZ MODE=${DEPLOYMENT_MODE:-<default>} MEALIE=$ENABLE_MEALIE OLLAMA=$ENABLE_OLLAMA KIOSK=$ENABLE_KIOSK STREAMDECK=$ENABLE_STREAMDECK HIDE_CURSOR=$HIDE_CURSOR TAG=$FOODASSISTANT_TAG DIR=$INSTALL_DIR"
}

# True when this device is a thin remote control surface (no local stack).
is_remote_mode() { [ "${DEPLOYMENT_MODE:-}" = "pi_remote" ]; }

# Pull deployment_mode / remote_server_url / display_type from a settings.json
# the wizard may have written, so a choice made in the UI survives a re-provision.
# Best effort: a tiny grep-based read keeps us free of a python/jq dependency here.
_load_mode_from_settings() {
  local sf="${SETTINGS_JSON:-$INSTALL_DIR/data/settings.json}"
  [ -r "$sf" ] || return 0
  local mode url dtype
  mode="$(grep -o '"deployment_mode"[[:space:]]*:[[:space:]]*"[^"]*"' "$sf" 2>/dev/null | sed 's/.*"\([^"]*\)"$/\1/' || true)"
  url="$(grep -o '"remote_server_url"[[:space:]]*:[[:space:]]*"[^"]*"' "$sf" 2>/dev/null | sed 's/.*"\([^"]*\)"$/\1/' || true)"
  dtype="$(grep -o '"display_type"[[:space:]]*:[[:space:]]*"[^"]*"' "$sf" 2>/dev/null | sed 's/.*"\([^"]*\)"$/\1/' || true)"
  [ -n "$mode" ] && DEPLOYMENT_MODE="$mode"
  [ -n "$url" ] && REMOTE_SERVER_URL="$url"
  [ -n "$dtype" ] && DISPLAY_TYPE="$dtype"
  return 0
}

# Timezone the OS is already set to (Raspberry Pi Imager writes this), so the
# appliance matches the buyer's locale with no config. Falls back if unknown.
os_timezone() {
  local tz=""
  if command -v timedatectl >/dev/null 2>&1; then
    tz="$(timedatectl show -p Timezone --value 2>/dev/null)"
  fi
  [ -z "$tz" ] && [ -f /etc/timezone ] && tz="$(cat /etc/timezone 2>/dev/null)"
  echo "${tz:-America/New_York}"
}

# The interactive user (the account Raspberry Pi Imager created, uid 1000).
# Kiosk and Stream Deck run as this user; we never assume a fixed name.
primary_user() {
  [ -n "${APPLIANCE_USER:-}" ] && { echo "$APPLIANCE_USER"; return; }
  getent passwd 1000 | cut -d: -f1
}

# Resolve an enable flag that may be true / false / auto. For "auto", the
# detector function ($2) decides based on attached hardware.
flag_enabled() {
  case "${1:-}" in
    auto|AUTO|Auto) "$2" ;;
    *) is_true "$1" ;;
  esac
}

# Normalize a truthy config value to "true" / "false".
is_true() {
  case "${1:-}" in
    true|TRUE|True|1|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

# Build the list of compose --profile args from the enable flags.
# Echoes a space-separated string (possibly empty).
compose_profiles() {
  local profiles=""
  is_true "$ENABLE_MEALIE" && profiles="$profiles --profile with-mealie"
  is_true "$ENABLE_OLLAMA" && profiles="$profiles --profile with-ollama"
  # Trim leading space.
  printf '%s' "${profiles# }"
}

# Ensure a local clone of the repo exists at $REPO_DIR, cloning it if needed.
# A flashed/baked image carries the assets next to this script, but a device
# provisioned by piping this script through bash (curl ... | sudo bash) has
# only the script itself. In that case we clone the public repo so the compose
# file, service build context, and Stream Deck package are all available.
ensure_repo() {
  [ -d "$REPO_DIR/.git" ] && return 0
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would clone $REPO_URL to $REPO_DIR"
    return 0
  fi
  command -v git >/dev/null 2>&1 || apt_install git || warn "git install failed"
  command -v git >/dev/null 2>&1 \
    || die "git unavailable; cannot fetch repo assets. Install git and re-run."
  log "Cloning $REPO_URL to $REPO_DIR"
  run git clone --depth 1 "$REPO_URL" "$REPO_DIR" \
    || die "Could not clone $REPO_URL. Check internet access and try again."
}

# Step: hostname
configure_hostname() {
  local current
  current="$(hostname 2>/dev/null || echo unknown)"
  if [ "$current" = "$HOSTNAME" ]; then
    log "Hostname already '$HOSTNAME'; skipping"
    return 0
  fi
  log "Setting hostname '$current' -> '$HOSTNAME'"
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would set hostname and update /etc/hosts"
    return 0
  fi
  if command -v hostnamectl >/dev/null 2>&1; then
    hostnamectl set-hostname "$HOSTNAME"
  else
    echo "$HOSTNAME" > /etc/hostname
    hostname "$HOSTNAME" || true
  fi
  # Keep /etc/hosts consistent so sudo/local resolution stays fast.
  if grep -qE '^127\.0\.1\.1' /etc/hosts 2>/dev/null; then
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$HOSTNAME/" /etc/hosts
  else
    printf '127.0.1.1\t%s\n' "$HOSTNAME" >> /etc/hosts
  fi
}

# Step: timezone
configure_timezone() {
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would set timezone to $TZ"
    return 0
  fi
  if command -v timedatectl >/dev/null 2>&1; then
    timedatectl set-timezone "$TZ" 2>/dev/null \
      || warn "Could not set timezone $TZ"
  fi
}

# Step: mDNS (avahi)
# Makes the box reachable at <hostname>.local on the LAN. avahi-daemon publishes
# the host's name over multicast DNS, which resolves on Linux (nss-mdns),
# macOS/iOS (Bonjour, built in), and Windows when Apple Bonjour is installed
# (shipped with iTunes; otherwise users browse by IP). Enabling the daemon makes
# it publish <hostname>.local automatically from the system hostname.
configure_mdns() {
  if dpkg -s avahi-daemon >/dev/null 2>&1; then
    log "avahi-daemon already installed"
  else
    log "Installing avahi-daemon for mDNS"
    apt_install avahi-daemon
  fi
  # Enable so it publishes <hostname>.local now and on every boot. avahi reads
  # the system hostname, so configure_hostname must have run first (it does).
  run systemctl enable --now avahi-daemon || warn "avahi-daemon enable failed"
  log "mDNS configured: ${HOSTNAME}.local should resolve on Linux, macOS, and iOS"
  log "  (Windows clients need Apple Bonjour installed, else browse by IP)"
}

# Step: Docker + Compose v2
apt_install() {
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would: apt-get install -y $*"
    return 0
  fi
  DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
}

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "Docker + Compose v2 already present; skipping install"
    return 0
  fi
  if command -v docker >/dev/null 2>&1; then
    warn "Docker present but Compose v2 missing; installing compose plugin"
    apt_install docker-compose-plugin || warn "compose-plugin install failed"
    return 0
  fi
  log "Installing Docker via get.docker.com convenience script"
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would download and run https://get.docker.com"
    return 0
  fi
  # The convenience script supports Pi OS / Debian / Ubuntu on arm64 + amd64.
  local tmp
  tmp="$(mktemp)"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com -o "$tmp"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tmp" https://get.docker.com
  else
    die "Need curl or wget to install Docker"
  fi
  sh "$tmp" || die "Docker install failed"
  rm -f "$tmp"
  systemctl enable --now docker || warn "Could not enable docker service"
}

# Comma-separated compose profile names for COMPOSE_PROFILES (may be empty).
# Persisting this in the stack's .env means every later `docker compose`
# command run from INSTALL_DIR (a manual `up -d`, the host bridge, the OTA
# helper) operates on the same services without needing --profile flags, so
# an enabled Mealie can never be silently dropped by a profile-less call.
compose_profiles_csv() {
  local csv=""
  is_true "$ENABLE_MEALIE" && csv="with-mealie"
  is_true "$ENABLE_OLLAMA" && csv="${csv:+$csv,}with-ollama"
  printf '%s' "$csv"
}

# Step: deploy the stack
write_env_file() {
  local env_path="$1"
  local profiles_csv
  profiles_csv="$(compose_profiles_csv)"
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would write $env_path (TZ, FOODASSISTANT_TAG, COMPOSE_PROFILES=$profiles_csv)"
    return 0
  fi
  # Only create if absent so a re-run does not clobber user edits / secrets.
  if [ -f "$env_path" ]; then
    log "$env_path exists; leaving it untouched"
    return 0
  fi
  cat > "$env_path" <<EOF
# Generated by FoodAssistant first-boot provisioner. Edit and re-run
# 'docker compose up -d' to apply. The /setup wizard is the recommended way to
# add API keys.
TZ=$TZ
FOODASSISTANT_TAG=$FOODASSISTANT_TAG
COMPOSE_PROFILES=$profiles_csv
EOF
}

deploy_stack() {
  log "Deploying stack into $INSTALL_DIR"
  run mkdir -p "$INSTALL_DIR"
  seed_app_settings
  # The compose file normally sits next to this script (baked image / git
  # checkout). When the script is run standalone (curl ... | bash) it is not
  # present, so fall back to a cloned copy of the repo.
  if [ ! -f "$COMPOSE_SRC" ]; then
    log "Compose file not found at $COMPOSE_SRC; fetching repo assets"
    ensure_repo
    COMPOSE_SRC="$REPO_DIR/scripts/image-build/docker-compose.appliance.yml"
    [ -f "$COMPOSE_SRC" ] || [ "$DRY_RUN" = "1" ] \
      || die "Appliance compose file still not found at $COMPOSE_SRC after clone"
  fi
  run cp "$COMPOSE_SRC" "$INSTALL_DIR/docker-compose.yml"
  write_env_file "$INSTALL_DIR/.env"

  local profiles
  profiles="$(compose_profiles)"
  log "Compose profiles: ${profiles:-<none>}"

  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would run: (cd $INSTALL_DIR && docker compose $profiles pull service || docker compose $profiles build service && docker compose $profiles up -d)"
    return 0
  fi

  # Try the pre-built image first; fall back to building from local source
  # if the registry pull fails (e.g. image not yet public or no internet).
  # Export REPO_DIR so the compose build: context variable resolves correctly.
  export REPO_DIR
  # shellcheck disable=SC2086
  if ! ( cd "$INSTALL_DIR" && docker compose $profiles pull service ) 2>/dev/null; then
    log "Image pull failed; building from local source at $REPO_DIR/service (this takes a few minutes)"
    # Self-heal: a flashed device only carries the boot payload, not the full
    # repo. Clone it (the repo is public) so the build context exists.
    [ -d "$REPO_DIR/service" ] || ensure_repo
    # shellcheck disable=SC2086
    ( cd "$INSTALL_DIR" && docker compose $profiles build service ) \
      || die "Local build also failed. Check $REPO_DIR/service and Docker logs."
  fi
  # shellcheck disable=SC2086
  ( cd "$INSTALL_DIR" && docker compose $profiles up -d )
}

# Detect and install the Adafruit LSM6DSOX accelerometer rotation helper.
# If an LSM6DSOX is wired to I2C-1 (the Pi's default), install smbus2 and
# copy the helper script so the kiosk service can call it to auto-orient.
install_accel_rotation() {
  # Probe I2C-1 for the LSM6DSOX at 0x6A or 0x6B.
  if ! command -v i2cdetect >/dev/null 2>&1; then
    return 0  # i2c-tools not available; skip silently
  fi
  local found=0
  for addr in 6a 6b; do
    if i2cdetect -y 1 2>/dev/null | grep -q "$addr"; then
      found=1; break
    fi
  done
  [ "$found" = "0" ] && return 0

  log "LSM6DSOX detected on I2C-1; installing accelerometer rotation helper"
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN: would install smbus2 and foodassistant-accel-rotation"; return 0
  fi

  local helper=""
  for candidate in "$ASSET_DIR/foodassistant-accel-rotation" \
                   "$REPO_DIR/scripts/image-build/foodassistant-accel-rotation"; do
    [ -f "$candidate" ] && helper="$candidate" && break
  done
  if [ -z "$helper" ]; then
    warn "foodassistant-accel-rotation not found; skipping accelerometer install"
    return 0
  fi

  pip3 install --quiet --break-system-packages smbus2 2>/dev/null || warn "smbus2 install failed; accelerometer rotation may not work"
  cp "$helper" /usr/local/bin/foodassistant-accel-rotation
  chmod +x /usr/local/bin/foodassistant-accel-rotation
  log "Installed /usr/local/bin/foodassistant-accel-rotation"
  log "Add 'ExecStartPre=/usr/local/bin/foodassistant-accel-rotation' to foodassistant-kiosk.service to auto-orient at boot."
}

# Step: KMS display rotation
# Saves the requested kiosk rotation. cage uses the wlroots DRM backend, which
# ignores WLR_OUTPUT_TRANSFORM, so rotation is applied at runtime with wlr-randr
# (foodassistant-apply-rotation, run from the kiosk service ExecStartPost). The
# value is stored in /etc/foodassistant/kiosk-rotation. Only the boot console
# stays unrotated; the kiosk output rotates once cage is up.
configure_display_rotation() {
  local rot="${DISPLAY_ROTATION:-0}"
  local transform="normal"
  case "$rot" in
    0|"") transform="normal" ;;
    90|180|270) transform="$rot" ;;
    *) warn "DISPLAY_ROTATION=$rot is not valid (use 0, 90, 180, or 270); skipping"; return 0 ;;
  esac
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN: would save transform $transform (kiosk-rotation) and WLR_OUTPUT_TRANSFORM=$transform (kiosk-env)"
    return 0
  fi
  mkdir -p /etc/foodassistant
  # kiosk-rotation is the source of truth applied at runtime by
  # foodassistant-apply-rotation (via wlr-randr) on every kiosk start. kiosk-env
  # keeps WLR_OUTPUT_TRANSFORM in sync for dev/nested backends and so the host
  # bridge can report the current value.
  echo "$transform" > /etc/foodassistant/kiosk-rotation
  echo "WLR_OUTPUT_TRANSFORM=$transform" > /etc/foodassistant/kiosk-env
  log "Kiosk rotation set to ${rot} degrees (transform $transform; applied by wlr-randr when the kiosk starts)"
}

# Step: kiosk (opt-in, display-gated)
# Returns 0 if a display appears attached. An existing X/Wayland session or a
# connected DRM connector counts; a bare /dev/dri/card0 does NOT (vc4 KMS
# creates the card even on a headless Pi, which used to make "auto" install
# the kiosk everywhere). A wizard-selected display type also counts: DSI and
# SPI panels only report a connector once their overlay is active, which may
# be after the reboot this very run schedules.
# Test hooks: FORCE_DISPLAY=1 forces present, FORCE_DISPLAY=0/false forces
# absent, DRM_SYS_ROOT points the connector scan at a fake sysfs tree.
has_display() {
  case "${FORCE_DISPLAY:-}" in
    0|false|FALSE|no) return 1 ;;   # test hook: force absent
    ?*) return 0 ;;                 # test hook: force present
  esac
  [ -n "${WAYLAND_DISPLAY:-}" ] && return 0
  [ -n "${DISPLAY:-}" ] && return 0
  case "${DISPLAY_TYPE:-generic}" in
    generic|"") : ;;
    *) return 0 ;;                  # a chosen panel type means a display
  esac
  local st
  for st in "${DRM_SYS_ROOT:-/sys/class/drm}"/*/status; do
    [ -r "$st" ] && grep -qx connected "$st" 2>/dev/null && return 0
  done
  return 1
}

# Returns 0 when the host is a Raspberry Pi (reads the device-tree model). Used
# to decide which deployment modes apply and (eventually) to skip the heavy
# install on a thin-client Pi Remote. FORCE_PI overrides for tests.
is_raspberry_pi() {
  [ -n "${FORCE_PI:-}" ] && return 0   # test hook
  local f
  for f in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
    [ -r "$f" ] && tr -d '\0' < "$f" | grep -qi 'raspberry pi' && return 0
  done
  return 1
}

# Returns 0 if an Elgato Stream Deck (USB vendor 0fd9) is attached now.
has_streamdeck() {
  [ -n "${FORCE_STREAMDECK:-}" ] && return 0   # test hook
  if command -v lsusb >/dev/null 2>&1; then
    lsusb 2>/dev/null | grep -qi '0fd9:' && return 0
  fi
  grep -qil '0fd9' /sys/bus/usb/devices/*/idVendor 2>/dev/null && return 0
  return 1
}

# Return the Pi OS boot config path: /boot/firmware/config.txt on Bookworm+,
# /boot/config.txt on older releases. Returns empty string if neither exists.
_pi_config_txt() {
  if [ -f /boot/firmware/config.txt ]; then
    echo /boot/firmware/config.txt
  elif [ -f /boot/config.txt ]; then
    echo /boot/config.txt
  fi
}

# Measured libinput calibration matrix for the ADS7846 SPI panel (Waveshare 4"
# HDMI LCD and siblings) when driven by our dtoverlay with swapxy=1. An identity
# transform leaves taps badly offset and axis-crossed; this affine maps raw panel
# coordinates onto the screen. Derived from a 4-corner evtest measurement on the
# reference panel and validated to about 1 percent. Used as the default for
# ads7846 at the unrotated orientation; override per panel with
# TOUCH_CALIBRATION_MATRIX, or recompute on-device with foodassistant-touch-calibrate.
ADS7846_DEFAULT_MATRIX="0 1.3753 -0.1688 1.2635 0 -0.1166"

# Derive a libinput calibration matrix. Priority: an explicit
# TOUCH_CALIBRATION_MATRIX always wins; otherwise an ADS7846 panel at the
# unrotated orientation uses the measured default above; otherwise the matrix
# follows DISPLAY_ROTATION. The 6-float matrix maps touch-panel coordinates to
# screen coordinates. For a rotated ADS7846 panel, set TOUCH_CALIBRATION_MATRIX
# to the composed matrix (rotation times the measured affine).
_touch_calibration_matrix() {
  local driver="${1:-}"
  local m="${TOUCH_CALIBRATION_MATRIX:-}"
  if [ -n "$m" ]; then
    echo "$m"
    return
  fi
  if [ "$driver" = "ads7846" ] && [ "${DISPLAY_ROTATION:-0}" = "0" ]; then
    echo "$ADS7846_DEFAULT_MATRIX"
    return
  fi
  case "${DISPLAY_ROTATION:-0}" in
    90)  echo "0 -1 1 1 0 0" ;;
    180) echo "-1 0 1 0 -1 1" ;;
    270) echo "0 1 0 -1 0 1" ;;
    *)   echo "1 0 0 0 1 0" ;;
  esac
}

# Apply the touch calibration matrix to libinput. libinput reads the affine
# from the LIBINPUT_CALIBRATION_MATRIX udev property, so we set it with a udev
# rule that matches the panel by name. (There is no libinput quirks key for a
# calibration matrix; the older local-overrides.quirks AttrCalibrationMatrix
# approach is invalid and even breaks quirks parsing, so any stale file is
# removed here.) Works for X11 and Wayland (cage/wlroots reads libinput).
_write_touch_calibration() {
  local name_glob="$1"     # udev ATTRS{name} glob, e.g. "ADS7846*"
  local driver="${2:-}"    # touch driver, so ads7846 gets its measured default
  local matrix
  matrix="$(_touch_calibration_matrix "$driver")"
  local rules="/etc/udev/rules.d/99-foodassistant-touch.rules"

  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would write $rules (LIBINPUT_CALIBRATION_MATRIX name=$name_glob matrix=$matrix)"
    log "DRY_RUN would remove any stale /etc/libinput/local-overrides.quirks and reload udev"
    return 0
  fi

  # Remove the stale, invalid quirks file from earlier builds: an unknown
  # AttrCalibrationMatrix key makes libinput fail to parse every quirk.
  if [ -f /etc/libinput/local-overrides.quirks ] && \
     grep -q 'FoodAssistant touchscreen calibration' /etc/libinput/local-overrides.quirks 2>/dev/null; then
    rm -f /etc/libinput/local-overrides.quirks
    log "Removed stale libinput quirks file (AttrCalibrationMatrix is not a valid quirk key)"
  fi

  mkdir -p /etc/udev/rules.d
  cat > "$rules" <<EOF
# FoodAssistant touchscreen calibration. libinput reads the affine transform
# from the LIBINPUT_CALIBRATION_MATRIX udev property. Six floats a b c d e f
# map normalized device coordinates onto the screen.
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="${name_glob}", ENV{LIBINPUT_CALIBRATION_MATRIX}="${matrix}"
EOF
  udevadm control --reload-rules 2>/dev/null || true
  udevadm trigger --subsystem-match=input 2>/dev/null || true
  log "Wrote touch calibration udev rule: name=${name_glob} matrix=${matrix}"
}

# Install foodassistant-touch-calibrate: an on-device helper that captures four
# corner taps and writes a fitted AttrCalibrationMatrix. This is how an operator
# re-tunes a panel whose taps land off, without hand-computing the affine. The
# script is self-contained Python 3 (no extra pip deps) and parses evtest output.
_install_touch_calibrate_helper() {
  local dst="/usr/local/bin/foodassistant-touch-calibrate"
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would install $dst"
    return 0
  fi
  cat > "$dst" <<'PYEOF'
#!/usr/bin/env python3
"""Interactive touchscreen calibration for the FoodAssistant appliance.

Tap each of the four screen corners when prompted. The script reads the raw
ABS_X/ABS_Y values via evtest, fits the 2x3 affine that libinput applies to
normalized device coordinates, and writes /etc/libinput/local-overrides.quirks.
Re-run after a panel swap or if taps land off. Requires evtest (installed by the
provisioner). Run with sudo so it can write the quirk file.

Usage: foodassistant-touch-calibrate [/dev/input/eventN]
"""
import os
import re
import subprocess
import sys

RULES = "/etc/udev/rules.d/99-foodassistant-touch.rules"
STALE_QUIRK = "/etc/libinput/local-overrides.quirks"
CORNERS = [
    ("TOP-LEFT", 0.0, 0.0),
    ("TOP-RIGHT", 1.0, 0.0),
    ("BOTTOM-LEFT", 0.0, 1.0),
    ("BOTTOM-RIGHT", 1.0, 1.0),
]


def find_device():
    """Return the first event device that looks like a touchscreen."""
    try:
        blocks = open("/proc/bus/input/devices").read().split("\n\n")
    except OSError:
        return None
    for b in blocks:
        if re.search(r"touch", b, re.I):
            m = re.search(r"Handlers=.*?(event\d+)", b)
            if m:
                return "/dev/input/" + m.group(1)
    return None


def device_name(device):
    """Read the kernel input device name for a /dev/input/eventN path."""
    ev = os.path.basename(device)
    try:
        return open("/sys/class/input/%s/device/name" % ev).read().strip()
    except OSError:
        return ""


def read_minmax(device):
    """Read ABS_X / ABS_Y min and max from the evtest startup banner.

    evtest prints each axis capability (with Min/Max) before the "Testing"
    line, so capture stdout until that point and parse the ranges.
    """
    ranges = {}
    proc = subprocess.Popen(["evtest", device], stdout=subprocess.PIPE,
                            text=True)
    code = None
    try:
        for line in proc.stdout:
            cm = re.search(r"\(ABS_(X|Y)\)", line)
            if cm:
                code = cm.group(1)
            elif re.search(r"Event code \d+", line):
                # A different ABS axis (e.g. ABS_PRESSURE) -- stop attributing
                # its Min/Max to the last X/Y code we saw.
                code = None
            mm = re.search(r"(Min|Max)\s+(-?\d+)", line)
            if mm and code:
                ranges.setdefault(code, {})[mm.group(1)] = int(mm.group(2))
            if "Testing" in line:
                break
    finally:
        proc.terminate()
    return ranges


def capture_tap(device):
    """Block until one tap completes; return (raw_x, raw_y)."""
    proc = subprocess.Popen(["evtest", device], stdout=subprocess.PIPE,
                            text=True)
    x = y = None
    try:
        for line in proc.stdout:
            mx = re.search(r"\(ABS_X\), value (-?\d+)", line)
            my = re.search(r"\(ABS_Y\), value (-?\d+)", line)
            mr = re.search(r"\(BTN_TOUCH\), value 0", line)
            if mx:
                x = int(mx.group(1))
            elif my:
                y = int(my.group(1))
            elif mr and x is not None and y is not None:
                return x, y
    finally:
        proc.terminate()
    return None


def solve_affine(samples):
    """Least-squares fit of a 2x3 affine from normalized device pts to targets.

    samples: list of (nx, ny, tx, ty). Returns six floats a b c d e f where
    screen_x = a*nx + b*ny + c and screen_y = d*nx + e*ny + f.
    """
    # Normal equations for [a b c] and [d e f] share the same 3x3 design matrix.
    sxx = sxy = sx = syy = sy = n = 0.0
    bx0 = bx1 = bx2 = by0 = by1 = by2 = 0.0
    for nx, ny, tx, ty in samples:
        sxx += nx * nx
        sxy += nx * ny
        sx += nx
        syy += ny * ny
        sy += ny
        n += 1
        bx0 += nx * tx; bx1 += ny * tx; bx2 += tx
        by0 += nx * ty; by1 += ny * ty; by2 += ty
    A = [[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, n]]
    return _solve3(A, [bx0, bx1, bx2]) + _solve3(A, [by0, by1, by2])


def _solve3(A, b):
    """Solve a 3x3 linear system by Gaussian elimination."""
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for i in range(3):
        p = max(range(i, 3), key=lambda r: abs(M[r][i]))
        M[i], M[p] = M[p], M[i]
        if abs(M[i][i]) < 1e-12:
            raise ValueError("degenerate corners; tap distinct corners")
        for r in range(3):
            if r != i:
                f = M[r][i] / M[i][i]
                for c in range(i, 4):
                    M[r][c] -= f * M[i][c]
    return [M[i][3] / M[i][i] for i in range(3)]


def _write_rule(matrix, device=None):
    """Write the udev calibration rule and reload udev. Needs root."""
    name = device_name(device) if device else None
    match = ('ATTRS{name}=="%s", ' % name) if name else ""
    try:
        if os.path.exists(STALE_QUIRK):
            try:
                with open(STALE_QUIRK) as f:
                    if "FoodAssistant touchscreen calibration" in f.read():
                        os.remove(STALE_QUIRK)
            except OSError:
                pass
        os.makedirs("/etc/udev/rules.d", exist_ok=True)
        with open(RULES, "w") as f:
            f.write("# FoodAssistant touchscreen calibration (generated by "
                    "foodassistant-touch-calibrate)\n")
            f.write('SUBSYSTEM=="input", KERNEL=="event*", %s'
                    'ENV{LIBINPUT_CALIBRATION_MATRIX}="%s"\n' % (match, matrix))
    except OSError as e:
        print("Could not write %s (run with sudo): %s" % (RULES, e),
              file=sys.stderr)
        return 1
    subprocess.run(["udevadm", "control", "--reload-rules"])
    subprocess.run(["udevadm", "trigger", "--subsystem-match=input"])
    print("Wrote %s." % RULES)
    return 0


def main():
    # Non-interactive mode: just write a matrix supplied by the caller.
    if len(sys.argv) >= 3 and sys.argv[1] == "--apply-matrix":
        parts = sys.argv[2].strip().split()
        if len(parts) != 6:
            print("Expected 6 floats; got %d." % len(parts), file=sys.stderr)
            return 2
        try:
            [float(p) for p in parts]
        except ValueError as e:
            print("Non-numeric value in matrix: %s" % e, file=sys.stderr)
            return 2
        device = find_device()
        return _write_rule(sys.argv[2].strip(), device)

    device = sys.argv[1] if len(sys.argv) > 1 else find_device()
    if not device or not os.path.exists(device):
        print("No touch device found. Pass one explicitly: "
              "foodassistant-touch-calibrate /dev/input/eventN", file=sys.stderr)
        return 2
    print("Calibrating %s" % device)
    ranges = read_minmax(device)
    try:
        xmin, xmax = ranges["X"]["Min"], ranges["X"]["Max"]
        ymin, ymax = ranges["Y"]["Min"], ranges["Y"]["Max"]
    except KeyError:
        print("Could not read ABS ranges from evtest.", file=sys.stderr)
        return 2
    samples = []
    for name, tx, ty in CORNERS:
        input("Tap and release the %s corner, then press Enter to continue..."
              % name)
        pt = capture_tap(device)
        if not pt:
            print("No tap captured; aborting.", file=sys.stderr)
            return 2
        rx, ry = pt
        nx = (rx - xmin) / (xmax - xmin) if xmax != xmin else 0.0
        ny = (ry - ymin) / (ymax - ymin) if ymax != ymin else 0.0
        samples.append((nx, ny, tx, ty))
        print("  %s raw=(%d,%d) normalized=(%.3f,%.3f)" % (name, rx, ry, nx, ny))
    coeffs = solve_affine(samples)
    matrix = " ".join("%.4f" % c for c in coeffs)
    print("Fitted LIBINPUT_CALIBRATION_MATRIX: %s" % matrix)
    rc = _write_rule(matrix, device)
    if rc == 0:
        print("Restart the kiosk (or reboot) to apply.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
PYEOF
  chmod +x "$dst"
  log "Installed touch calibration helper: $dst"
}

# Waveshare HDMI touchscreen HAT support (DISPLAY_TYPE=waveshare_hdmi).
# These panels output video over HDMI and report touch over a USB HID link. The
# video side works with no extra config, but the touch controller often needs a
# device tree overlay plus an evdev/libinput hint before it is registered as an
# input device. This is gated entirely on DISPLAY_TYPE so it never runs for a
# non-Waveshare device.
#
# NOTE: the exact dtoverlay name depends on the specific Waveshare panel model.
# We apply the commonly documented "vc4-kms-v3d" KMS pipeline plus the generic
# "waveshare-touch" hint below, which covers the USB-HID Goodix/ADS style panels
# most of these HATs ship. If a particular panel needs a different overlay (some
# Waveshare wikis list a model specific name), set WAVESHARE_TOUCH_OVERLAY in
# config.env to override the overlay line written here.
_configure_waveshare_hdmi_touch() {
  local cfg
  cfg="$(_pi_config_txt)"

  # Boot overlay. KMS must be on for HDMI output (Pi OS Bookworm enables
  # vc4-kms-v3d by default; we add it only if absent). The touch overlay name
  # varies per panel; the default below is the common Waveshare HDMI-touch one
  # and can be overridden with WAVESHARE_TOUCH_OVERLAY.
  local touch_overlay="${WAVESHARE_TOUCH_OVERLAY:-waveshare-hdmi-touch}"
  if [ -z "$cfg" ]; then
    warn "No Pi boot config.txt found; cannot write Waveshare HDMI touch overlay"
  elif grep -q "dtoverlay=${touch_overlay}" "$cfg" 2>/dev/null; then
    log "Waveshare touch overlay (${touch_overlay}) already present in $cfg; leaving untouched"
  elif [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would append Waveshare HDMI touch overlay to $cfg: dtoverlay=${touch_overlay}"
  else
    # The overlay name depends on the exact Waveshare panel model; adjust it
    # (or set WAVESHARE_TOUCH_OVERLAY) if touch is still not detected. Many of
    # these HATs are plain USB HID and work with no overlay at all, in which
    # case the udev rule below is enough; the overlay line is harmless if the
    # firmware has no matching overlay file (it is ignored at boot).
    printf '\n# FoodAssistant: Waveshare HDMI touchscreen (overlay name varies by panel model)\ndtoverlay=%s\n' "$touch_overlay" >> "$cfg"
    log "Appended Waveshare HDMI touch overlay (dtoverlay=${touch_overlay}) to $cfg (takes effect after reboot)"
  fi

  # udev / libinput rule. The Waveshare HDMI-touch controllers report as USB HID
  # touchscreens but some do not get tagged for libinput automatically, so cage
  # and Chromium never see touch events. Tag the input device as a touchscreen
  # so libinput (and thus wlroots/Chromium) picks it up. Best-effort broad match
  # on the vendor strings Waveshare panels report; harmless on other hardware.
  local rules="/etc/udev/rules.d/98-foodassistant-waveshare-touch.rules"
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would write $rules (tag Waveshare HDMI touch controller for libinput)"
  else
    mkdir -p /etc/udev/rules.d
    cat > "$rules" <<'EOF'
# FoodAssistant: tag the Waveshare HDMI touchscreen controller so libinput (and
# thus cage/wlroots and Chromium) treats it as a touchscreen input device. The
# match is broad on purpose: Waveshare HDMI-touch HATs report a range of vendor
# names (Goodix, ADS, "WaveShare"). Adjust the ATTRS{name} glob if a specific
# panel reports a different name and its touch is still not registered.
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="*WaveShare*", ENV{ID_INPUT_TOUCHSCREEN}="1", ENV{LIBINPUT_DEVICE_GROUP}="waveshare-hdmi-touch"
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="*Waveshare*", ENV{ID_INPUT_TOUCHSCREEN}="1", ENV{LIBINPUT_DEVICE_GROUP}="waveshare-hdmi-touch"
SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="*Goodix*", ENV{ID_INPUT_TOUCHSCREEN}="1"
EOF
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger --subsystem-match=input 2>/dev/null || true
    log "Wrote Waveshare HDMI touch udev rule: $rules"
  fi

  # Apply a libinput calibration matrix matching the panel name so taps land
  # correctly (identity by default; follows DISPLAY_ROTATION / an explicit
  # TOUCH_CALIBRATION_MATRIX). Mirrors the USB HID touch path above.
  _write_touch_calibration "*WaveShare*"
}

# MIPI DSI 7-inch panel support (DISPLAY_TYPE=dsi_7inch). Covers the official
# Raspberry Pi 7-inch touchscreen and 800x480 driver-free clones (e.g. Hosyond).
# Bookworm's full KMS pipeline removed DSI panel auto-detection, so the panel
# stays dark until dtoverlay=vc4-kms-dsi-7inch is added to config.txt. The panel
# reports touch over I2C (Goodix/FT5406), which libinput picks up once the
# overlay brings the panel up. invx/invy can flip the touch axes for an upside
# down mount via DSI_TOUCH_INVERT (e.g. "invx", "invy", or "invx,invy").
# Confirmed on a Pi4 with a Hosyond 7-inch DSI.
_configure_dsi_7inch() {
  local cfg
  cfg="$(_pi_config_txt)"
  local overlay="vc4-kms-dsi-7inch"
  # Optional touch-axis inversion appended to the overlay (invx / invy).
  local invert="${DSI_TOUCH_INVERT:-}"
  local overlay_line="dtoverlay=${overlay}"
  if [ -n "$invert" ]; then
    overlay_line="dtoverlay=${overlay},${invert}"
  fi
  if [ -z "$cfg" ]; then
    warn "No Pi boot config.txt found; cannot write DSI 7-inch overlay"
  elif grep -q "dtoverlay=${overlay}" "$cfg" 2>/dev/null; then
    log "DSI 7-inch overlay (${overlay}) already present in $cfg; leaving untouched"
  elif [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would append DSI 7-inch overlay to $cfg: ${overlay_line}"
  else
    printf '\n# FoodAssistant: MIPI DSI 7-inch touchscreen (official / 800x480 clone)\n%s\n' "$overlay_line" >> "$cfg"
    log "Appended DSI 7-inch overlay (${overlay_line}) to $cfg (takes effect after reboot)"
  fi

  # The DSI panel's touch controller (Goodix on the official panel, FT5406 on
  # many clones) is an I2C touchscreen libinput recognises once the overlay is
  # active. Apply a calibration matrix matching the rotation so taps land right.
  _write_touch_calibration "*Goodix*"
}

configure_touch() {
  local driver="${TOUCH_DRIVER:-auto}"

  # A MIPI DSI 7-inch panel needs the vc4-kms-dsi-7inch overlay to light up on
  # Bookworm full KMS. Apply it here, gated on the wizard's display type, then
  # fall through so the verification tools and calibration helper install too.
  if [ "${DISPLAY_TYPE:-generic}" = "dsi_7inch" ]; then
    log "Display type is dsi_7inch; configuring MIPI DSI 7-inch touchscreen"
    if [ "$DRY_RUN" = "1" ]; then
      log "DRY_RUN would install touch verification tools: libinput-tools evtest"
    else
      apt_install libinput-tools evtest || warn "touch tools (libinput-tools/evtest) install failed; on-device calibration check unavailable"
    fi
    _install_touch_calibrate_helper
    _configure_dsi_7inch
    return 0
  fi

  # Explicit opt-out
  if [ "$driver" = "none" ]; then
    log "Touch driver set to none; skipping touch configuration"
    return 0
  fi

  # A Waveshare HDMI touchscreen HAT needs its own overlay + udev rule for the
  # touch controller to be registered. Apply that here, gated on the display
  # type chosen in the wizard. The generic touch handling below still runs so
  # the verification tools and calibration helper are installed too.
  if [ "${DISPLAY_TYPE:-generic}" = "waveshare_hdmi" ]; then
    log "Display type is waveshare_hdmi; configuring Waveshare HDMI touchscreen"
    if [ "$DRY_RUN" = "1" ]; then
      log "DRY_RUN would install touch verification tools: libinput-tools evtest"
    else
      apt_install libinput-tools evtest || warn "touch tools (libinput-tools/evtest) install failed; on-device calibration check unavailable"
    fi
    _install_touch_calibrate_helper
    _configure_waveshare_hdmi_touch
    return 0
  fi

  # A resistive HDMI panel with an ADS7846 SPI controller (Waveshare 3.5-4 inch
  # HDMI LCD and similar) reports no input device until SPI and the ads7846
  # overlay are configured. Auto-detect cannot find it because SPI is off at
  # first boot, so selecting this display type forces the ads7846 driver path
  # below, which writes the overlay and enables SPI.
  if [ "${DISPLAY_TYPE:-generic}" = "ads7846_hdmi" ]; then
    log "Display type is ads7846_hdmi; forcing ADS7846 SPI touch"
    driver="ads7846"
  fi

  # Auto-detect: check for SPI bus (ADS7846 candidate) or existing HID touch
  if [ "$driver" = "auto" ]; then
    if ls /dev/spidev* >/dev/null 2>&1 || \
       grep -qr 'ads7846\|ADS7846' /sys/bus/spi/devices/ 2>/dev/null; then
      log "Auto-detected SPI bus; assuming ADS7846 touch controller"
      driver="ads7846"
    elif find /dev/input -name 'event*' 2>/dev/null | \
         xargs -I{} grep -lqE 'touchscreen|Touch' /sys/class/input/*/device/name 2>/dev/null; then
      log "Auto-detected HID touch input device; driver=usb"
      driver="usb"
    else
      log "No touch device auto-detected (TOUCH_DRIVER=auto). Set TOUCH_DRIVER=ads7846 or usb in config.env if a touchscreen is attached, then re-run with STEPS=touch."
      return 0
    fi
  fi

  log "Configuring touch driver: $driver"

  # Tools for confirming and re-tuning the calibration on-device:
  #   libinput-tools -> `libinput quirks list <dev>` and `libinput list-devices`
  #   evtest         -> raw corner-tap capture used by foodassistant-touch-calibrate
  # Best-effort: a missing package must not abort provisioning.
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would install touch verification tools: libinput-tools evtest"
  else
    apt_install libinput-tools evtest || warn "touch tools (libinput-tools/evtest) install failed; on-device calibration check unavailable"
  fi
  _install_touch_calibrate_helper

  if [ "$driver" = "ads7846" ]; then
    # ADS7846 is a SPI resistive touch controller used on Waveshare HDMI
    # panels and many small Pi HAT displays. It needs a device tree overlay
    # in config.txt. The defaults below work for Waveshare 3.5"-4" HDMI LCD;
    # adjust cs/penirq/speed in config.env for other layouts.
    local cfg
    cfg="$(_pi_config_txt)"
    if [ -z "$cfg" ]; then
      warn "No Pi boot config.txt found; cannot write dtoverlay for ADS7846"
    else
      # penirq_pull=2 sets a pull-up on the PENIRQ GPIO. PENIRQ is active-low
      # (driven low on touch), so without the pull-up the line floats and the
      # controller registers no touches. Waveshare's own config includes it.
      local overlay_line="dtoverlay=ads7846,cs=1,penirq=25,penirq_pull=2,speed=50000,keep_vcc,swapxy=1,pmax=255,xohms=150,xmin=200,xmax=3900,ymin=200,ymax=3900"
      if grep -q 'dtoverlay=ads7846' "$cfg" 2>/dev/null; then
        log "ads7846 dtoverlay already present in $cfg; leaving untouched"
      elif [ "$DRY_RUN" = "1" ]; then
        log "DRY_RUN would append to $cfg: $overlay_line"
      else
        printf '\n# FoodAssistant: ADS7846 SPI touch\n%s\n' "$overlay_line" >> "$cfg"
        log "Appended ads7846 dtoverlay to $cfg (takes effect after reboot)"
      fi
    fi
    # SPI must also be enabled for the overlay to work. Add dtparam=spi=on if
    # missing (idempotent: do nothing if already present or if no config found).
    if [ -n "$cfg" ] && ! grep -q 'dtparam=spi=on' "$cfg" 2>/dev/null; then
      if [ "$DRY_RUN" = "1" ]; then
        log "DRY_RUN would append dtparam=spi=on to $cfg"
      else
        printf 'dtparam=spi=on\n' >> "$cfg"
        log "Enabled SPI (dtparam=spi=on) in $cfg"
      fi
    fi
    _write_touch_calibration "ADS7846*" "ads7846"
  fi

  if [ "$driver" = "usb" ]; then
    # USB HID touch panels enumerate as generic input devices and need no kernel
    # overlay, but the coordinate axes are sometimes mirrored or rotated. Write
    # a calibration quirk with a broad match so most panels are covered. The
    # matrix defaults to identity; set TOUCH_CALIBRATION_MATRIX in config.env
    # if the touch is mis-mapped.
    _write_touch_calibration "*Touchscreen*"
  fi
}

# Returns 0 when a pointer device (mouse, trackball, touchpad) is attached now.
# Touchscreens are NOT pointers for our purposes: a touch panel reports absolute
# touch events, not a moving cursor, so a touch-only box has no pointer here.
# FORCE_POINTER overrides for tests. When the variable is set it is
# authoritative: a non-empty value means a pointer is present, an empty value
# means none. Leaving it unset falls through to real hardware detection, so
# production is unaffected. (An empty value must force "absent" rather than
# fall through, or the result depends on whatever input devices the test host
# happens to expose.)
has_pointer_device() {
  if [ -n "${FORCE_POINTER+set}" ]; then   # test hook: set means authoritative
    [ -n "$FORCE_POINTER" ] && return 0 || return 1
  fi
  # by-path symlinks libinput/udev create for relative pointing devices.
  local p
  for p in /dev/input/by-path/*event-mouse* /dev/input/by-id/*event-mouse*; do
    [ -e "$p" ] && return 0
  done
  # Fall back to the device names the kernel exposes; match Mouse/Touchpad but
  # deliberately not Touchscreen.
  if ls /sys/class/input/*/device/name >/dev/null 2>&1; then
    grep -qiE 'mouse|touchpad|trackball|trackpad' /sys/class/input/*/device/name 2>/dev/null && return 0
  fi
  return 1
}

# Decide whether to hide the cursor, resolving HIDE_CURSOR=auto against the
# attached hardware. Echoes "true" or "false". auto hides only when no pointer
# device is present at provision time.
_resolve_hide_cursor() {
  case "${HIDE_CURSOR:-auto}" in
    auto|AUTO|Auto)
      if has_pointer_device; then echo "false"; else echo "true"; fi
      ;;
    *)
      if is_true "$HIDE_CURSOR"; then echo "true"; else echo "false"; fi
      ;;
  esac
}

# Ship a fully-transparent XCursor theme so wlroots/cage renders an invisible
# pointer. cage has no native hide-cursor flag on the Pi OS versions we target
# (see cage-kiosk/cage issues #235, #299, #422), and there is no Chromium or
# WLR_* flag that hides it. The portable, well-known kiosk fix is to point the
# cursor theme at a transparent cursor via XCURSOR_PATH/XCURSOR_THEME, which the
# unit's Environment lines do.
#
# We write the Xcursor binary directly with printf rather than depend on
# xcursorgen (not always installable). The bytes below are a valid single-image
# Xcursor: "Xcur" magic, a 16-byte file header, one TOC entry pointing at one
# 1x1 image chunk whose only pixel is ARGB 0x00000000 (fully transparent).
# `file` reports this as "X11 cursor". Format ref: x.org Xcursor(3).
# Echoes the theme name on success; returns non-zero (and the caller falls back
# to the normal cursor) if the files cannot be written.
_install_blank_cursor_theme() {
  local theme="foodassistant-hidden"
  local base="/usr/share/icons/$theme"
  local cdir="$base/cursors"
  mkdir -p "$cdir" || return 1
  # The single transparent cursor file.
  if ! printf '\130\143\165\162\020\000\000\000\000\000\001\000\001\000\000\000\002\000\375\377\001\000\000\000\034\000\000\000\044\000\000\000\002\000\375\377\001\000\000\000\001\000\000\000\001\000\000\000\001\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000' > "$cdir/left_ptr"; then
    return 1
  fi
  # Point the common cursor names at the one transparent cursor so whatever
  # name an app asks for resolves to the invisible pointer. Symlinks keep it
  # to a single real file; libxcursor follows them.
  local name
  for name in default left_ptr_watch watch text xterm hand1 hand2 pointer \
              top_left_arrow arrow crosshair fleur grabbing; do
    ln -sf left_ptr "$cdir/$name" 2>/dev/null || true
  done
  cat > "$base/index.theme" <<'THEME'
[Icon Theme]
Name=FoodAssistant Hidden Cursor
Comment=Fully transparent cursor for touch/Stream Deck kiosks
THEME
  echo "$theme"
}

configure_kiosk() {
  if ! flag_enabled "$ENABLE_KIOSK" has_display; then
    log "Kiosk not enabled (ENABLE_KIOSK=$ENABLE_KIOSK); skipping"
    return 0
  fi
  if ! has_display; then
    # Only reachable with an explicit ENABLE_KIOSK=true (auto already skipped
    # above). An explicit flag wins: install anyway, the service simply starts
    # once a display is attached.
    warn "Kiosk enabled but no display detected; installing anyway (ENABLE_KIOSK=$ENABLE_KIOSK), it starts when a display is attached"
  fi
  log "Installing Chromium kiosk via cage (Wayland) for $KIOSK_URL"

  # Decide whether to hide the cursor (HIDE_CURSOR=auto/true/false). Resolved
  # early so the choice is visible (and testable) even in DRY_RUN. auto hides
  # only when no pointer device is attached at provision time.
  local hide_cursor
  hide_cursor="$(_resolve_hide_cursor)"
  if [ "$hide_cursor" = "true" ]; then
    log "Cursor will be hidden (HIDE_CURSOR=$HIDE_CURSOR; no pointer device or forced)"
  else
    log "Cursor will be shown (HIDE_CURSOR=$HIDE_CURSOR)"
  fi

  # cage = minimal single-app Wayland compositor; chromium = browser. Install
  # them on separate apt lines and try both browser package names. Bundling
  # them in one "apt-get install cage chromium" meant a single unmatched name
  # (the browser is "chromium" on Bookworm, "chromium-browser" on older Pi OS)
  # aborted the whole line and left cage uninstalled too, so the unit crash
  # looped on status=203/EXEC.
  apt_install cage || warn "cage install failed"
  apt_install chromium || apt_install chromium-browser \
    || warn "chromium install failed"
  # wlr-randr applies the display rotation at runtime (cage ignores
  # WLR_OUTPUT_TRANSFORM on the DRM backend). Best-effort: rotation just stays
  # at normal if it is missing.
  apt_install wlr-randr || warn "wlr-randr install failed; display rotation will not apply"
  # seatd brokers DRM/VT access to the kiosk user (FoodAssistant-hmr3). On a
  # headless-provisioned Pi the kiosk user has no active seat0 logind session,
  # so libseat's builtin (root-only) backend denies cage (running as the user)
  # access to tty0/DRM and the unit crash-loops. seatd runs as root and hands a
  # session to members of the _seatd group, which is the reliable fix here.
  apt_install seatd || warn "seatd install failed; kiosk may not get a DRM session"

  # Bake absolute binary paths into the unit. cage execs the browser via PATH,
  # but a systemd service runs with a minimal environment, so we resolve full
  # paths here and skip cleanly if either binary is missing rather than leave a
  # unit that fails to exec on every restart.
  local cage_bin chromium_bin
  cage_bin="$(command -v cage || true)"
  chromium_bin="$(command -v chromium || command -v chromium-browser || true)"
  if [ -z "$cage_bin" ] || [ -z "$chromium_bin" ]; then
    warn "kiosk binaries missing (cage=${cage_bin:-none} chromium=${chromium_bin:-none}); skipping kiosk service"
    return 0
  fi

  # cage needs a real logind seat session, which a bare root service does not
  # get (it fails with "XDG_RUNTIME_DIR is not set" / libseat tty errors). We
  # run it as the interactive user via PAMName=login, owning tty1 in place of
  # the getty. --remote-debugging-port lets the Stream Deck drive the browser.
  local kuser kuid
  kuser="$(primary_user)"
  if [ -z "$kuser" ]; then
    warn "No interactive (uid 1000) user found; cannot run kiosk seat session. Skipping."
    return 0
  fi
  kuid="$(id -u "$kuser")"

  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would run kiosk as $kuser (uid $kuid), disable getty@tty1, enable seatd, add $kuser to _seatd, write foodassistant-kiosk.service"
    return 0
  fi

  loginctl enable-linger "$kuser" || warn "enable-linger failed"
  systemctl disable getty@tty1.service 2>/dev/null || true

  # Grant the kiosk user a DRM/VT session via seatd (FoodAssistant-hmr3).
  # Idempotent: usermod -aG is additive, and enable --now is a no-op if seatd
  # is already running. The unit sets LIBSEAT_BACKEND=seatd so cage uses this
  # broker instead of the root-only builtin backend.
  if getent group _seatd >/dev/null 2>&1; then
    usermod -aG _seatd "$kuser" || warn "could not add $kuser to _seatd group"
  else
    warn "_seatd group missing (seatd not installed?); kiosk DRM session may fail"
  fi
  systemctl enable --now seatd 2>/dev/null || warn "seatd enable/start failed"

  # When hiding, install the transparent cursor theme and prepare the extra
  # Environment lines for the unit. If theme install fails we leave the cursor
  # visible rather than risk a broken unit.
  local cursor_env=""
  if [ "$hide_cursor" = "true" ]; then
    local theme
    theme="$(_install_blank_cursor_theme || true)"
    if [ -n "$theme" ]; then
      cursor_env="Environment=XCURSOR_PATH=/usr/share/icons
Environment=XCURSOR_THEME=$theme
Environment=XCURSOR_SIZE=24"
      log "Installed transparent cursor theme '$theme' for the kiosk"
    else
      warn "Could not install transparent cursor theme; cursor will remain visible"
    fi
  fi

  cat > /etc/systemd/system/foodassistant-kiosk.service <<EOF
[Unit]
Description=FoodAssistant Chromium kiosk
After=foodassistant.target systemd-user-sessions.service getty@tty1.service network-online.target seatd.service
Wants=network-online.target seatd.service
Conflicts=getty@tty1.service
# The kiosk is the only thing on the display: never give up on it. Early-boot
# crashes (DRM not ready, seatd racing up) otherwise leave the screen stuck on
# the boot console until someone starts the unit by hand.
StartLimitIntervalSec=0

[Service]
EnvironmentFile=-/etc/foodassistant/kiosk-env
Type=simple
# Room for the app wait (ExecStartPre) plus the rotation apply (ExecStartPost)
# on a slow first boot; the default 90s start timeout is too tight for a Pi 3.
TimeoutStartSec=240
User=$kuser
PAMName=login
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
StandardInput=tty
StandardOutput=journal
StandardError=journal
UtmpIdentifier=tty1
UtmpMode=user
Environment=XDG_RUNTIME_DIR=/run/user/$kuid
Environment=LIBSEAT_BACKEND=seatd
${cursor_env:+$cursor_env
}# Wait (bounded, best-effort) for the app to answer before launching the
# browser. Chromium renders a connection-refused error page and never retries,
# which is what left the display empty until a reboot when the kiosk came up
# ahead of the app on first boot (FoodAssistant-kyl2). Always exits 0 so a
# missing curl or an app that stays down never blocks the kiosk itself.
ExecStartPre=-/bin/sh -c 'command -v curl >/dev/null 2>&1 || exit 0; for i in \$\$(seq 1 40); do curl -sf -o /dev/null --max-time 2 "$KIOSK_URL" && exit 0; sleep 2; done; exit 0'
ExecStart=$cage_bin -- $chromium_bin --kiosk --noerrdialogs \\
  --disable-infobars --no-first-run --ozone-platform=wayland \\
  --touch-events=enabled --use-gl=egl \\
  --remote-debugging-port=9222 --disable-restore-session-state $KIOSK_URL
# Apply the saved display rotation once cage is up (cage ignores
# WLR_OUTPUT_TRANSFORM; the helper drives wlr-randr and retries while the
# output comes up). Best-effort, so a missing helper never blocks the kiosk.
ExecStartPost=-/usr/local/bin/foodassistant-apply-rotation
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable foodassistant-kiosk.service || warn "kiosk enable failed"
  systemctl start foodassistant-kiosk.service || warn "kiosk start failed (will retry on boot)"

  # A kiosk display needs the rotation helper so the web UI / accel helper can
  # change KMS orientation later. (Pi Hosted also installs it via the host
  # bridge; this covers Pi Remote, where the bridge is skipped.)
  install_rotation_helper

  # Optional: install the LSM6DSOX accelerometer rotation helper if the sensor
  # is detected. No-op when the sensor isn't present or i2c-tools is missing.
  install_accel_rotation
}

# Step: Stream Deck controller (auto-detected by default)
configure_streamdeck() {
  if ! flag_enabled "$ENABLE_STREAMDECK" has_streamdeck; then
    log "Stream Deck not enabled (ENABLE_STREAMDECK=$ENABLE_STREAMDECK, none detected); skipping"
    return 0
  fi
  log "Installing Stream Deck controller"

  local venv_dir="/opt/foodassistant/venv"
  local sd_dst="/opt/foodassistant/foodassistant_streamdeck"
  # The package may sit beside this script (boot payload) or in the cloned repo
  # under streamdeck/. Resolve whichever is present.
  local sd_src=""
  if [ -d "$ASSET_DIR/foodassistant_streamdeck" ]; then
    sd_src="$ASSET_DIR/foodassistant_streamdeck"
  elif [ -d "$REPO_DIR/streamdeck/foodassistant_streamdeck" ]; then
    sd_src="$REPO_DIR/streamdeck/foodassistant_streamdeck"
  fi
  # Not present anywhere yet: clone the public repo so the package exists.
  if [ -z "$sd_src" ]; then
    ensure_repo
    [ -d "$REPO_DIR/streamdeck/foodassistant_streamdeck" ] && sd_src="$REPO_DIR/streamdeck/foodassistant_streamdeck"
  fi

  # Ensure venv exists (reuse if already created, e.g. on re-run).
  log "Installing python3-venv and Stream Deck USB dependencies"
  apt_install python3-venv libhidapi-libusb0 libudev-dev || warn "streamdeck dependencies install failed"

  if [ -d "$venv_dir" ]; then
    log "venv at $venv_dir already exists; reusing"
  else
    log "Creating Python venv at $venv_dir"
    run python3 -m venv "$venv_dir"
  fi

  # Pin floor on streamdeck>=0.9.8: 0.9.5 does not recognise USB product id
  # 0x00ba on current XL / Module 32 hardware.
  log "Installing Python dependencies into venv"
  run "$venv_dir/bin/pip" install --quiet --upgrade pip
  run "$venv_dir/bin/pip" install --quiet \
    "streamdeck>=0.9.8" \
    "Pillow>=10.4.0" \
    "httpx>=0.27.0" \
    "websockets>=12.0"

  # Copy the streamdeck package from the resolved source.
  if [ -n "$sd_src" ] && [ -d "$sd_src" ]; then
    log "Copying foodassistant_streamdeck package from $sd_src to $sd_dst"
    if [ "$DRY_RUN" != "1" ]; then
      # Replace any prior copy outright. Copying into an existing target dir
      # nests the package one level too deep (sd_dst/foodassistant_streamdeck),
      # which leaves __main__.py unreachable and breaks `python -m`.
      rm -rf "$sd_dst"
      cp -a "$sd_src" "$sd_dst"
      # Manual installs landed with mode 700 and broke the service.
      chmod -R a+rX "$sd_dst"
    fi
  else
    warn "foodassistant_streamdeck source not found (looked in boot payload and $REPO_DIR/streamdeck); skipping package copy"
  fi

  # Run the controller as the interactive user (the account Imager created),
  # not a fixed name, and add them to plugdev so they can open the USB device.
  local sd_user
  sd_user="$(primary_user)"
  [ -n "$sd_user" ] || { warn "No interactive (uid 1000) user found; skipping Stream Deck service"; return 0; }

  # Give the interactive user ownership of the streamdeck directory and venv
  # so they can write local logs and state files.
  if [ "$DRY_RUN" != "1" ]; then
    chown -R "$sd_user" "$sd_dst" "$venv_dir" 2>/dev/null || true
  fi

  # Install udev rule so the service user can open the USB device.
  log "Installing Elgato Stream Deck udev rule"
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would write /etc/udev/rules.d/99-streamdeck.rules"
  else
    printf 'SUBSYSTEM=="usb", ATTR{idVendor}=="0fd9", GROUP="plugdev", MODE="0660"\n' \
      > /etc/udev/rules.d/99-streamdeck.rules
    udevadm control --reload-rules || warn "udevadm reload failed"
    # Apply the rule to a Stream Deck that was already plugged in at install
    # time; without this the existing device node keeps root-only perms and
    # the controller cannot open it until the next replug or reboot.
    udevadm trigger --attr-match=idVendor=0fd9 || warn "udevadm trigger failed"
  fi

  if getent group plugdev >/dev/null 2>&1; then
    run usermod -aG plugdev "$sd_user" || warn "Could not add $sd_user to plugdev"
  else
    warn "plugdev group not found; skipping usermod"
  fi

  # A satellite runs the full app locally on port 80, so its Stream Deck drives
  # the LOCAL app (which talks to the shared Grocy/Mealie it pulled). Point the
  # controller at localhost:80; the controller reads FOODASSISTANT_BASE_URL.
  local sd_base_env=""
  if is_remote_mode; then
    sd_base_env="Environment=FOODASSISTANT_BASE_URL=http://localhost:80"
  fi

  # Write the systemd service unit.
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would write /etc/systemd/system/foodassistant-streamdeck.service for user $sd_user${sd_base_env:+ (base http://localhost:80)}"
    return 0
  fi
  cat > /etc/systemd/system/foodassistant-streamdeck.service <<EOF
[Unit]
Description=FoodAssistant Stream Deck controller
After=foodassistant.target network-online.target
Wants=network-online.target

[Service]
ExecStart=/opt/foodassistant/venv/bin/python -m foodassistant_streamdeck
Environment=FOODASSISTANT_STREAMDECK_CONFIG=/opt/foodassistant/config.toml
WorkingDirectory=/opt/foodassistant
Restart=always
RestartSec=5
User=$sd_user
${sd_base_env}

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable foodassistant-streamdeck.service || warn "streamdeck service enable failed"
  systemctl start foodassistant-streamdeck.service || warn "streamdeck service start failed (will retry on boot)"
}

# Install the foodassistant-set-rotation helper to /usr/local/bin so the KMS
# rotation controls (web UI, host bridge, accelerometer helper) can call it.
# Idempotent: copies whenever a newer/any source is found. Resolves the source
# from the boot payload (ASSET_DIR) or the cloned repo.
install_rotation_helper() {
  local src=""
  for candidate in "$ASSET_DIR/foodassistant-set-rotation" \
                   "$REPO_DIR/scripts/image-build/foodassistant-set-rotation"; do
    [ -f "$candidate" ] && src="$candidate" && break
  done
  [ -z "$src" ] && { warn "foodassistant-set-rotation not found; KMS rotation control unavailable"; return 0; }
  # The apply helper does the actual wlr-randr call; set-rotation and the kiosk
  # service's ExecStartPost both invoke it. Install it alongside.
  local apply_src=""
  for candidate in "$ASSET_DIR/foodassistant-apply-rotation" \
                   "$REPO_DIR/scripts/image-build/foodassistant-apply-rotation"; do
    [ -f "$candidate" ] && apply_src="$candidate" && break
  done
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would install $src and apply-rotation to /usr/local/bin"
    return 0
  fi
  install -m 755 "$src" /usr/local/bin/foodassistant-set-rotation
  log "Installed /usr/local/bin/foodassistant-set-rotation"
  if [ -n "$apply_src" ]; then
    install -m 755 "$apply_src" /usr/local/bin/foodassistant-apply-rotation
    log "Installed /usr/local/bin/foodassistant-apply-rotation"
  else
    warn "foodassistant-apply-rotation not found; live rotation will not apply"
  fi
  # The compositor-aware display blank/wake helper (FoodAssistant-8khi). The
  # host bridge prefers it over vcgencmd so blanking does not drop a cage kiosk
  # to the console. Best-effort: a missing helper just means the bridge falls
  # back to vcgencmd/xset.
  local power_src=""
  for candidate in "$ASSET_DIR/foodassistant-display-power" \
                   "$REPO_DIR/scripts/image-build/foodassistant-display-power"; do
    [ -f "$candidate" ] && power_src="$candidate" && break
  done
  if [ -n "$power_src" ]; then
    install -m 755 "$power_src" /usr/local/bin/foodassistant-display-power
    log "Installed /usr/local/bin/foodassistant-display-power"
  else
    warn "foodassistant-display-power not found; display blanking will use vcgencmd"
  fi
}

# Install the foodassistant-update helper to /usr/local/bin so the host bridge's
# /update endpoint (the in-app "Check for updates" button) can pull new source,
# refresh the venv, and restart the service. Idempotent: copies whenever a
# source is found. Resolves it from the boot payload (ASSET_DIR) or cloned repo.
install_update_helper() {
  local src=""
  for candidate in "$ASSET_DIR/foodassistant-update" \
                   "$REPO_DIR/scripts/image-build/foodassistant-update"; do
    [ -f "$candidate" ] && src="$candidate" && break
  done
  [ -z "$src" ] && { warn "foodassistant-update not found; in-app updates unavailable"; return 0; }
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would install $src to /usr/local/bin/foodassistant-update"
    return 0
  fi
  install -m 755 "$src" /usr/local/bin/foodassistant-update
  log "Installed /usr/local/bin/foodassistant-update"
}

# Step: host bridge (Pi Hosted / server modes only, not Pi Remote)
# Installs a small localhost HTTP helper that lets the Docker container call
# host-level operations (Wi-Fi, hostname, KMS rotation, service restarts)
# without needing privileged access inside the container. The app container
# reaches it on 127.0.0.1:9299 because docker-compose.appliance.yml uses
# network_mode: host.
install_host_bridge() {

  local bridge_src="" svc_src=""
  for candidate in "$ASSET_DIR/foodassistant-host-bridge" \
                   "$REPO_DIR/scripts/image-build/foodassistant-host-bridge"; do
    [ -f "$candidate" ] && bridge_src="$candidate" && break
  done
  for candidate in "$ASSET_DIR/foodassistant-host-bridge.service" \
                   "$REPO_DIR/scripts/image-build/foodassistant-host-bridge.service"; do
    [ -f "$candidate" ] && svc_src="$candidate" && break
  done

  if [ -z "$bridge_src" ]; then
    warn "foodassistant-host-bridge not found; skipping host bridge install"
    return 0
  fi

  log "Installing host bridge from $bridge_src"
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would install host bridge and systemd unit"
    return 0
  fi

  install -m 755 "$bridge_src" /usr/local/bin/foodassistant-host-bridge
  # The bridge's /display/rotation endpoint shells out to this helper.
  install_rotation_helper
  # The bridge's /update endpoint shells out to the OTA update helper.
  install_update_helper
  if [ -n "$svc_src" ]; then
    install -m 644 "$svc_src" /etc/systemd/system/foodassistant-host-bridge.service
    systemctl daemon-reload
    systemctl enable --now foodassistant-host-bridge.service \
      || warn "host bridge service enable failed"
  else
    warn "foodassistant-host-bridge.service not found; bridge installed but not started"
  fi
}

# Step: Wi-Fi fallback AP mode
# Installs hostapd and dnsmasq, then registers a watchdog service that
# activates a fallback hotspot (SSID: FoodAssistant) when wlan0 is not
# associated within 30 seconds of boot. The AP is NOT started immediately;
# it only activates on failure, so a device that connects normally is
# unaffected.
configure_wifi_ap_fallback() {
  log "Configuring Wi-Fi fallback AP mode"
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would install hostapd dnsmasq and write watchdog service"
    return 0
  fi

  DEBIAN_FRONTEND=noninteractive apt-get install -y -q hostapd dnsmasq \
    || warn "hostapd/dnsmasq install failed; AP fallback unavailable"

  # hostapd configuration: WPA2 personal on wlan0, channel 6.
  mkdir -p /etc/hostapd
  cat > /etc/hostapd/hostapd.conf <<'EOF'
interface=wlan0
driver=nl80211
ssid=FoodAssistant
hw_mode=g
channel=6
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=foodassist
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
EOF

  # dnsmasq DHCP range on the AP subnet. All DNS queries redirect to the
  # gateway (captive portal NXD hint via dhcp-option=6).
  mkdir -p /etc/dnsmasq.d
  cat > /etc/dnsmasq.d/foodassistant-ap.conf <<'EOF'
interface=wlan0
dhcp-range=192.168.99.2,192.168.99.20,12h
dhcp-option=3,192.168.99.1
dhcp-option=6,192.168.99.1
address=/#/192.168.99.1
EOF

  # Watchdog script: after network-online.target, only fall back to the setup
  # hotspot when the device has NO network at all. A Pi on wired Ethernet (or
  # already on Wi-Fi) must never drop into Wi-Fi setup mode, so we check for a
  # default route and for any wired interface that is up with an IP before
  # starting the AP (FoodAssistant-8ah5).
  cat > /usr/local/sbin/foodassistant-ap-watchdog <<'EOF'
#!/bin/bash
sleep 30
# Already associated to a Wi-Fi network: nothing to do.
if iw dev wlan0 link 2>/dev/null | grep -q "Connected"; then exit 0; fi
# A default route via any interface means we have a gateway: stay off the AP.
if ip route show default 2>/dev/null | grep -q .; then exit 0; fi
# A wired interface that is up with an IPv4 address means LAN connectivity (the
# appliance is reachable on the network), so the setup hotspot is not needed.
for dev in /sys/class/net/*; do
  name=$(basename "$dev")
  # Skip loopback, Wi-Fi (handled above), and virtual interfaces (docker,
  # bridges, veth, tun/tap) so only a real wired link counts as connectivity.
  case "$name" in lo|wlan*|docker*|br-*|veth*|tun*|tap*|vir*) continue;; esac
  if [ "$(cat "$dev/carrier" 2>/dev/null)" = "1" ] \
     && ip -4 addr show dev "$name" 2>/dev/null | grep -q "inet "; then
    exit 0
  fi
done
# No connectivity anywhere: bring up the captive setup hotspot.
ip addr add 192.168.99.1/24 dev wlan0 2>/dev/null || true
systemctl start hostapd
systemctl start dnsmasq
touch /run/foodassistant-ap-active
EOF
  chmod +x /usr/local/sbin/foodassistant-ap-watchdog

  # Watchdog service: runs the script once after the network comes up.
  cat > /etc/systemd/system/foodassistant-ap-watchdog.service <<'EOF'
[Unit]
Description=FoodAssistant Wi-Fi fallback AP watchdog
After=network-online.target
Wants=network-online.target
RemainAfterExit=yes

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/foodassistant-ap-watchdog

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable foodassistant-ap-watchdog.service \
    || warn "ap-watchdog service enable failed"
  log "Wi-Fi fallback AP watchdog installed (activates only when wlan0 fails to connect)"
}

# Step: mark done
mark_done() {
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would touch $DONE_MARKER"
    return 0
  fi
  mkdir -p "$(dirname "$DONE_MARKER")"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$DONE_MARKER"
  # Disable the oneshot unit so we never run again.
  systemctl disable foodassistant-firstboot.service 2>/dev/null || true
}

seed_app_settings() {
  local settings_file="$INSTALL_DIR/data/settings.json"
  if grep -q '"grocy_api_key"' "$settings_file" 2>/dev/null; then
    log "seed_app_settings: settings.json already contains grocy_api_key — skipping"
    return 0
  fi
  local sd_val="false"
  if is_true "${ENABLE_STREAMDECK:-auto}"; then
    sd_val="true"
  elif [ "${ENABLE_STREAMDECK:-auto}" = "auto" ] && has_streamdeck; then
    sd_val="true"
  fi
  local mode="${DEPLOYMENT_MODE:-pi_hosted}"
  # Pre-seed the BACKEND service URLs for a local stack (server / pi_hosted).
  # The app container runs with host networking, and Grocy/Mealie are published
  # on host ports, so the app reaches them at localhost:PORT (a docker service
  # name like "grocy" or an mDNS host like foodassistant.local does not resolve
  # from the host-networked app). The browser-facing links are derived from
  # these and rewritten to the LAN address automatically, so a single localhost
  # base URL serves both. A satellite (pi_remote) pulls these from its server,
  # so they are left unset there.
  local extra=""
  if ! is_remote_mode; then
    extra=', "grocy_base_url": "http://localhost:9383"'
    if is_true "$ENABLE_MEALIE"; then
      extra="$extra, \"mealie_base_url\": \"http://localhost:9285\""
    fi
  fi
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would write $settings_file: deployment_mode=$mode has_streamdeck=$sd_val${extra}"
    return 0
  fi
  mkdir -p "$(dirname "$settings_file")"
  cat > "$settings_file" <<EOF
{"deployment_mode": "$mode", "has_streamdeck": $sd_val${extra}}
EOF
  chmod 600 "$settings_file"
  log "seed_app_settings: wrote $settings_file (deployment_mode=$mode, has_streamdeck=$sd_val)"
}

# Returns success when the containerised Pi Remote stack (docker-compose.remote.yml)
# is already running and publishing host port 80. In that case the venv service
# must not also bind port 80, so the caller skips it. See FoodAssistant-dhr: the
# compose path and the venv path are mutually exclusive, never run both.
remote_docker_stack_active() {
  command -v docker >/dev/null 2>&1 || return 1
  docker compose version >/dev/null 2>&1 || return 1
  # The compose service container name is fixed in docker-compose.remote.yml.
  local state
  state="$(docker inspect -f '{{.State.Running}}' foodassistant-service 2>/dev/null)" || return 1
  [ "$state" = "true" ]
}

# Step: deploy the FoodAssistant UI service in a Python venv for Pi Remote.
# No Docker needed: just uvicorn + the app, bound directly on port 80.
# The user can then browse to http://<hostname>.local/ to set the remote URL.
deploy_remote_service() {
  local venv_dir="/opt/foodassistant/venv"
  local svc_src=""
  if [ -d "$REPO_DIR/service" ]; then
    svc_src="$REPO_DIR/service"
  elif [ -d "$ASSET_DIR/service" ]; then
    svc_src="$ASSET_DIR/service"
  fi

  # Guard against the port-80 conflict (FoodAssistant-dhr): if the operator has
  # already brought up the containerised stack (docker-compose.remote.yml, which
  # publishes host port 80), do not also install a venv service on the same port.
  # The two paths are alternatives, not additions. Disable any prior venv unit so
  # a re-provision does not leave both fighting for the port, then bail out.
  if remote_docker_stack_active; then
    warn "Containerised Pi Remote stack is running on port 80; skipping the venv service to avoid a port conflict (see docker-compose.remote.yml)."
    if [ "$DRY_RUN" != "1" ]; then
      systemctl disable --now foodassistant-remote.service 2>/dev/null || true
    fi
    return 0
  fi

  log "Installing FoodAssistant remote UI service (Python venv, port 80)"
  apt_install python3-venv python3-pip || die "python3-venv install failed"

  if [ -d "$venv_dir" ]; then
    log "Reusing existing venv at $venv_dir"
  else
    if [ "$DRY_RUN" = "1" ]; then
      log "DRY_RUN would create venv at $venv_dir"
    else
      python3 -m venv "$venv_dir"
    fi
  fi

  if [ -n "$svc_src" ] && [ -f "$svc_src/requirements.txt" ]; then
    log "Installing service requirements into venv"
    if [ "$DRY_RUN" != "1" ]; then
      # Retry the install: a single transient network or mirror hiccup here
      # used to leave uvicorn uninstalled, and the unit then crash looped
      # forever on status=203/EXEC (no such executable). Try a few times with
      # backoff before giving up.
      local _try
      for _try in 1 2 3; do
        if "$venv_dir/bin/pip" install --quiet -r "$svc_src/requirements.txt"; then
          break
        fi
        warn "pip install attempt $_try failed; retrying"
        sleep $((_try * 5))
      done
      # Verify the entry point actually landed. If it did not, the service can
      # never start, so say so loudly rather than hand systemd a doomed unit.
      if [ ! -x "$venv_dir/bin/uvicorn" ]; then
        warn "uvicorn missing from venv after pip install; the remote UI service will not start. Re-run with network access: $venv_dir/bin/pip install -r $svc_src/requirements.txt"
      fi
    fi
  else
    warn "service/requirements.txt not found; pip install skipped"
  fi

  # Copy service source into place (separate from venv so updates don't
  # require reinstalling packages).
  local app_dir="/opt/foodassistant/service"
  if [ -n "$svc_src" ]; then
    log "Copying service app from $svc_src to $app_dir"
    if [ "$DRY_RUN" != "1" ]; then
      run mkdir -p "$app_dir"
      cp -a "$svc_src/." "$app_dir/"
    fi
  fi

  # Write the .env file that the service reads on startup. Always (re)write it:
  # it is derived state, and REMOTE_SERVER_URL is carried in from settings.json
  # by _load_mode_from_settings, so a re-provision keeps the user's URL while
  # still picking up corrections like DATA_DIR. A stale env file here was what
  # left data_dir at the Docker default (/app/data) and crashed the service.
  local env_file="/opt/foodassistant/remote.env"
  log "Writing $env_file"
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would write $env_file (DEPLOYMENT_MODE=pi_remote, REMOTE_SERVER_URL, TZ, DATA_DIR)"
  else
    cat > "$env_file" <<EOF
DEPLOYMENT_MODE=pi_remote
TZ=${TZ:-America/New_York}
AUTH_REQUIRED=false
FOODASSISTANT_FORCE_MODEL=Raspberry Pi
DATA_DIR=$INSTALL_DIR/data
EOF
    # Only pin REMOTE_SERVER_URL when we actually have one. An empty env var
    # still counts as "set" to pydantic and would shadow the value saved to
    # settings.json by the web wizard, bouncing the device back to setup on
    # every reboot. When blank here, settings.json is the single source.
    if [ -n "${REMOTE_SERVER_URL:-}" ]; then
      echo "REMOTE_SERVER_URL=${REMOTE_SERVER_URL}" >> "$env_file"
    fi
    chmod 600 "$env_file"
  fi

  seed_app_settings

  # Systemd service: run uvicorn directly on port 80 (CAP_NET_BIND_SERVICE
  # lets an unprivileged binary bind ports below 1024 when set on the binary,
  # but systemd AmbientCapabilities is the most portable approach here).
  local sd_user
  sd_user="$(primary_user)"
  local exec_uvicorn="$venv_dir/bin/uvicorn"

  # The service runs as the primary (non-root) user, so it must be able to read
  # the app and read/write the data dir (settings.json is saved from the wizard).
  # firstboot created these as root, so hand them to the service user.
  if [ "$DRY_RUN" != "1" ] && [ -n "$sd_user" ]; then
    run mkdir -p "$INSTALL_DIR/data"
    run chown -R "$sd_user":"$sd_user" "$INSTALL_DIR" 2>/dev/null \
      || chown -R "$sd_user" "$INSTALL_DIR" 2>/dev/null \
      || warn "could not chown $INSTALL_DIR to $sd_user; service may not be able to save settings"
    # Add the service user to the input group so it can read /dev/input/event*
    # directly. This is needed for the web-UI touch calibration: the app streams
    # raw evtest events (the SSE endpoint) to draw the tap targets. Applying the
    # resulting matrix is done by the host bridge (root), not the app, so no
    # sudoers entry is needed here.
    usermod -aG input "$sd_user" 2>/dev/null \
      || warn "could not add $sd_user to the input group; touch calibration stream may be unavailable"
  fi

  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would write /etc/systemd/system/foodassistant-remote.service"
  else
    cat > /etc/systemd/system/foodassistant-remote.service <<EOF
[Unit]
Description=FoodAssistant Pi Remote UI
Documentation=https://github.com/Syracuse3DPrintingOrg/PantryRaider
After=network.target
Wants=network.target

[Service]
Type=simple
User=${sd_user:-foodassistant}
WorkingDirectory=$app_dir
EnvironmentFile=$env_file
ExecStart=$exec_uvicorn app.main:app --host 0.0.0.0 --port 80
Restart=on-failure
RestartSec=5
# Allow binding port 80 without running as root.
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable foodassistant-remote.service || warn "remote service enable failed"
    systemctl start  foodassistant-remote.service || warn "remote service start failed (will retry on boot)"
  fi
  log "FoodAssistant remote UI service installed; reachable at http://${HOSTNAME}.local/"
}

# Step: port-80 redirect for Pi Hosted.
# Routes incoming TCP port 80 to port 9284 so users can browse to
# http://<hostname>.local/ without specifying a port. Uses iptables NAT
# (no nginx needed). Persistence across reboots is handled two ways so it is
# robust even if iptables-persistent is unavailable: (1) a tiny systemd unit
# that re-applies the rule on every boot (the source of truth), and (2) a
# best-effort iptables-persistent save when that package is present.
#
# Pi-only: this is invoked solely on the pi_hosted path so we never hijack
# port 80 on a generic Linux server that may run its own web server.
configure_port80() {
  # The systemd unit re-applies an idempotent rule on each boot. We keep the
  # add commands in one place so the live run and the boot-time run match.
  local oneshot=/etc/systemd/system/foodassistant-port80.service

  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would add iptables PREROUTING 80->9284 (idempotent -C/-A)"
    log "DRY_RUN would install $oneshot to re-apply the redirect on every boot"
    log "DRY_RUN would persist via iptables-persistent/netfilter-persistent if available"
    log "DRY_RUN would enable foodassistant-port80.service (port 80 redirect persistence)"
    return 0
  fi

  # Check if iptables-persistent is available; install quietly if not. This is
  # best-effort: the systemd unit below is what actually guarantees the rule
  # survives a reboot, so we do not fail if the package is missing.
  if ! dpkg -l iptables-persistent &>/dev/null; then
    # Pre-answer the "save current rules?" prompt to avoid interactive install.
    echo "iptables-persistent iptables-persistent/autosave_v4 boolean true" \
      | debconf-set-selections 2>/dev/null || true
    echo "iptables-persistent iptables-persistent/autosave_v6 boolean false" \
      | debconf-set-selections 2>/dev/null || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      iptables-persistent 2>/dev/null || true
  fi

  # Idempotent: only add the rule if it is not already there.
  if ! iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 9284 2>/dev/null; then
    iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 9284
    log "Added iptables PREROUTING rule: port 80 -> 9284"
  fi
  # Same for the device browsing to its own port 80, but scoped to the loopback
  # interface. Locally generated packets to our own address (127.0.0.1 or the
  # host's own LAN IP) route through lo, while traffic to other hosts exits a
  # physical NIC. Without "-o lo" this rule hijacked ALL outbound port-80
  # traffic, including apt to the Debian mirrors, which then hit the local app
  # and got a 401. Remove any old unscoped rule before adding the scoped one.
  iptables -t nat -D OUTPUT -p tcp --dport 80 -j REDIRECT --to-port 9284 2>/dev/null || true
  if ! iptables -t nat -C OUTPUT -o lo -p tcp --dport 80 -j REDIRECT --to-port 9284 2>/dev/null; then
    iptables -t nat -A OUTPUT -o lo -p tcp --dport 80 -j REDIRECT --to-port 9284
  fi

  # Install a oneshot systemd unit that re-applies the rule on boot. iptables
  # NAT rules live in the kernel and are wiped on reboot; relying on
  # iptables-persistent alone is fragile (the package or its rules.v4 file may
  # be missing). This unit is the durable source of truth: the same idempotent
  # -C/-A guard means it is a no-op if the rule already exists.
  cat > "$oneshot" <<'EOF'
[Unit]
Description=FoodAssistant port 80 -> 9284 redirect (pi_hosted)
After=network-pre.target
Wants=network-pre.target
Before=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
# Idempotent: only add each rule if it is not already present.
ExecStart=/bin/sh -c 'iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 9284 2>/dev/null || iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 9284'
ExecStart=/bin/sh -c 'iptables -t nat -D OUTPUT -p tcp --dport 80 -j REDIRECT --to-port 9284 2>/dev/null; iptables -t nat -C OUTPUT -o lo -p tcp --dport 80 -j REDIRECT --to-port 9284 2>/dev/null || iptables -t nat -A OUTPUT -o lo -p tcp --dport 80 -j REDIRECT --to-port 9284'

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload 2>/dev/null || true
  systemctl enable foodassistant-port80.service 2>/dev/null \
    || warn "could not enable foodassistant-port80.service; port 80 may not redirect after reboot"

  # Best-effort secondary persistence via iptables-persistent when present.
  if command -v netfilter-persistent >/dev/null 2>&1; then
    netfilter-persistent save 2>/dev/null || warn "netfilter-persistent save failed"
  elif [ -d /etc/iptables ]; then
    iptables-save > /etc/iptables/rules.v4 2>/dev/null || warn "iptables-save failed"
  fi
  log "Port 80 -> 9284 redirect configured (pi_hosted); persistence via foodassistant-port80.service"
}

# Returns 0 when step $1 should run: always when STEPS is empty (run all),
# or when $1 appears in the comma-separated STEPS list.
_step_requested() {
  [ -z "$STEPS" ] && return 0
  local s
  local IFS=','
  for s in $STEPS; do
    [ "$s" = "$1" ] && return 0
  done
  return 1
}

# Main
main() {
  # Tee all output to the log (skip under DRY_RUN to keep test output clean and
  # avoid needing write access to /var/log).
  if [ "$DRY_RUN" != "1" ]; then
    mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
    exec > >(tee -a "$LOG_FILE") 2>&1
  fi

  log "FoodAssistant first-boot starting (DRY_RUN=$DRY_RUN, arch=$(detect_arch))"

  # Done-marker check: bypassed when FORCE=1 or when only specific steps are
  # targeted (STEPS= means "run these steps regardless of done state").
  if [ -f "$DONE_MARKER" ] && [ "${FORCE:-0}" != "1" ] && [ -z "$STEPS" ]; then
    log "Already provisioned ($DONE_MARKER exists); nothing to do. Set FORCE=1 to re-run."
    return 0
  fi
  if [ -n "$STEPS" ]; then
    log "Targeted step run (STEPS=$STEPS); done-marker check and write skipped"
  fi

  if ! is_debian_like; then
    warn "This provisioner targets Debian-like systems (Pi OS/Debian/Ubuntu)."
    warn "Detected non-Debian OS; Docker install may not work. Continuing best-effort."
  fi

  load_config

  # Refresh apt metadata once up front; skip for targeted step runs (each step
  # guards its own installs) and in DRY_RUN.
  if [ -z "$STEPS" ] && [ "$DRY_RUN" != "1" ] && is_debian_like; then
    DEBIAN_FRONTEND=noninteractive apt-get update -y || warn "apt-get update failed"
  fi

  _step_requested "hostname"    && configure_hostname
  _step_requested "timezone"    && configure_timezone
  _step_requested "mdns"        && configure_mdns

  if is_remote_mode; then
    # Satellite: no local Docker/Grocy/Mealie stack. The full FoodAssistant app
    # runs in a Python venv on port 80 and pulls its backend config (Grocy,
    # Mealie, AI keys, expiry defaults) from a main server. The user browses to
    # http://<hostname>.local/ to enter that server's URL + API key. No SSH.
    log "Satellite mode: skipping Docker stack; will pull config from ${REMOTE_SERVER_URL:-<set in web UI>}"
    _step_requested "remote_service"   && deploy_remote_service
    _step_requested "hostbridge"       && install_host_bridge
    _step_requested "wifi_ap_fallback" && configure_wifi_ap_fallback
    _step_requested "rotation"         && configure_display_rotation
    _step_requested "touch"          && configure_touch
    _step_requested "kiosk"          && configure_kiosk
    _step_requested "streamdeck"     && configure_streamdeck
    [ -z "$STEPS" ] && mark_done
    local _svc_url="http://${HOSTNAME}.local/"
    log "FoodAssistant satellite first-boot complete."
    log "  Open ${_svc_url} in a browser on your LAN to set the main server URL + API key."
    return 0
  fi

  _step_requested "docker"           && install_docker
  _step_requested "stack"            && deploy_stack
  _step_requested "port80"           && configure_port80
  _step_requested "hostbridge"       && install_host_bridge
  _step_requested "wifi_ap_fallback" && configure_wifi_ap_fallback
  _step_requested "rotation"         && configure_display_rotation
  _step_requested "touch"       && configure_touch
  _step_requested "kiosk"       && configure_kiosk
  _step_requested "streamdeck"  && configure_streamdeck
  [ -z "$STEPS" ] && mark_done

  log "FoodAssistant first-boot complete. Reach the UI at:"
  log "  http://${HOSTNAME}.local/   (or http://<device-ip>/)"
  log "First-time setup wizard: http://${HOSTNAME}.local/setup"
}

main "$@"
