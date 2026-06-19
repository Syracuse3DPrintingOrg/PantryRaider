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

# ── Tunables (env-overridable; mostly for tests) ───────────────────────────
DRY_RUN="${DRY_RUN:-0}"
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

# ── Logging helpers ────────────────────────────────────────────────────────
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

# ── Config loading ─────────────────────────────────────────────────────────
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

  # Defaults — mirror image/config.env.
  HOSTNAME="${HOSTNAME:-foodassistant}"
  TZ="${TZ:-America/New_York}"
  ENABLE_MEALIE="${ENABLE_MEALIE:-false}"
  ENABLE_OLLAMA="${ENABLE_OLLAMA:-false}"
  ENABLE_KIOSK="${ENABLE_KIOSK:-false}"
  # ?kiosk=1 latches kiosk mode in the browser so the attached-display scale
  # and orientation settings apply (and never affect other browsers).
  KIOSK_URL="${KIOSK_URL:-http://localhost:9284/ui/?kiosk=1}"
  ENABLE_STREAMDECK="${ENABLE_STREAMDECK:-false}"
  FOODASSISTANT_TAG="${FOODASSISTANT_TAG:-latest}"
  INSTALL_DIR="${INSTALL_DIR:-/opt/foodassistant}"

  log "Config: HOSTNAME=$HOSTNAME TZ=$TZ MEALIE=$ENABLE_MEALIE OLLAMA=$ENABLE_OLLAMA KIOSK=$ENABLE_KIOSK STREAMDECK=$ENABLE_STREAMDECK TAG=$FOODASSISTANT_TAG DIR=$INSTALL_DIR"
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

# ── Step: hostname ─────────────────────────────────────────────────────────
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

# ── Step: timezone ─────────────────────────────────────────────────────────
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

# ── Step: mDNS (avahi) ─────────────────────────────────────────────────────
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

# ── Step: Docker + Compose v2 ──────────────────────────────────────────────
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

# ── Step: deploy the stack ─────────────────────────────────────────────────
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
  [ -f "$COMPOSE_SRC" ] || die "Appliance compose file not found at $COMPOSE_SRC"
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
    if [ ! -d "$REPO_DIR/service" ]; then
      command -v git >/dev/null 2>&1 || apt_install git || warn "git install failed"
      command -v git >/dev/null 2>&1 \
        || die "git unavailable and image pull failed. Make the GHCR package public or pre-clone the repo to $REPO_DIR."
      log "Source not present; cloning $REPO_URL to $REPO_DIR"
      run git clone --depth 1 "$REPO_URL" "$REPO_DIR" \
        || die "Could not clone $REPO_URL. Check internet, or make the GHCR package public."
    fi
    # shellcheck disable=SC2086
    ( cd "$INSTALL_DIR" && docker compose $profiles build service ) \
      || die "Local build also failed. Check $REPO_DIR/service and Docker logs."
  fi
  # shellcheck disable=SC2086
  ( cd "$INSTALL_DIR" && docker compose $profiles up -d )
}

# ── Step: kiosk (opt-in, display-gated) ────────────────────────────────────
# Returns 0 if a display appears usable. We treat a present DRM/KMS card or an
# existing X/Wayland session as "has display".
has_display() {
  [ -n "${FORCE_DISPLAY:-}" ] && return 0   # test hook
  [ -e /dev/dri/card0 ] && return 0
  [ -n "${WAYLAND_DISPLAY:-}" ] && return 0
  [ -n "${DISPLAY:-}" ] && return 0
  return 1
}

configure_kiosk() {
  if ! is_true "$ENABLE_KIOSK"; then
    log "Kiosk disabled (ENABLE_KIOSK=$ENABLE_KIOSK); skipping"
    return 0
  fi
  if ! has_display; then
    warn "ENABLE_KIOSK=true but no display detected; skipping kiosk"
    return 0
  fi
  log "Installing Chromium kiosk via cage (Wayland) for $KIOSK_URL"
  # cage = minimal single-app Wayland compositor; chromium = browser.
  apt_install cage chromium || apt_install cage chromium-browser \
    || warn "kiosk package install failed"

  local chromium_bin="chromium"
  command -v chromium-browser >/dev/null 2>&1 && chromium_bin="chromium-browser"

  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would write /etc/systemd/system/foodassistant-kiosk.service using $chromium_bin"
    return 0
  fi
  cat > /etc/systemd/system/foodassistant-kiosk.service <<EOF
[Unit]
Description=FoodAssistant Chromium kiosk
After=foodassistant.target network-online.target
Wants=network-online.target

[Service]
# cage launches a single fullscreen Wayland app on the first DRM device.
ExecStart=/usr/bin/cage -- $chromium_bin --kiosk --noerrdialogs \\
  --disable-infobars --no-first-run --ozone-platform=wayland $KIOSK_URL
Restart=always
RestartSec=5
TTYPath=/dev/tty1

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable foodassistant-kiosk.service || warn "kiosk enable failed"
  systemctl start foodassistant-kiosk.service || warn "kiosk start failed (will retry on boot)"
}

