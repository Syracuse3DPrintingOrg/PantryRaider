# FoodAssistant SD-card image tooling

This directory builds a **flashable appliance** from the official Raspberry Pi
OS Lite (64-bit) image, without compiling a custom image from scratch. A
first-boot provisioner installs Docker, deploys the FoodAssistant + Grocy
stack, sets up mDNS (`pr.local`), and optionally launches a
Chromium kiosk.

End-user instructions live in [`docs/hardware/sd-image.md`](../../docs/hardware/sd-image.md).
This README is the developer/maintainer view.

## Approach & tradeoff

We **layer a first-boot script on top of stock Pi OS Lite** rather than baking a
full custom image with `pi-gen` / `rpi-image-gen`.

| | First-boot layering (this) | Full custom image |
|---|---|---|
| Build infra | None (copy files to boot partition) | arm64 host or qemu, ~30 min builds |
| Upstream security updates | Inherited from official image | Must re-spin |
| First boot | Online, a few minutes (pull Docker + images) | Instant, fully offline |
| Maintenance | Bump compose tags | Track Pi OS releases |

The only cost is a one-time online first boot. For a single reproducible
artifact you can still wrap these assets in `pi-gen` later.

## Files

| File | Role |
|------|------|
| `firstboot.sh` | The provisioner. Idempotent, logs to `/var/log/foodassistant-firstboot.log`, supports `DRY_RUN=1`. Does the real work (Docker, mDNS, stack, kiosk). |
| `foodassistant-firstrun.sh` | Tiny POSIX bootstrap that the `cmdline.txt` hook runs once (via `systemd.run`). Removes its own hook first, copies the payload to `/opt/foodassistant-setup`, and enables `foodassistant-firstboot.service` for the next boot. |
| `foodassistant-firstboot.service` | systemd oneshot that runs `firstboot.sh` until it succeeds (survives reboots / transient network). |
| `docker-compose.appliance.yml` | Compose stack (adapted from `docker-compose.prod.yml`); Grocy on by default, Mealie/Ollama profile-gated. |
| `prepare-image.sh` | Bakes the above into a stock `.img` boot partition or an already-flashed boot dir, and wires `cmdline.txt`. |
| `../../image/config.env` | User-editable appliance config consumed by `firstboot.sh`. |

## Flow

At image-prep time, `prepare-image.sh` copies `foodassistant-firstrun.sh`, the
`foodassistant-setup/` payload, and `foodassistant.config.env` into
`/boot/firmware/`, and appends a hook to `cmdline.txt`:
`systemd.run=/boot/firmware/foodassistant-firstrun.sh
systemd.run_success_action=reboot systemd.unit=kernel-command-line.target`.

The first boot goes to `kernel-command-line.target` and runs
`foodassistant-firstrun.sh`. Its first action is to delete that exact hook
from `cmdline.txt`. Nothing else removes it: Raspberry Pi Imager's
`firstrun.sh` used to strip every `systemd.run` entry, but Raspberry Pi OS
Trixie does its first-boot setup with cloud-init and has no such script. A
hook left in place sends every boot back to the script and reboots, so the
device never reaches `multi-user.target`. The script then copies the payload
to `/opt/foodassistant-setup` and enables (does not start)
`foodassistant-firstboot.service`, and systemd reboots.

The second boot is a normal multi-user boot. cloud-init creates the login user
and the network from `user-data` and `network-config` before
`network-online.target`, and then `foodassistant-firstboot.service`
(`After=network-online.target`) runs `firstboot.sh`, which loads the config,
sets the hostname and timezone, brings up avahi for mDNS, installs Docker,
deploys the stack, provisions the kiosk when one is attached (as the uid 1000
user that cloud-init created), and marks the boot done.

## First-boot settings on the pre-built image

Raspberry Pi Imager 2.x disables OS customization for third-party images, and
Raspberry Pi OS has ignored `wpa_supplicant.conf` since Bookworm. On the
pre-built image, Wi-Fi, the login user, and SSH come from the cloud-init seed
files on the boot partition: `network-config` (netplan v2 `wifis`, with
`renderer: NetworkManager`) and `user-data` (`users:`, `enable_ssh`,
`ssh_pwauth`). They ship as comments only, so there is no login user until
`user-data` creates one, and the kiosk and Stream Deck steps skip without it.
The end-user steps, including recovery for cards from earlier images that keep
restarting, are in
[`docs/hardware/sd-image.md`](../../docs/hardware/sd-image.md).

## Testing

```bash
# Lint
shellcheck -s bash firstboot.sh prepare-image.sh
shellcheck -s sh   foodassistant-firstrun.sh

# Validate compose
docker compose -f docker-compose.appliance.yml config -q

# Dry-run the provisioner (no installs, no Docker, no system writes)
DRY_RUN=1 ./firstboot.sh

# Config-parsing / decision tests
python -m pytest ../../tests/test_firstboot_config.py -q

# The cmdline.txt hook: prepare-image.sh adds it, firstrun removes it
python -m pytest ../../tests/test_prepare_image.py -q
```

`DRY_RUN=1` exercises every decision branch (profiles, kiosk display gating,
hostname, done-marker) and prints the actions it *would* take.
