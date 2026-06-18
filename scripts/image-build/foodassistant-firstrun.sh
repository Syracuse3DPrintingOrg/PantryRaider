#!/bin/sh
# FoodAssistant firstrun bootstrap (foodassistant-firstrun.sh)
# ============================================================
# Invoked via systemd.run=/boot/firmware/foodassistant-firstrun.sh in
# cmdline.txt alongside (not replacing) any firstrun.sh that Raspberry Pi
# Imager wrote for wifi/SSH/user-creation. It runs EARLY, as root, before the
# full system is up, so we keep it tiny: it just installs our systemd unit and
# hands off to firstboot.sh on the next/regular boot.
#
# This file, together with the foodassistant-setup payload, is placed on the
# boot partition by scripts/image-build/prepare-image.sh. If you flashed a
# stock image and copied files manually, see docs/hardware/sd-image.md.
#
# POSIX sh on purpose: this may run under the minimal early-boot environment.
set -e

BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot
SETUP_SRC="$BOOT/foodassistant-setup"
SETUP_DST=/opt/foodassistant-setup

log() { echo "[foodassistant-firstrun] $*"; }

log "FoodAssistant firstrun bootstrap starting"

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

# Install + enable the oneshot provisioner unit, which does the heavy lifting
# on this and subsequent boots until it succeeds.
if [ -f "$SETUP_DST/foodassistant-firstboot.service" ]; then
  cp "$SETUP_DST/foodassistant-firstboot.service" /etc/systemd/system/
  systemctl daemon-reload || true
  systemctl enable foodassistant-firstboot.service || true
  log "Enabled foodassistant-firstboot.service"
fi

# Kick it off now so the appliance comes up on this first boot without waiting
# for a reboot. The service self-disables once provisioning succeeds.
systemctl start foodassistant-firstboot.service || \
  log "Provisioner will run on next boot"

log "FoodAssistant firstrun bootstrap done"
