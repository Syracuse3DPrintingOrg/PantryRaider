# Get started

This page takes you from nothing installed to a working kitchen: pick where
Pantry Raider runs, install it, finish the short setup in your browser, and
add your first food. On a machine that already runs Docker it takes about 15
minutes, most of it spent scanning groceries.

Pantry Raider is in public beta. Back up before you update, and tell us what
breaks (see [Getting help](#getting-help) at the bottom of this page).

## What you need

- **Somewhere to run it.** Any 64-bit Linux machine with Docker (a NAS, a mini
  PC, a virtual machine), an Unraid server, Home Assistant OS, or a Raspberry
  Pi 4 or 5. Plan on 2 GB of memory for Pantry Raider and its inventory, and a
  few GB of disk. See [Supported hardware](hardware/supported-hardware.md) for
  the boards and limits.
- **An internet connection during the install**, to download the app. After
  that, the kitchen runs on your own network.
- **Optional: an AI key** for photo and receipt scanning. A free Google Gemini
  key from [aistudio.google.com](https://aistudio.google.com/) is the easiest
  start, and you can add one later. Everything else works without AI.
- **Optional: a USB barcode scanner.** Any scanner that acts as a keyboard
  works. A phone camera works too.

## Pick an install

| What you have | Install | Updates |
|---|---|---|
| A Linux server, NAS or VM with Docker | [Docker in one command](#docker-in-one-command) | Automatic, daily |
| Unraid | [Install on Unraid](unraid.md) | From the Docker tab |
| Home Assistant OS or Supervised | [Home Assistant add-on](#home-assistant-add-on) | From the add-on page |
| A Raspberry Pi 4 or 5 | [Raspberry Pi appliance](#raspberry-pi-appliance) | Automatic, from signed releases |
| A Debian or Ubuntu machine set aside for the kitchen | [Dedicated server](#dedicated-debian-or-ubuntu-server) | Automatic, from signed releases |

If you are not sure, use Docker. It is the most tested way to run Pantry
Raider, and every install keeps your data in a plain folder you can back up.

### Docker in one command

You need Docker 24 or newer with Compose v2. Check with `docker version` and
`docker compose version`; if Docker is missing, the
[Docker install guide](https://docs.docker.com/engine/install/) covers every
common Linux.

Run this as a user who can use Docker (or with `sudo`):

```bash
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/scripts/install.sh | bash
```

It creates a `foodassistant` folder in the current directory, downloads the
compose file into it, and starts Pantry Raider with its own Grocy inventory.
When it finishes it prints the address of the setup page. Keep the folder: it
holds all of your data.

Ports 9284 (Pantry Raider) and 9383 (Grocy) must be free. If something else
already uses one, the README explains how to
[pick other ports](https://github.com/Syracuse3DPrintingOrg/PantryRaider#option-1---docker-server-nas-proxmox-truenas-unraid).

### Home Assistant add-on

In Home Assistant, open **Settings > Add-ons > Add-on Store**, choose
**Repositories** from the menu, and add
`https://github.com/Syracuse3DPrintingOrg/PantryRaider`. Install the community
**Grocy** add-on first, then install and start **Pantry Raider** and click
**Open Web UI**. The
[add-on guide](https://github.com/Syracuse3DPrintingOrg/PantryRaider/blob/main/homeassistant/addon/foodassistant/DOCS.md)
has the details.

### Raspberry Pi appliance

Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager, setting the
hostname to `pr`, your Wi-Fi, and SSH. Boot the Pi, sign in over SSH, and run:

```bash
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/install.sh | bash
```

The installer asks how the Pi will be used (a kitchen screen, headless, or a
remote screen for a server elsewhere) and whether you want label printing,
then sets everything up. The [SD-card guide](hardware/sd-image.md) walks
through each step and also covers the ready-made card image, which skips SSH
entirely.

### Dedicated Debian or Ubuntu server

The same installer works on a 64-bit Debian 12 or 13 or Ubuntu 24.04 machine
that does nothing but run the kitchen. It installs Docker if needed, puts the
stack in `/opt/foodassistant`, and keeps it up to date from signed releases,
the way a Raspberry Pi appliance does. It does not rename the machine. Run it
over SSH as a user with `sudo`:

```bash
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/install.sh | bash
```

It asks whether you want label printing, shows the plan, and asks you to
confirm. To answer everything up front instead (for a script, or a session
without a terminal):

```bash
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/install.sh \
  | NONINTERACTIVE=1 DEPLOYMENT_MODE=server bash
```

## Finish setup in your browser

Open the address the installer printed, usually `http://YOUR-HOST:9284/setup`
(`http://pr.local:9284/setup` on a Pi). The wizard has seven short steps:

1. **Welcome.** Pick how this device is used: **Server hosted** for Docker,
   Unraid or a dedicated server, **Pi Hosted** for a Raspberry Pi appliance.
2. **Security.** Choose a password for the app. This is the one step not to
   skip.
3. **Hardware.** Screen size, rotation and a Stream Deck. Only matters if this
   machine drives a kitchen screen.
4. **Inventory.** Pantry Raider sets up its Grocy inventory for you; wait for
   the green "ready" line. If you run your own Grocy, open **Advanced** and
   enter its address and API key instead.
5. **AI.** Paste an AI key to turn on photo and receipt scanning, or skip it.
6. **Optional.** Your scanner type, recipe suggestions from the web, remote
   access, and an existing Mealie, all of which can wait.
7. **Done.** Click **Start using Pantry Raider**.

Every choice can be changed later in Settings. [First run](first-run.md)
explains what the app sets up for you behind the scenes.

### The setup code

A device on your home network opens the wizard straight away. If you open it
from somewhere else (through a reverse proxy, a tunnel, or a phone on mobile
data), a new kitchen first asks for its setup code. Find the newest line that
starts with `Pantry Raider setup code`:

- **Docker, a dedicated server, or a Pi:** run
  `sudo docker logs foodassistant-service 2>&1 | grep "setup code"`.
- **Unraid:** the Docker tab, click Pantry Raider, then **Logs**.
- **Home Assistant add-on:** the add-on's **Log** tab.

The code works once, within 24 hours.

## Your first food

- **Add groceries.** Open **Manage**. With **Stock up** selected, scan a
  barcode, type one in, take a photo of an item or a receipt, or enter it by
  hand. New items wait on the **Review** page so you can check names and dates
  before they reach your inventory.
- **See what you have.** **Inventory** groups everything by fridge, freezer,
  counter and pantry. **Expiring** lists what to use first.
- **Cook from it.** **Cook** ranks recipes by how much of each you already have.
  Import a recipe from any web page, or start from a suggestion.
- **Use it up.** On **Manage**, pick **Use stock** and scan an item as you
  finish it.

On a phone, **Open on phone** on the Manage page shows a QR code that opens the
same page on your phone, camera ready.

## Back up

Your data lives in a few folders. Back them up before every update.

| Install | Where your data is |
|---|---|
| Docker in one command | The `foodassistant` folder: `data` (Pantry Raider), `grocy` (your inventory) and `mealie` if you run it |
| Raspberry Pi or dedicated server | `/opt/foodassistant`: the same `data` and `grocy` folders |
| Unraid | `/mnt/user/appdata/pantryraider`, plus your Grocy's appdata folder |
| Home Assistant add-on | Home Assistant's own backups include the add-on |

For a full copy on Docker, stop the stack, archive the folders, and start it
again, from inside the `foodassistant` folder:

```bash
docker compose --profile with-grocy stop
sudo tar czf pantryraider-full-backup.tar.gz data grocy
docker compose --profile with-grocy start
```

In the app, **Settings > Backups & Updates > Download Backup** saves Pantry
Raider's own settings and data as a zip (your inventory lives in Grocy, so it
is not in that zip). The same page can back up to a USB drive on a schedule or
to cloud storage through rclone, and a Raspberry Pi's USB backups include the
whole inventory.

## Update

- **Docker in one command:** a bundled updater installs each new release on
  its own, once a day. To update right away, run `docker compose pull` and
  then `docker compose up -d` in the `foodassistant` folder.
- **Raspberry Pi or dedicated server:** updates install on their own from
  signed releases. **Settings > Backups & Updates > Check and update** does it
  now.
- **Unraid:** apply the update from the Docker tab when it appears.
- **Home Assistant add-on:** update from the add-on page.

The version you run is shown in **Settings > Backups & Updates**. The
[Changelog](https://github.com/Syracuse3DPrintingOrg/PantryRaider/blob/main/CHANGELOG.md)
says what changed in each release.

## Getting help

- **Something broken or confusing?** Open an issue on
  [GitHub](https://github.com/Syracuse3DPrintingOrg/PantryRaider/issues).
  Include the version from Settings, Backups & Updates, how you installed it,
  and what you expected to happen. For a tricky problem, turn on
  **Settings > Advanced > Debug logging**, reproduce it, and attach the log
  from the Download button there; passwords and keys are removed from it.
- **Is a feature meant to be solid yet?** [Feature maturity](maturity.md) rates
  each part of the app.
- **A security problem?** Please report it privately, as described in the
  [security policy](https://github.com/Syracuse3DPrintingOrg/PantryRaider/blob/main/SECURITY.md),
  not in a public issue.
- **A Forager account or billing question?** Email
  [support@pantryraider.app](mailto:support@pantryraider.app).