# ── Step: Stream Deck controller (opt-in) ──────────────────────────────────
configure_streamdeck() {
  if ! is_true "$ENABLE_STREAMDECK"; then
    log "Stream Deck disabled (ENABLE_STREAMDECK=$ENABLE_STREAMDECK); skipping"
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
  if [ -z "$sd_src" ] && [ ! -d "$REPO_DIR/streamdeck/foodassistant_streamdeck" ]; then
    command -v git >/dev/null 2>&1 || apt_install git || warn "git install failed"
    if command -v git >/dev/null 2>&1; then
      log "Stream Deck package not present; cloning $REPO_URL to $REPO_DIR"
      run git clone --depth 1 "$REPO_URL" "$REPO_DIR" || warn "clone for streamdeck failed"
    fi
    [ -d "$REPO_DIR/streamdeck/foodassistant_streamdeck" ] && sd_src="$REPO_DIR/streamdeck/foodassistant_streamdeck"
  fi

  # Ensure venv exists (reuse if already created, e.g. on re-run).
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
    run mkdir -p "$sd_dst"
    if [ "$DRY_RUN" != "1" ]; then
      cp -a "$sd_src"/. "$sd_dst"/
      # Manual installs landed with mode 700 and broke the service.
      chmod -R a+rX "$sd_dst"
    fi
  else
    warn "foodassistant_streamdeck source not found (looked in boot payload and $REPO_DIR/streamdeck); skipping package copy"
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

  # Add the service user (foodassistant) to the plugdev group.
  if getent group plugdev >/dev/null 2>&1; then
    run usermod -aG plugdev foodassistant || warn "Could not add foodassistant to plugdev"
  else
    warn "plugdev group not found; skipping usermod"
  fi

  # Write the systemd service unit.
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN would write /etc/systemd/system/foodassistant-streamdeck.service"
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
User=foodassistant

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable foodassistant-streamdeck.service || warn "streamdeck service enable failed"
  systemctl start foodassistant-streamdeck.service || warn "streamdeck service start failed (will retry on boot)"
}

# ── Step: mark done ────────────────────────────────────────────────────────
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

# ── Main ───────────────────────────────────────────────────────────────────
main() {
  # Tee all output to the log (skip under DRY_RUN to keep test output clean and
  # avoid needing write access to /var/log).
  if [ "$DRY_RUN" != "1" ]; then
    mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
    exec > >(tee -a "$LOG_FILE") 2>&1
  fi

  log "FoodAssistant first-boot starting (DRY_RUN=$DRY_RUN, arch=$(detect_arch))"

  if [ -f "$DONE_MARKER" ] && [ "${FORCE:-0}" != "1" ]; then
    log "Already provisioned ($DONE_MARKER exists); nothing to do. Set FORCE=1 to re-run."
    return 0
  fi

  if ! is_debian_like; then
    warn "This provisioner targets Debian-like systems (Pi OS/Debian/Ubuntu)."
    warn "Detected non-Debian OS; Docker install may not work. Continuing best-effort."
  fi

  load_config

  # Refresh apt metadata once up front (skipped in DRY_RUN by run()).
  if [ "$DRY_RUN" != "1" ] && is_debian_like; then
    DEBIAN_FRONTEND=noninteractive apt-get update -y || warn "apt-get update failed"
  fi

  configure_hostname
  configure_timezone
  configure_mdns
  install_docker
  deploy_stack
  configure_kiosk
  configure_streamdeck
  mark_done

  log "FoodAssistant first-boot complete. Reach the UI at:"
  log "  http://${HOSTNAME}.local:9284/   (or http://<device-ip>:9284/)"
  log "First-time setup wizard: http://${HOSTNAME}.local:9284/setup"
}

main "$@"
