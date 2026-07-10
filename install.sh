#!/usr/bin/env bash
# Pantry Raider on-device installer (loader)
# ==========================================
# Run this ON the device (a freshly imaged Raspberry Pi, or any Debian/Ubuntu
# box) over SSH. It asks what you want to install, then provisions only that.
#
#   curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/install.sh | bash
#
# There is nothing to edit on your PC and no repo to clone on your PC. Flash the
# card with Raspberry Pi Imager (set wifi/hostname/locale there), boot, SSH in,
# and run the line above.
#
# What it does:
#   1. Detects whether this is a Raspberry Pi, and whether a display and/or a
#      Stream Deck are attached right now.
#   2. Asks for the deployment mode and which add-ons to enable.
#   3. Fetches the repo to the device (so the compose file, service build
#      context, and Stream Deck package are available) and runs the provisioner
#      (scripts/image-build/firstboot.sh) with the choices you made.
#
# Modes:
#   pi_hosted  - full stack on this Pi (Pantry Raider + Grocy, optional Mealie).
#   pi_remote  - thin client: NO Docker/Grocy here, just a kiosk and/or Stream
#                Deck pointed at a Pantry Raider server elsewhere on the LAN.
#   server     - full stack on a general (non-Pi) Debian/Ubuntu host.
#
# Non-interactive use (CI, scripted installs): set NONINTERACTIVE=1 and pass the
# choices as env vars (DEPLOYMENT_MODE, REMOTE_SERVER_URL, ENABLE_MEALIE,
# ENABLE_OLLAMA, ENABLE_KIOSK, ENABLE_STREAMDECK, ENABLE_PRINTING,
# DISPLAY_ROTATION, HOSTNAME). Label printing is OFF by default (ENABLE_PRINTING
# is only turned on when you ask for it, so a normal install never installs CUPS
# or Bluetooth).
# PLAN_ONLY=1 prints the resolved plan and the firstboot command, then exits
# without cloning, using sudo, or provisioning (used by the test suite).
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Syracuse3DPrintingOrg/PantryRaider.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
# Where the repo is checked out ON THE DEVICE (never on the user's PC).
REPO_DIR="${REPO_DIR:-/opt/foodassistant-src}"

NONINTERACTIVE="${NONINTERACTIVE:-0}"
PLAN_ONLY="${PLAN_ONLY:-0}"

# -- pretty output ------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_CYAN=$'\033[1;36m'; C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'
  C_RED=$'\033[1;31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_CYAN=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_DIM=""; C_OFF=""
fi
say()  { printf '%s==>%s %s\n' "$C_CYAN" "$C_OFF" "$*"; }
ok()   { printf '%s[ok]%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }
die()  { printf '%sError:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }
hr()   { printf '%s----------------------------------------%s\n' "$C_DIM" "$C_OFF"; }

# -- interactive helpers (read from the terminal, not stdin) ------------------
# When invoked as `curl ... | bash`, stdin is the script itself, so prompts must
# read from /dev/tty. If there is no terminal and we are interactive, that is a
# fatal setup error with a clear fix.
TTY="/dev/tty"
have_tty() { [ -e "$TTY" ] && { : >/dev/null 2>&1 <"$TTY"; }; }

prompt_line() {  # prompt default -> echoes the answer (or default if blank)
  local prompt="$1" def="${2:-}" ans=""
  printf '%s%s%s ' "$C_CYAN" "$prompt" "$C_OFF" >"$TTY"
  IFS= read -r ans <"$TTY" || ans=""
  printf '%s' "${ans:-$def}"
}

prompt_yn() {  # prompt default(y|n) -> returns 0 for yes, 1 for no
  local prompt="$1" def="$2" hint ans
  case "$def" in y|Y) hint="[Y/n]";; *) hint="[y/N]";; esac
  while :; do
    printf '%s%s %s%s ' "$C_CYAN" "$prompt" "$hint" "$C_OFF" >"$TTY"
    IFS= read -r ans <"$TTY" || ans=""
    ans="${ans:-$def}"
    case "$ans" in
      y|Y|yes|YES) return 0 ;;
      n|N|no|NO)   return 1 ;;
      *) printf '  Please answer y or n.\n' >"$TTY" ;;
    esac
  done
}

