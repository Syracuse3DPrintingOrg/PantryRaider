#!/usr/bin/env bash
# prepare-image.sh — bake the FoodAssistant first-boot provisioner into a stock
# Raspberry Pi OS Lite (64-bit) image, or onto an already-flashed boot partition.
# ==============================================================================
# WHY THIS APPROACH (and the tradeoff):
#   Building a fully custom image from scratch (pi-gen / rpi-image-gen) is
#   heavyweight: it needs an arm64 build host or qemu, bind-mounts, ~30 min
#   builds, and ongoing maintenance to track upstream Pi OS. We instead layer a
#   *first-boot provisioner* on top of the official, already-trusted Pi OS Lite
#   image. The cost is a one-time online first boot (a few minutes to install
#   Docker + pull images); the benefit is near-zero build infrastructure, easy
#   updates (just bump the compose tags), and users keep the upstream image's
#   security updates. For a reproducible single-file artifact you can still wrap
#   this in pi-gen later — see docs/hardware/sd-image.md.
#
# This script supports two modes:
#   (A) Mount an .img file's boot partition and copy assets in (default).
#   (B) Copy assets to an already-mounted boot dir (e.g. the FAT volume that
#       appears after Raspberry Pi Imager writes the card) via --boot-dir.
#
# Usage:
#   sudo ./prepare-image.sh --image 2025-xx-raspios-lite-arm64.img [--config ../../image/config.env]
#   ./prepare-image.sh --boot-dir /media/$USER/bootfs [--config ../../image/config.env]
#
# It does NOT flash the card. Flash with Raspberry Pi Imager or balenaEtcher.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG_SRC="$REPO_ROOT/image/config.env"
IMAGE=""
BOOT_DIR=""

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,30p' "$0"
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --image)    IMAGE="${2:?--image needs a path}"; shift 2 ;;
    --boot-dir) BOOT_DIR="${2:?--boot-dir needs a path}"; shift 2 ;;
    --config)   CONFIG_SRC="${2:?--config needs a path}"; shift 2 ;;
    -h|--help)  usage 0 ;;
    *) die "Unknown argument: $1 (try --help)" ;;
  esac
done

[ -f "$CONFIG_SRC" ] || die "Config not found: $CONFIG_SRC"

# Assets copied into the boot partition under foodassistant-setup/.
ASSETS="
$SCRIPT_DIR/firstboot.sh
$SCRIPT_DIR/foodassistant-firstrun.sh
$SCRIPT_DIR/foodassistant-firstboot.service
$SCRIPT_DIR/docker-compose.appliance.yml
"

# Copy the provisioner payload + config into a boot directory.
install_payload() {
  local boot="$1"
  [ -d "$boot" ] || die "Boot dir does not exist: $boot"
  say "Installing payload into $boot/foodassistant-setup"
  mkdir -p "$boot/foodassistant-setup"
  local a
  for a in $ASSETS; do
    [ -f "$a" ] || die "Missing asset: $a"
    cp "$a" "$boot/foodassistant-setup/"
  done
  cp "$CONFIG_SRC" "$boot/foodassistant-setup/config.env"
  # Also place the user-editable config at the top level for easy editing.
  if [ ! -f "$boot/foodassistant.config.env" ]; then
    cp "$CONFIG_SRC" "$boot/foodassistant.config.env"
  fi
  # Place our bootstrap script on the boot partition under its own name, leaving
  # any firstrun.sh that Raspberry Pi Imager wrote (wifi/SSH/user-creation) intact.
  if [ -f "$boot/firstrun.sh" ]; then
    say "NOTE: Raspberry Pi Imager's firstrun.sh is present -- NOT overwriting it."
    say "      Only adding foodassistant-firstrun.sh alongside it."
  fi
  cp "$SCRIPT_DIR/foodassistant-firstrun.sh" "$boot/foodassistant-firstrun.sh"
  chmod +x "$boot/foodassistant-firstrun.sh" "$boot/foodassistant-setup/firstboot.sh"

  # Wire cmdline.txt to invoke foodassistant-firstrun.sh once. Only append if
  # not already present, to stay idempotent. We leave any existing
  # firstrun.sh hook (placed by Raspberry Pi Imager) untouched.
  if [ -f "$boot/cmdline.txt" ]; then
    if ! grep -q 'systemd.run=.*foodassistant-firstrun.sh' "$boot/cmdline.txt"; then
      say "Wiring cmdline.txt to run foodassistant-firstrun.sh on first boot"
      # cmdline.txt is a single line; append our systemd.run hook.
      local line
      line="$(tr -d '\n' < "$boot/cmdline.txt")"
      printf '%s systemd.run=/boot/firmware/foodassistant-firstrun.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target\n' \
        "$line" > "$boot/cmdline.txt"
    else
      say "cmdline.txt already wired for foodassistant-firstrun.sh; leaving as-is"
    fi
  else
    say "NOTE: no cmdline.txt here. foodassistant-firstrun.sh is installed; the systemd unit"
    say "      (foodassistant-firstboot.service) will provision on first boot"
    say "      if you enable it, or rely on Pi Imager's own firstrun hook."
  fi
  say "Payload installed."
}

if [ -n "$BOOT_DIR" ]; then
  install_payload "$BOOT_DIR"
  say "Done. Eject the card and boot your device."
  exit 0
fi

[ -n "$IMAGE" ] || usage 1
[ -f "$IMAGE" ] || die "Image not found: $IMAGE"
command -v losetup >/dev/null 2>&1 || die "losetup required for --image mode (run on Linux as root)"
[ "$(id -u)" -eq 0 ] || die "--image mode must run as root (mounts loop devices)"

say "Attaching $IMAGE to a loop device"
LOOP="$(losetup --show -fP "$IMAGE")"
cleanup() { umount "$MNT" 2>/dev/null || true; losetup -d "$LOOP" 2>/dev/null || true; rmdir "$MNT" 2>/dev/null || true; }
trap cleanup EXIT
MNT="$(mktemp -d)"
# Boot partition is the first FAT partition (p1).
say "Mounting ${LOOP}p1 (boot partition)"
mount "${LOOP}p1" "$MNT"
install_payload "$MNT"
sync
say "Done. Customized image at: $IMAGE"
say "Flash it with Raspberry Pi Imager or balenaEtcher."
