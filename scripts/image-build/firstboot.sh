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
REPO_URL="${REPO_URL:-https://github.com/Syracuse3DPrinting/FoodAssistant.git}"

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
  ENABLE_MEALIE="${ENABLE_MEALIE:-false}"
  ENABLE_OLLAMA="${ENABLE_OLLAMA:-false}"
  ENABLE_KIOSK="${ENABLE_KIOSK:-auto}"
  ENABLE_STREAMDECK="${ENABLE_STREAMDECK:-auto}"
  DISPLAY_ROTATION="${DISPLAY_ROTATION:-0}"
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

  # The kiosk URL defaults to this device, except in remote mode where it points
  # at the server being controlled. ?kiosk=1 latches kiosk mode in the browser
  # so the attached-display scale/orientation apply (and never affect others).
  if is_remote_mode; then
    KIOSK_URL="${KIOSK_URL:-${REMOTE_SERVER_URL%/}/ui/?kiosk=1}"
  else
    KIOSK_URL="${KIOSK_URL:-http://localhost:9284/ui/?kiosk=1}"
  fi

  log "Config: HOSTNAME=$HOSTNAME TZ=$TZ MODE=${DEPLOYMENT_MODE:-<default>} MEALIE=$ENABLE_MEALIE OLLAMA=$ENABLE_OLLAMA KIOSK=$ENABLE_KIOSK STREAMDECK=$ENABLE_STREAMDECK TAG=$FOODASSISTANT_TAG DIR=$INSTALL_DIR"
}

# True when this device is a thin remote control surface (no local stack).
is_remote_mode() { [ "${DEPLOYMENT_MODE:-}" = "pi_remote" ]; }

# Pull deployment_mode / remote_server_url from a settings.json the wizard may
# have written, so a choice made in the UI survives a re-provision. Best effort:
# a tiny grep-based read keeps us free of a python/jq dependency here.
_load_mode_from_settings() {
  local sf="${SETTINGS_JSON:-$INSTALL_DIR/data/settings.json}"
  [ -r "$sf" ] || return 0
  local mode url
  mode="$(grep -o '"deployment_mode"[[:space:]]*:[[:space:]]*"[^"]*"' "$sf" 2>/dev/null | sed 's/.*"\([^"]*\)"$/\1/' || true)"
  url="$(grep -o '"remote_server_url"[[:space:]]*:[[:space:]]*"[^"]*"' "$sf" 2>/dev/null | sed 's/.*"\([^"]*\)"$/\1/' || true)"
  [ -n "$mode" ] && DEPLOYMENT_MODE="$mode"
  [ -n "$url" ] && REMOTE_SERVER_URL="$url"
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
# Makes the box reachable at <hostname>.local on the LAN.
configure_mdns() {
  if dpkg -s avahi-daemon >/dev/null 2>&1; then
    log "avahi-daemon already installed"
  else
    log "Installing avahi-daemon for mDNS"
    apt_install avahi-daemon
  fi
  run systemctl enable --now avahi-daemon || warn "avahi-daemon enable failed"
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

# Step: deploy the stack
write_env_file() {
  local env_path="$1"
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would write $env_path (TZ, FOODASSISTANT_TAG)"
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
# Writes video=HDMI-A-1:rotate=N to cmdline.txt so the DRM/KMS framebuffer
# (boot console, kiosk, everything) is rotated at the hardware level. Requires
# a reboot to take effect; the provisioner's normal first-boot reboot handles
# this automatically. Only runs when DISPLAY_ROTATION != 0.
configure_display_rotation() {
  local rot="${DISPLAY_ROTATION:-0}"
  case "$rot" in
    0|"") log "Display rotation is 0 (default); nothing to do"; return 0 ;;
    90|180|270) ;;
    *) warn "DISPLAY_ROTATION=$rot is not valid (use 0, 90, 180, or 270); skipping"; return 0 ;;
  esac

  local cmdline=""
  for path in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    [ -f "$path" ] && cmdline="$path" && break
  done

  if [ -z "$cmdline" ]; then
    warn "cmdline.txt not found; skipping KMS rotation (non-Pi or boot partition not mounted)"
    return 0
  fi

  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN: would add video=HDMI-A-1:rotate=${rot} to $cmdline"
    return 0
  fi

  local line
  line="$(tr -d '\n' < "$cmdline")"
  # Remove any existing video=HDMI-A-1:rotate=... parameter first (idempotent).
  line="$(printf '%s' "$line" | sed 's/ video=HDMI-A-1:rotate=[0-9]*//')"
  printf '%s video=HDMI-A-1:rotate=%s\n' "$line" "$rot" > "$cmdline"
  log "KMS rotation set to ${rot} degrees in $cmdline (takes effect after reboot)"
}

