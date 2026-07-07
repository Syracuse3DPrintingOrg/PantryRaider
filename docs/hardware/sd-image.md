# SD-card image guide

Set up a Pantry Raider appliance on a Raspberry Pi in four steps: flash a stock
Raspberry Pi OS Lite card, boot, SSH in, and run one install command. The
installer asks what you want (full stack, or a thin remote) and provisions only
that. No files to edit on your PC, nothing to clone on your PC.

> New to the hardware side? See [supported-hardware.md](supported-hardware.md)
> for boards, RAM guidance, and peripherals. For how a Pi install protects the
> SD card from power-loss corruption and which stack to run per board, see
> [Pi reliability and memory tiers](pi-reliability.md).

## How it works

Pantry Raider runs on the official **Raspberry Pi OS Lite (64-bit)** image plus
an on-device installer. You flash the stock OS, boot it, then run the installer
over SSH. It detects the board, any attached display, and any attached Stream
Deck, asks for the deployment mode and add-ons, then installs Docker and the
containers (for a full host) or just the kiosk/Stream Deck (for a thin remote).

**Tradeoff:** the install needs internet and takes a few minutes the first time
while it pulls Docker and the container images. After that the device is
self-contained and boots fast. Staying on the official base image means you keep
Raspberry Pi's security updates. (Maintainer/build details:
`scripts/image-build/README.md`.)

## What you need

- A supported board: **Raspberry Pi 4 or Pi 5 (ARM64)** for a full host; a
  **Pi 3** is fine for a thin remote (see "Hardware coverage" below).
- A 16 GB+ SD card (32 GB+ recommended).
- Ethernet or Wi-Fi with internet.
- **Raspberry Pi Imager** on your PC to flash the card.

## Step 1: Flash Raspberry Pi OS Lite (64-bit)

1. Install [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
2. **Choose Device:** your Pi model. **Choose OS:** *Raspberry Pi OS (other) →
   Raspberry Pi OS Lite (64-bit)*. **Choose Storage:** your card.
3. Click the gear / **Edit Settings** and set:
   - **Hostname:** `foodassistant` (this becomes `foodassistant.local`).
   - **Enable SSH** (use a password or your public key); it is required for Step 3.
   - **Wi-Fi** credentials (skip if using Ethernet).
   - **Locale / timezone.**
4. **Write** the image, then eject the card.

That is the only thing you do on your PC. Everything else happens on the Pi.

## Step 2: Boot the Pi

Insert the card, connect the network (Ethernet or the Wi-Fi you configured), and
power on. Give it a minute to come up on the network.

## Step 3: SSH in and run the installer

From your PC, SSH to the Pi using the user and hostname you set in Imager:

```bash
ssh <user>@foodassistant.local
```

If `foodassistant.local` doesn't resolve, use the Pi's IP address (find it in
your router, or it may print on an attached screen).

Then run the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/install.sh | bash
```

The installer shows what it detected (board, display, Stream Deck) and asks one
question:

- **Deployment mode**
  - **Pi Hosted**: run the full Pantry Raider stack on this Pi (Pantry Raider +
    Grocy). Pick this for a normal appliance.
  - **Pi Remote**: thin client. Installs **no** Docker, Grocy, or Mealie; this
    device only drives a kiosk and/or Stream Deck pointed at a Pantry Raider
    server already running elsewhere on your LAN. Viable on a Pi 3. It asks for
    that server's URL.

The kiosk browser and Stream Deck controller are auto-enabled when the hardware
is detected at install time, and Mealie (recipes, meal plans, shopping lists)
installs by default on a hosted device (set `ENABLE_MEALIE=false` to skip it).
Everything else (Ollama, display rotation, AI provider, password, Grocy key) is
configured in the browser after the install completes.

When it finishes, the terminal prints the URL to open in your browser.

### Non-interactive / scripted installs

The installer can run unattended by passing the choices as environment variables
and setting `NONINTERACTIVE=1`:

```bash
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/install.sh \
  | NONINTERACTIVE=1 DEPLOYMENT_MODE=pi_hosted ENABLE_MEALIE=true bash
```

Recognized variables: `DEPLOYMENT_MODE` (`pi_hosted` | `pi_remote` | `server`),
`REMOTE_SERVER_URL`, `ENABLE_MEALIE`, `ENABLE_OLLAMA`, `ENABLE_KIOSK`,
`ENABLE_STREAMDECK`, `DISPLAY_ROTATION`, `HOSTNAME`. Anything left unset is
auto-detected (kiosk/Stream Deck default to whether the hardware is attached).

## Step 4: Complete setup in the browser

The installer prints the URL when it finishes. Open it:

```
http://foodassistant.local:9284/setup
```

The web wizard takes you through: deployment mode confirmation, security
(set a password), hardware (display scale and rotation, Stream Deck), Grocy
connection, AI provider, and optional integrations. When you click
**Start using Pantry Raider** everything is saved and you're done.

A Pi Remote install has no local app; it drives the server URL you gave the
installer.

If `foodassistant.local` doesn't resolve, use the device's IP:
`http://<device-ip>:9284/`. (Some Android devices and older Windows lack mDNS;
see Troubleshooting.)

