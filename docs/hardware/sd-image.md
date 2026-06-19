# SD-card image guide

Flash a pre-configured FoodAssistant appliance to an SD card (or USB/NVMe),
boot your board, and reach the app at **http://foodassistant.local:9284/** with
minimal setup. No terminal required on the device.

> New to the hardware side? See [supported-hardware.md](supported-hardware.md)
> for boards, RAM guidance, and peripherals.

## How it works

FoodAssistant uses the official **Raspberry Pi OS Lite (64-bit)** image plus a
small **first-boot provisioner** instead of a bespoke custom image. On first
boot the device installs Docker, downloads the FoodAssistant + Grocy
containers, and starts them automatically. This keeps you on Raspberry Pi's
official, security-patched base image.

**Tradeoff:** the very first boot needs internet and takes a few minutes while
it pulls Docker and the container images. After that it is fully self-contained
and boots fast. (Maintainer/build details: `scripts/image-build/README.md`.)

## What you need

- A supported board — **Raspberry Pi 4 or Pi 5 (ARM64)** recommended; generic
  ARM64/x86-64 Debian/Ubuntu also works (see "Hardware coverage" below).
- A 16 GB+ SD card (32 GB+ recommended).
- Ethernet or Wi-Fi with internet for the first boot.
- A flashing tool: **Raspberry Pi Imager** (recommended) or **balenaEtcher**.

## Step 1 — Flash Raspberry Pi OS Lite (64-bit)

### Using Raspberry Pi Imager (recommended)