# Step: kiosk (opt-in, display-gated)
# Returns 0 if a display appears usable. We treat a present DRM/KMS card or an
# existing X/Wayland session as "has display".
has_display() {
  [ -n "${FORCE_DISPLAY:-}" ] && return 0   # test hook
  [ -e /dev/dri/card0 ] && return 0
  [ -n "${WAYLAND_DISPLAY:-}" ] && return 0
  [ -n "${DISPLAY:-}" ] && return 0
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

configure_kiosk() {
  if ! flag_enabled "$ENABLE_KIOSK" has_display; then
    log "Kiosk not enabled (ENABLE_KIOSK=$ENABLE_KIOSK); skipping"
    return 0
  fi
  if ! has_display; then
    warn "Kiosk enabled but no display detected; skipping kiosk"
    return 0
  fi
  log "Installing Chromium kiosk via cage (Wayland) for $KIOSK_URL"
  # cage = minimal single-app Wayland compositor; chromium = browser.
  apt_install cage chromium || apt_install cage chromium-browser \
    || warn "kiosk package install failed"

  local chromium_bin="chromium"
  command -v chromium-browser >/dev/null 2>&1 && chromium_bin="chromium-browser"

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
    log "DRY_RUN would run kiosk as $kuser (uid $kuid), disable getty@tty1, write foodassistant-kiosk.service"
    return 0
  fi

  loginctl enable-linger "$kuser" || warn "enable-linger failed"
  systemctl disable getty@tty1.service 2>/dev/null || true

  cat > /etc/systemd/system/foodassistant-kiosk.service <<EOF
[Unit]
Description=FoodAssistant Chromium kiosk
After=foodassistant.target systemd-user-sessions.service getty@tty1.service network-online.target
Wants=network-online.target
Conflicts=getty@tty1.service

[Service]
Type=simple
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
ExecStart=/usr/bin/cage -- $chromium_bin --kiosk --noerrdialogs \\
  --disable-infobars --no-first-run --ozone-platform=wayland \\
  --remote-debugging-port=9222 --disable-restore-session-state $KIOSK_URL
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
  if [ -d "$venv_dir" ]; then
    log "venv at $venv_dir already exists; reusing"
  else
    log "Installing python3-venv and Stream Deck USB dependencies"
    apt_install python3-venv libhidapi-hidraw0 libudev-dev || warn "streamdeck dependencies install failed"
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
    run mkdir -p "$sd_dst"
    if [ "$DRY_RUN" != "1" ]; then
      cp -a "$sd_src"/. "$sd_dst"/
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
  fi

  if getent group plugdev >/dev/null 2>&1; then
    run usermod -aG plugdev "$sd_user" || warn "Could not add $sd_user to plugdev"
  else
    warn "plugdev group not found; skipping usermod"
  fi

  # In remote mode the controller talks to the remote server, not localhost;
  # the controller reads FOODASSISTANT_BASE_URL from its environment.
  local sd_base_env=""
  if is_remote_mode && [ -n "$REMOTE_SERVER_URL" ]; then
    sd_base_env="Environment=FOODASSISTANT_BASE_URL=${REMOTE_SERVER_URL%/}"
  fi

  # Write the systemd service unit.
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would write /etc/systemd/system/foodassistant-streamdeck.service for user $sd_user${sd_base_env:+ (base ${REMOTE_SERVER_URL%/})}"
    return 0
  fi
  cat > /etc/systemd/system/foodassistant-streamdeck.service <<EOF
[Unit]
Description=FoodAssistant Stream Deck controller
After=foodassistant.target network-online.target
Wants=network-online.target

[Service]
ExecStart=/opt/foodassistant/venv/bin/python -m foodassistant_streamdeck
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
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would install $src to /usr/local/bin/foodassistant-set-rotation"
    return 0
  fi
  install -m 755 "$src" /usr/local/bin/foodassistant-set-rotation
  log "Installed /usr/local/bin/foodassistant-set-rotation"
}

# Step: host bridge (Pi Hosted / server modes only, not Pi Remote)
# Installs a small localhost HTTP helper that lets the Docker container call
# host-level operations (Wi-Fi, hostname, KMS rotation, service restarts)
# without needing privileged access inside the container. The app container
# reaches it on 127.0.0.1:9299 because docker-compose.appliance.yml uses
# network_mode: host.
install_host_bridge() {
  is_remote_mode && return 0   # thin remote has no local app container

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
  if [ -n "$svc_src" ]; then
    install -m 644 "$svc_src" /etc/systemd/system/foodassistant-host-bridge.service
    systemctl daemon-reload
    systemctl enable --now foodassistant-host-bridge.service \
      || warn "host bridge service enable failed"
  else
    warn "foodassistant-host-bridge.service not found; bridge installed but not started"
  fi
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
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would write $settings_file: deployment_mode=$mode has_streamdeck=$sd_val"
    return 0
  fi
  mkdir -p "$(dirname "$settings_file")"
  cat > "$settings_file" <<EOF
{"deployment_mode": "$mode", "has_streamdeck": $sd_val}
EOF
  chmod 600 "$settings_file"
  log "seed_app_settings: wrote $settings_file (deployment_mode=$mode, has_streamdeck=$sd_val)"
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
    # Thin client: no Docker, no Grocy/Mealie, no local FoodAssistant service.
    # Just a kiosk and/or Stream Deck pointed at the remote server. This is what
    # keeps a Pi Remote viable on low-spec hardware (Pi 3).
    if [ -z "$REMOTE_SERVER_URL" ]; then
      warn "Pi Remote mode selected but REMOTE_SERVER_URL is empty; the kiosk/Stream Deck will have no server to talk to. Set REMOTE_SERVER_URL in config.env."
    fi
    log "Pi Remote mode: skipping Docker and the local stack; controlling ${REMOTE_SERVER_URL:-<unset>}"
    _step_requested "rotation"    && configure_display_rotation
    _step_requested "kiosk"       && configure_kiosk
    _step_requested "streamdeck"  && configure_streamdeck
    [ -z "$STEPS" ] && mark_done
    log "FoodAssistant Pi Remote first-boot complete."
    log "  This device controls: ${REMOTE_SERVER_URL:-<set REMOTE_SERVER_URL>}"
    return 0
  fi

  _step_requested "docker"      && install_docker
  _step_requested "stack"       && deploy_stack
  _step_requested "hostbridge"  && install_host_bridge
  _step_requested "rotation"    && configure_display_rotation
  _step_requested "kiosk"       && configure_kiosk
  _step_requested "streamdeck"  && configure_streamdeck
  [ -z "$STEPS" ] && mark_done

  log "FoodAssistant first-boot complete. Reach the UI at:"
  log "  http://${HOSTNAME}.local:9284/   (or http://<device-ip>:9284/)"
  log "First-time setup wizard: http://${HOSTNAME}.local:9284/setup"
}

main "$@"