prompt_choice() {  # title; then "key:label" pairs in $@ ; echoes chosen key
  local title="$1"; shift
  local -a keys=() labels=()
  local pair
  for pair in "$@"; do
    keys+=("${pair%%:*}"); labels+=("${pair#*:}")
  done
  local i
  printf '%s%s%s\n' "$C_CYAN" "$title" "$C_OFF" >"$TTY"
  for i in "${!keys[@]}"; do
    printf '  %s) %s\n' "$((i+1))" "${labels[$i]}" >"$TTY"
  done
  while :; do
    local sel
    printf '%sChoose 1-%s [1]:%s ' "$C_CYAN" "${#keys[@]}" "$C_OFF" >"$TTY"
    IFS= read -r sel <"$TTY" || sel=""
    sel="${sel:-1}"
    if [[ "$sel" =~ ^[0-9]+$ ]] && [ "$sel" -ge 1 ] && [ "$sel" -le "${#keys[@]}" ]; then
      printf '%s' "${keys[$((sel-1))]}"; return 0
    fi
    printf '  Enter a number between 1 and %s.\n' "${#keys[@]}" >"$TTY"
  done
}

# -- hardware detection (same signals firstboot.sh uses) ----------------------
is_raspberry_pi() {
  [ -n "${FORCE_PI:-}" ] && return 0
  local f
  for f in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
    [ -r "$f" ] && tr -d '\0' <"$f" | grep -qi 'raspberry pi' && return 0
  done
  return 1
}
has_display() {
  [ -n "${FORCE_DISPLAY:-}" ] && return 0
  [ -e /dev/dri/card0 ] && return 0
  [ -n "${WAYLAND_DISPLAY:-}" ] && return 0
  [ -n "${DISPLAY:-}" ] && return 0
  return 1
}
has_streamdeck() {
  [ -n "${FORCE_STREAMDECK:-}" ] && return 0
  if command -v lsusb >/dev/null 2>&1; then
    lsusb 2>/dev/null | grep -qi '0fd9:' && return 0
  fi
  grep -qil '0fd9' /sys/bus/usb/devices/*/idVendor 2>/dev/null && return 0
  return 1
}
board_model() {
  local f
  for f in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
    [ -r "$f" ] && { tr -d '\0' <"$f"; return; }
  done
  echo "unknown"
}

yesno() { case "$1" in true|TRUE|1|yes|on) echo true;; *) echo false;; esac; }

# -- gather configuration -----------------------------------------------------
IS_PI=false; is_raspberry_pi && IS_PI=true
HAS_DISPLAY=false; has_display && HAS_DISPLAY=true
HAS_DECK=false; has_streamdeck && HAS_DECK=true

DEPLOYMENT_MODE="${DEPLOYMENT_MODE:-}"
REMOTE_SERVER_URL="${REMOTE_SERVER_URL:-}"
# Mealie is OFF by default everywhere: recipes, the meal plan, and the
# shopping list are built into Pantry Raider. It remains available as an
# opt-in for people who already use Mealie: set ENABLE_MEALIE=true in the
# environment (or enable the with-mealie compose profile later).
ENABLE_MEALIE="${ENABLE_MEALIE:-false}"
ENABLE_OLLAMA="${ENABLE_OLLAMA:-false}"
ENABLE_KIOSK="${ENABLE_KIOSK:-}"
ENABLE_STREAMDECK="${ENABLE_STREAMDECK:-}"
# Label printing (CUPS + Bluetooth + generic drivers) is off by default: it is a
# separate opt-in the user makes here or later from the setup UI. An explicit
# ENABLE_PRINTING in the environment (1/true to turn it on) always wins.
ENABLE_PRINTING="${ENABLE_PRINTING:-}"
DISPLAY_ROTATION="${DISPLAY_ROTATION:-0}"
HOSTNAME_CHOICE="${HOSTNAME:-$(hostname 2>/dev/null || echo foodassistant)}"

banner() {
  hr
  printf '%s  Pantry Raider installer%s\n' "$C_GREEN" "$C_OFF"
  hr
  if [ "$IS_PI" = true ]; then
    say "Device: $(board_model)"
  else
    say "Device: non-Pi host ($(uname -m))"
  fi
  say "Display attached:    $([ "$HAS_DISPLAY" = true ] && echo yes || echo no)"
  say "Stream Deck attached: $([ "$HAS_DECK" = true ] && echo yes || echo no)"
  hr
}

interactive_config() {
  have_tty || die "No terminal for prompts. Run over SSH, or set NONINTERACTIVE=1 with the choices as env vars (see the header of this script)."

  # Mode is the only question asked interactively. Everything else (kiosk,
  # Stream Deck, display rotation, Ollama) is auto-detected or set later via
  # the web setup wizard at /setup. Mealie is not installed by default:
  # recipes, the meal plan, and shopping are built in. Set ENABLE_MEALIE=true
  # in the environment if you already use Mealie and want it alongside.
  if [ "$IS_PI" = true ]; then
    DEPLOYMENT_MODE="$(prompt_choice "How will this device be used?" \
      "pi_hosted:Pi Hosted  - run the full Pantry Raider stack on this Pi" \
      "pi_remote:Pi Remote  - thin client (kiosk/Stream Deck) for a server elsewhere")"
  else
    say "Non-Pi host detected; using Server hosted mode."
    DEPLOYMENT_MODE="server"
  fi

  # In Pi Remote mode the server URL is NOT asked here. The device boots
  # with a small web setup app on port 80, so the end user (or you, before
  # shipping) sets the server URL from a browser at the device address. Set
  # REMOTE_SERVER_URL in the environment to pre-seed it for a hands-off image.
  if [ "$DEPLOYMENT_MODE" = "pi_remote" ] && [ -z "$REMOTE_SERVER_URL" ]; then
    say "Server URL will be set later in the web setup wizard (no SSH needed)."
  fi

  # Auto-detect kiosk and Stream Deck based on attached hardware.
  # Display orientation, Mealie, and Ollama are configured in the web UI.
  [ -z "$ENABLE_KIOSK" ]      && ENABLE_KIOSK="$([ "$HAS_DISPLAY" = true ] && echo true || echo false)"
  [ -z "$ENABLE_STREAMDECK" ] && ENABLE_STREAMDECK="$([ "$HAS_DECK" = true ] && echo true || echo false)"

  # Label printing: opt-in, off by default. Ask once here; it can also be turned
  # on later from Settings > Printing without reinstalling. Skip the question if
  # an explicit ENABLE_PRINTING was passed in the environment.
  if [ -z "$ENABLE_PRINTING" ]; then
    if prompt_yn "Enable label printing (installs CUPS + Bluetooth)?" n; then
      ENABLE_PRINTING=true
    else
      ENABLE_PRINTING=false
    fi
  fi
}

# Non-interactive: fill any unset enable flags from detection so a bare
# NONINTERACTIVE=1 still does something sensible.
noninteractive_config() {
  [ -z "$DEPLOYMENT_MODE" ] && { [ "$IS_PI" = true ] && DEPLOYMENT_MODE="pi_hosted" || DEPLOYMENT_MODE="server"; }
  if [ "$DEPLOYMENT_MODE" = "pi_remote" ]; then
    ENABLE_MEALIE=false; ENABLE_OLLAMA=false
  fi
  [ -z "$ENABLE_KIOSK" ]      && ENABLE_KIOSK="$([ "$HAS_DISPLAY" = true ] && echo true || echo false)"
  [ -z "$ENABLE_STREAMDECK" ] && ENABLE_STREAMDECK="$([ "$HAS_DECK" = true ] && echo true || echo false)"
  # Printing stays off unless the operator asked for it explicitly.
  if [ -z "$ENABLE_PRINTING" ]; then ENABLE_PRINTING=false; fi
}

confirm_plan() {
  hr
  say "Install plan"
  printf '  Mode:        %s\n' "$DEPLOYMENT_MODE"
  [ "$DEPLOYMENT_MODE" = "pi_remote" ] && printf '  Controls:    %s\n' "${REMOTE_SERVER_URL:-(set later in web wizard)}"
  printf '  Hostname:    %s\n' "$HOSTNAME_CHOICE"
  if [ "$DEPLOYMENT_MODE" != "pi_remote" ]; then
    printf '  Mealie:      %s\n' "$(yesno "$ENABLE_MEALIE")"
    printf '  Ollama:      %s\n' "$(yesno "$ENABLE_OLLAMA")"
  fi
  printf '  Kiosk:       %s%s\n' "$(yesno "$ENABLE_KIOSK")" \
    "$([ "$DISPLAY_ROTATION" != 0 ] && printf ' (rotated %s)' "$DISPLAY_ROTATION")"
  printf '  Stream Deck: %s\n' "$(yesno "$ENABLE_STREAMDECK")"
  printf '  Printing:    %s\n' "$(yesno "$ENABLE_PRINTING")"
  hr
}

# -- provisioning -------------------------------------------------------------
SUDO=""
need_root() {
  if [ "$(id -u)" -eq 0 ]; then SUDO=""; return; fi
  command -v sudo >/dev/null 2>&1 || die "This step needs root. Re-run as root or install sudo."
  SUDO="sudo"
}

fetch_repo() {
  say "Fetching Pantry Raider to $REPO_DIR (on this device)"
  if [ -d "$REPO_DIR/.git" ]; then
    if $SUDO git -C "$REPO_DIR" fetch --depth 1 origin "$REPO_BRANCH"; then
      $SUDO git -C "$REPO_DIR" reset --hard "origin/$REPO_BRANCH" \
        || warn "Fetched but could not fast-forward; using what is on disk."
    else
      warn "Could not update existing checkout; using what is on disk."
    fi
  else
    command -v git >/dev/null 2>&1 || { say "Installing git"; $SUDO apt-get update -y && $SUDO apt-get install -y git; }
    $SUDO git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$REPO_DIR" \
      || die "Could not clone $REPO_URL. Check internet access and try again."
  fi
  ok "Repo ready at $REPO_DIR"
}

run_provisioner() {
  local fb="$REPO_DIR/scripts/image-build/firstboot.sh"
  [ -f "$fb" ] || die "Provisioner not found at $fb"
  say "Provisioning (this can take a few minutes on first run)"
  $SUDO env \
    DEPLOYMENT_MODE="$DEPLOYMENT_MODE" \
    REMOTE_SERVER_URL="$REMOTE_SERVER_URL" \
    ENABLE_MEALIE="$(yesno "$ENABLE_MEALIE")" \
    ENABLE_OLLAMA="$(yesno "$ENABLE_OLLAMA")" \
    ENABLE_KIOSK="$(yesno "$ENABLE_KIOSK")" \
    ENABLE_STREAMDECK="$(yesno "$ENABLE_STREAMDECK")" \
    ENABLE_PRINTING="$(yesno "$ENABLE_PRINTING")" \
    DISPLAY_ROTATION="$DISPLAY_ROTATION" \
    HOSTNAME="$HOSTNAME_CHOICE" \
    REPO_DIR="$REPO_DIR" \
    bash "$fb"
}

# Optional print stack: only when the operator opted in. Runs the idempotent
# host print-setup helper (CUPS + Bluetooth + generic drivers), then recreates
# the app container so it picks up the CUPS_SERVER the helper wrote to the
# stack .env. A no-op for anyone who did not enable printing.
run_print_setup() {
  [ "$(yesno "$ENABLE_PRINTING")" = "true" ] || return 0
  local ps="$REPO_DIR/scripts/image-build/foodassistant-print-setup"
  if [ ! -f "$ps" ]; then
    warn "Print setup helper not found at $ps; skipping print stack install."
    return 0
  fi
  say "Installing the label print stack (CUPS + Bluetooth)"
  $SUDO env REPO_DIR="$REPO_DIR" bash "$ps" || warn "Print stack setup reported problems; printing may need attention in Settings > Printing."
  # Recreate the app container so env_file (CUPS_SERVER) takes effect. Only the
  # non-remote stacks run Docker; a pi_remote venv app reads local cupsd
  # directly and needs no recreate.
  if [ "$DEPLOYMENT_MODE" != "pi_remote" ] && command -v docker >/dev/null 2>&1; then
    for d in /opt/foodassistant "$REPO_DIR"; do
      if [ -f "$d/docker-compose.yml" ]; then
        say "Applying print settings to the running app"
        ( cd "$d" && $SUDO docker compose up -d ) || warn "Could not recreate the app container; run 'docker compose up -d' in $d."
        break
      fi
    done
  fi
}

print_done() {
  hr
  ok "Pantry Raider installed."
  if [ "$DEPLOYMENT_MODE" = "pi_remote" ] && [ -n "$REMOTE_SERVER_URL" ]; then
    say "This device controls: $REMOTE_SERVER_URL"
    say "To change it later, open the setup wizard in your browser:"
    printf '    %shttp://%s.local/setup%s\n' "$C_GREEN" "$HOSTNAME_CHOICE" "$C_OFF"
  elif [ "$DEPLOYMENT_MODE" = "pi_remote" ]; then
    say "Open this URL in your browser to point the device at your server:"
    printf '    %shttp://%s.local/setup%s\n' "$C_GREEN" "$HOSTNAME_CHOICE" "$C_OFF"
    say "(If .local doesn't resolve, use the device IP instead.)"
  else
    say "Open this URL in your browser to finish configuration:"
    printf '    %shttp://%s.local:9284/setup%s\n' "$C_GREEN" "$HOSTNAME_CHOICE" "$C_OFF"
    say "(If .local doesn't resolve, use the device IP instead.)"
  fi
  hr
}

main() {
  banner
  if [ "$NONINTERACTIVE" = "1" ]; then
    noninteractive_config
  else
    interactive_config
  fi
  # Pi Remote never runs Mealie/Ollama regardless of how flags arrived.
  if [ "$DEPLOYMENT_MODE" = "pi_remote" ]; then
    ENABLE_MEALIE=false; ENABLE_OLLAMA=false
  fi
  confirm_plan

  if [ "$PLAN_ONLY" = "1" ]; then
    # Emit a stable, greppable plan for tests/automation, then stop.
    printf 'PLAN mode=%s remote=%s mealie=%s ollama=%s kiosk=%s streamdeck=%s printing=%s rotation=%s hostname=%s repo_dir=%s\n' \
      "$DEPLOYMENT_MODE" "$REMOTE_SERVER_URL" \
      "$(yesno "$ENABLE_MEALIE")" "$(yesno "$ENABLE_OLLAMA")" \
      "$(yesno "$ENABLE_KIOSK")" "$(yesno "$ENABLE_STREAMDECK")" \
      "$(yesno "$ENABLE_PRINTING")" \
      "$DISPLAY_ROTATION" "$HOSTNAME_CHOICE" "$REPO_DIR"
    exit 0
  fi

  if [ "$NONINTERACTIVE" != "1" ]; then
    prompt_yn "Proceed with this install?" y || die "Aborted by user."
  fi

  need_root
  fetch_repo
  run_provisioner
  run_print_setup
  print_done
}

main "$@"
