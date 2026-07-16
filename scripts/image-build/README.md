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
| `firstrun.sh` | Tiny POSIX bootstrap run once by Pi OS via `cmdline.txt` (the Raspberry Pi Imager mechanism). Installs the systemd unit and kicks off `firstboot.sh`. |
| `foodassistant-firstboot.service` | systemd oneshot that runs `firstboot.sh` until it succeeds (survives reboots / transient network). |
| `docker-compose.appliance.yml` | Compose stack (adapted from `docker-compose.prod.yml`); Grocy on by default, Mealie/Ollama profile-gated. |
| `prepare-image.sh` | Bakes the above into a stock `.img` boot partition or an already-flashed boot dir, and wires `cmdline.txt`. |
| `../../image/config.env` | User-editable appliance config consumed by `firstboot.sh`. |

## Flow

```
prepare-image.sh  ──copies──▶  /boot/firmware/{firstrun.sh, foodassistant-setup/, foodassistant.config.env}
                   ──wires───▶  cmdline.txt  (systemd.run=…/firstrun.sh)

first boot:
  cmdline.txt → firstrun.sh → installs+starts foodassistant-firstboot.service
                                   └─▶ firstboot.sh:
                                         load config → set hostname/TZ → avahi (mDNS)
                                         → install Docker → deploy stack → [kiosk] → mark done
```

## Testing

```bash
# Lint
shellcheck -s bash firstboot.sh prepare-image.sh
shellcheck -s sh   firstrun.sh

# Validate compose
docker compose -f docker-compose.appliance.yml config -q

# Dry-run the provisioner (no installs, no Docker, no system writes)
DRY_RUN=1 ./firstboot.sh

# Config-parsing / decision tests
python -m pytest ../../tests/test_firstboot_config.py -q
```

`DRY_RUN=1` exercises every decision branch (profiles, kiosk display gating,
hostname, done-marker) and prints the actions it *would* take.
