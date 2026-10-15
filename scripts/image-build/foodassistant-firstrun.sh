#!/bin/sh
# FoodAssistant firstrun bootstrap (foodassistant-firstrun.sh)
# ============================================================
# Invoked once via systemd.run=/boot/firmware/foodassistant-firstrun.sh, the
# hook prepare-image.sh appends to cmdline.txt. It runs EARLY, as root, in the
# minimal kernel-command-line boot, so we keep it tiny.
#
# It no longer runs alongside a Raspberry Pi Imager firstrun.sh. That script
# used to strip every systemd.run entry from cmdline.txt, but Raspberry Pi OS
# Trixie sets up the user, Wi-Fi and SSH with cloud-init instead and has no
# firstrun.sh, so nothing else removes our hook. Left in place, every boot
# would come straight back here and reboot, and the device would never reach
# a normal boot. So the first thing this script does is take its own hook out
# of cmdline.txt.
#
# Then it copies the provisioning payload onto the rootfs and enables
# foodassistant-firstboot.service, without starting it. When this script
# exits, systemd reboots (systemd.run_success_action=reboot) into a normal
# multi-user boot: cloud-init creates the login user and the network before
# network-online.target, and the enabled unit (After=network-online.target)
# runs firstboot.sh.
#
# This file, together with the foodassistant-setup payload, is placed on the
# boot partition by scripts/image-build/prepare-image.sh. If you flashed a
# stock image and copied files manually, see docs/hardware/sd-image.md.
#
# POSIX sh on purpose: this runs under the minimal early-boot environment.
# FOODASSISTANT_BOOT, FOODASSISTANT_SETUP_DST and FOODASSISTANT_UNIT_DIR
# override the paths below (tests/test_prepare_image.py uses them).
set -e

if [ -n "${FOODASSISTANT_BOOT:-}" ]; then
  BOOT="$FOODASSISTANT_BOOT"
else
  BOOT=/boot/firmware
  [ -d "$BOOT" ] || BOOT=/boot
fi

# Remove our own hook before anything that can fail under set -e, so a later
# failure can never leave the device rebooting into this script forever. Only
# the exact string prepare-image.sh appends is removed; the rest of the line
# stays as it was.
if [ -f "$BOOT/cmdline.txt" ]; then
  sed -i 's# systemd\.run=/boot/firmware/foodassistant-firstrun\.sh systemd\.run_success_action=reboot systemd\.unit=kernel-command-line\.target##' "$BOOT/cmdline.txt" || true
fi

SETUP_SRC="$BOOT/foodassistant-setup"
SETUP_DST="${FOODASSISTANT_SETUP_DST:-/opt/foodassistant-setup}"
UNIT_DIR="${FOODASSISTANT_UNIT_DIR:-/etc/systemd/system}"

log() { echo "[foodassistant-firstrun] $*"; }

log "FoodAssistant firstrun bootstrap starting"
if grep -q 'foodassistant-firstrun' "$BOOT/cmdline.txt" 2>/dev/null; then
  log "WARN: $BOOT/cmdline.txt still mentions foodassistant-firstrun; remove it by hand if the device keeps restarting"
fi

# Copy the provisioning payload off the (FAT) boot partition onto the rootfs so
# it survives and runs with proper permissions.
if [ -d "$SETUP_SRC" ]; then
  mkdir -p "$SETUP_DST"
  cp -a "$SETUP_SRC"/. "$SETUP_DST"/
  chmod +x "$SETUP_DST"/firstboot.sh 2>/dev/null || true
else
  log "WARN: $SETUP_SRC not found; cannot install provisioner"
fi

# Make the user config discoverable by firstboot.sh at its boot-partition path.
if [ -f "$BOOT/foodassistant.config.env" ]; then
  log "Found user config at $BOOT/foodassistant.config.env"
elif [ -f "$SETUP_DST/config.env" ]; then
  cp "$SETUP_DST/config.env" "$BOOT/foodassistant.config.env" || true
  log "Seeded default config to $BOOT/foodassistant.config.env"
fi

# Install + enable the oneshot provisioner unit. It is not started here: it
# runs on the normal boot that follows this one, and on later boots until it
# succeeds.
if [ -f "$SETUP_DST/foodassistant-firstboot.service" ]; then
  cp "$SETUP_DST/foodassistant-firstboot.service" "$UNIT_DIR"/
  systemctl daemon-reload || true
  systemctl enable foodassistant-firstboot.service || true
  log "Enabled foodassistant-firstboot.service; it provisions on the next boot"
fi

log "FoodAssistant firstrun bootstrap done; rebooting into a normal boot"