## Pre-built appliance image (advanced, no SSH)

If you want a flash-and-go card with no SSH step, a pre-built
`foodassistant-appliance-*-arm64.img.xz` is published to the
[Releases page](https://github.com/Syracuse3DPrintingOrg/PantryRaider/releases).
It bakes the provisioner into the image so it self-installs the full Pi Hosted
stack on first boot. Flash it with balenaEtcher or `dd`.

> **Limitations:** Raspberry Pi Imager 2.x disables the OS customization tab
> (Wi-Fi, SSH, hostname) for third-party images, so with the pre-built image you
> configure Wi-Fi manually (drop a `wpa_supplicant.conf` on the boot partition)
> and create an empty `/boot/firmware/ssh` file to enable SSH. The pre-built
> image always installs the full host stack; to choose Pi Remote or pick
> add-ons, use the stock-OS + installer path above. The pre-built image takes
> its timezone from whatever the OS is set to and auto-detects display/Stream
> Deck.

## Add-ons and settings

The installer auto-enables the kiosk and Stream Deck when the hardware is
present. Use **Settings** in the web UI to adjust display settings, Stream Deck
configuration, Wi-Fi, and hostname at any time.

To add optional backends to a running device:

### Enable Mealie / Ollama later

```bash
cd /opt/foodassistant
docker compose --profile with-mealie up -d     # add Mealie
docker compose --profile with-ollama up -d      # add Ollama
```

### Display rotation

Choose a rotation in the installer, or change it later without reflashing:

```bash
sudo /usr/local/bin/foodassistant-set-rotation 90 --reboot
```

This rotates the KMS framebuffer (boot console, splash, and kiosk). The app's
Settings page also offers a CSS-only rotation for the kiosk browser (no reboot,
but it does not affect the boot console).

### Kiosk mode (touchscreen)

If a display is present at install time the installer offers to set up the
kiosk: it installs `cage` + Chromium and starts `foodassistant-kiosk.service`,
which opens the app full-screen on `tty1`. Manage it with:

```bash
systemctl status foodassistant-kiosk
systemctl restart foodassistant-kiosk
```

A display added later lights the kiosk up on its own: the device notices the
screen within about a minute of it being plugged in and provisions (or starts)
the kiosk with no reflash or SSH needed. The Settings Hardware pane shows the
same detection with a one-click Enable button. On a kiosk device the boot
console is also quieted, so the screen stays clean from power-on until the app
appears.

## Hardware coverage

| Board / class | Status |
|---------------|--------|
| Raspberry Pi 5 (ARM64) | ✅ Recommended |
| Raspberry Pi 4B 4/8 GB (ARM64) | ✅ Supported |
| Raspberry Pi 4B 2 GB | 🟡 Grocy-only; Mealie tight |
| Raspberry Pi 3B+ / equivalent | ✅ Pi Remote (thin client) only |
| Generic x86-64 Debian/Ubuntu | ✅ Server mode (the installer runs the same way) |
| Other ARM64 Debian/Ubuntu boards | 🟡 Best-effort; Docker via get.docker.com |
| Pi Zero 2 W | ❌ Insufficient RAM |

The installer runs on any Debian/Ubuntu host, not just a Pi. On a non-Pi host it
selects **Server** mode automatically.

See [supported-hardware.md](supported-hardware.md) for the full matrix.

## Troubleshooting

**`foodassistant.local` won't resolve.** mDNS isn't universal. Use the device
IP, or install Bonjour (Windows) / ensure `avahi-daemon` is running on the
device (`systemctl status avahi-daemon`). Find the IP from your router.

**The install seems stuck.** It's pulling Docker images: give it 5 to 10 minutes on
a slow connection. The provisioner logs to
`/var/log/foodassistant-firstboot.log` (`tail -f` it in another SSH session).

**The Stream Deck isn't detected (`No Stream Deck found` in the log).** Check
the USB cable first: many USB-C and micro-USB cables are charge-only, so the
deck lights up but carries no data. A deck that disconnects at random usually
means an undersized power supply instead. Both are covered in
[Power and cabling](../hardware.md#power-and-cabling).

**Re-run the installer / change choices.** Just run the `curl ... | bash` line
again. To force the provisioner to redo a completed step set `FORCE=1`:

```bash
sudo rm -f /var/lib/foodassistant/firstboot.done
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/install.sh | bash
```

**Verify the stack.**

```bash
cd /opt/foodassistant && docker compose ps
```

**Containers didn't start.** Confirm Docker installed:
`docker --version && docker compose version`, then re-run the installer.

**No internet during install.** Docker install and image pulls require it.
Connect the network and re-run.

**`docker compose up` fails with "unauthorized" or "pull access denied".** The
GHCR package may be private. The installer handles this automatically: when the
pull fails it builds the image from the on-device checkout at
`/opt/foodassistant-src`. That first build adds a few minutes.