1. Install [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
2. **Choose Device:** your Pi model. **Choose OS:** *Raspberry Pi OS (other) →
   Raspberry Pi OS Lite (64-bit)*. **Choose Storage:** your card.
3. Click the gear / **Edit Settings** and set:
   - **Hostname:** `foodassistant` (optional — our config sets this too).
   - **Wi-Fi** credentials (skip if using Ethernet).
   - **Locale / timezone.**
   - Enable **SSH** if you want remote access (optional).
4. **Write** the image, but **do not eject yet.**

### Using balenaEtcher

Download Raspberry Pi OS Lite (64-bit) from
[raspberrypi.com](https://www.raspberrypi.com/software/operating-systems/),
flash it with [balenaEtcher](https://etcher.balena.io/), then continue to
Step 2 to add the FoodAssistant payload (Etcher has no customization, so the
prepare step is required).

## Step 2 — Add the FoodAssistant first-boot payload

After flashing, the card's **boot partition** (`bootfs`) reappears as a small
FAT volume on your PC. You need to copy the provisioner files onto it and edit
one config file.

First, clone the repository if you haven't already:

```bash
git clone https://github.com/Syracuse3DPrinting/FoodAssistant
cd FoodAssistant
```

Then follow the section for your operating system.

### Windows 11

You need two things from the repo: the `image\config.env` you edit, and a
helper script that copies everything onto the card for you. Clone the repo (or
download it as a ZIP from GitHub and extract it), then open **PowerShell** in
that folder.

**2a. Edit the config file**

Open `image\config.env` in any text editor (Notepad, VS Code) and set at least
the timezone:

```
TZ=America/New_York    # change to your IANA timezone
HOSTNAME=foodassistant # optional, sets the mDNS name (foodassistant.local)
```

Save and close the file.

**2b. Find the boot drive letter**

In File Explorer, look for a small (~256 MB) drive that appeared when you
inserted the card, labelled `bootfs`. Note its letter (often **D:** or **E:**).

**2c. Run the helper script**

```powershell
.\scripts\image-build\prepare-image.ps1 -BootDrive D:
```

Replace `D:` with your boot drive letter. The script copies the provisioner
files onto the card, installs your config, and wires `cmdline.txt` for you. It
refuses to run if the drive doesn't look like a Pi boot partition, so it won't
touch the wrong drive.

If PowerShell blocks the script with an execution-policy error, run it for this
one session only:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\image-build\prepare-image.ps1 -BootDrive D:
```

When it prints `Payload installed`, eject the card safely from the system tray
and skip to [Step 3](#step-3--first-boot).

### Linux / macOS (automated)

```bash
# Edit the appliance config first:
nano image/config.env
# Then point at the mounted boot volume (path varies by OS):
scripts/image-build/prepare-image.sh --boot-dir /Volumes/bootfs        # macOS
scripts/image-build/prepare-image.sh --boot-dir /media/$USER/bootfs    # Linux
```

You can also bake it into an `.img` before flashing (Linux, as root):

```bash
sudo scripts/image-build/prepare-image.sh --image path/to/raspios-lite-arm64.img
```

### Linux / macOS (manual)

If you prefer not to run the script, copy these files to the boot partition:

- `scripts/image-build/foodassistant-firstrun.sh` to `bootfs/foodassistant-firstrun.sh`
- All files from `scripts/image-build/` into `bootfs/foodassistant-setup/`
- `image/config.env` to both `bootfs/foodassistant-setup/config.env` and `bootfs/foodassistant.config.env`

Then append this to the single line in `bootfs/cmdline.txt` (one space before
it, no newline):

```
systemd.run=/boot/firmware/foodassistant-firstrun.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target
```

The script name is deliberately not `firstrun.sh`: Raspberry Pi Imager uses that
exact name for its own wifi/SSH/user setup, so reusing it would wipe those. Our
hook runs alongside Imager's, not in place of it.

Eject the card safely.

## Step 3 — First boot

1. Insert the card, connect network, power on.
2. The first boot runs the provisioner. Expect **a few minutes** while it
   installs Docker and pulls images. The device may reboot once.
3. Watch progress (if you enabled SSH):
   ```bash
   ssh <user>@foodassistant.local
   tail -f /var/log/foodassistant-firstboot.log
   ```

## Step 4 — Open the app

Browse to:

```
http://foodassistant.local:9284/
```

First time, you'll be sent to `http://foodassistant.local:9284/setup` to set a
password and add your Grocy + AI provider details.

If `foodassistant.local` doesn't resolve, use the device's IP:
`http://<device-ip>:9284/`. (Some Android devices and older Windows lack mDNS;
see Troubleshooting.)

## Configuration (`config.env`)

Set these in `image/config.env` (or directly in
`bootfs/foodassistant.config.env` after flashing):

| Key | Default | Purpose |
|-----|---------|---------|
| `HOSTNAME` | `foodassistant` | Hostname and mDNS name (`<name>.local`). |
| `TZ` | `America/New_York` | Timezone (IANA name). |
| `ENABLE_MEALIE` | `false` | Start Mealie (recipes/meal plan). Needs 4 GB RAM. |
| `ENABLE_OLLAMA` | `false` | Start local Ollama. Not recommended on SBCs. |
| `ENABLE_KIOSK` | `false` | Auto-launch full-screen Chromium **if a display is present**. |
| `ENABLE_STREAMDECK` | `false` | Install and start the Stream Deck controller (venv, driver, udev rule, systemd unit). |
| `KIOSK_URL` | `http://localhost:9284/ui/?kiosk=1` | What the kiosk opens. `?kiosk=1` enables the attached-display scale/orientation settings. |
| `FOODASSISTANT_TAG` | `latest` | Pin a specific app image version. |
| `INSTALL_DIR` | `/opt/foodassistant` | Where the stack is installed on-device. |

### Enabling Mealie / Ollama later

Edit `config.env` before flashing, **or** on a running device:

```bash
cd /opt/foodassistant
docker compose --profile with-mealie up -d     # add Mealie
docker compose --profile with-ollama up -d      # add Ollama
```

### Kiosk mode (touchscreen)

Set `ENABLE_KIOSK=true`. On first boot, if a display is detected (DRM/KMS, or
an X/Wayland session), the provisioner installs `cage` + Chromium and starts
`foodassistant-kiosk.service`, which opens `KIOSK_URL` full-screen on `tty1`.
On a headless box the flag is harmless — it logs and skips. Manage it with:

```bash
systemctl status foodassistant-kiosk
systemctl restart foodassistant-kiosk
```

## Hardware coverage

| Board / class | Status |
|---------------|--------|
| Raspberry Pi 5 (ARM64) | ✅ Recommended |
| Raspberry Pi 4B 4/8 GB (ARM64) | ✅ Supported |
| Raspberry Pi 4B 2 GB | 🟡 Grocy-only; Mealie tight |
| Generic x86-64 Debian/Ubuntu | ✅ Provisioner runs (boot-partition wiring is Pi-specific; run `firstboot.sh` directly) |
| Other ARM64 Debian/Ubuntu boards | 🟡 Best-effort; Docker install via get.docker.com |
| Pi 3B+ / Zero 2 W | ❌ Insufficient RAM |

On non-Pi hardware there's no `cmdline.txt`/`firstrun.sh` boot hook. Install
the provisioner directly:

```bash
sudo cp -r scripts/image-build /opt/foodassistant-setup
sudo cp image/config.env /etc/foodassistant/config.env   # mkdir -p first
sudo /opt/foodassistant-setup/firstboot.sh
```

See [supported-hardware.md](supported-hardware.md) for the full matrix.

## Troubleshooting

**`foodassistant.local` won't resolve.** mDNS isn't universal. Use the device
IP, or install Bonjour (Windows) / ensure `avahi-daemon` is running on the
device (`systemctl status avahi-daemon`). Find the IP from your router or
`ssh` with the IP.

**First boot seems stuck.** It's pulling Docker images — give it 5–10 minutes
on a slow connection. Check `tail -f /var/log/foodassistant-firstboot.log`.
The provisioner is idempotent and retries on transient failures
(`foodassistant-firstboot.service` is `Restart=on-failure`).

**Want to re-run provisioning.** Remove the marker and restart the service:

```bash
sudo rm -f /var/lib/foodassistant/firstboot.done
sudo systemctl start foodassistant-firstboot.service
# or run directly:  sudo FORCE=1 /opt/foodassistant-setup/firstboot.sh
```

**Verify the stack.**

```bash
cd /opt/foodassistant && docker compose ps
```

**Containers didn't start.** Confirm Docker installed:
`docker --version && docker compose version`. Re-run the provisioner (above).

**No internet on first boot.** Docker install and image pulls require it.
Connect the network and re-run provisioning.

**`docker compose up` fails with "unauthorized" or "pull access denied".** The
GHCR package for `ghcr.io/syracuse3dprinting/foodassistant` has not been made
public yet (or was recently re-privatized). To fix: go to the GitHub repo ->
Packages -> foodassistant -> Package settings -> Change visibility -> Public.
If the package is already public, this is a transient network error -- re-run
the provisioner. You do not have to do anything, though: when the pull fails,
`firstboot.sh` clones the (public) repo to `/home/foodassistant/FoodAssistant`
and builds the image from source automatically. That first build adds a few
minutes; pulling a public image is faster, so making the package public is still
worthwhile for at-scale imaging.
