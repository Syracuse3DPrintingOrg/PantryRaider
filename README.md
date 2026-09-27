# Pantry Raider

> [!IMPORTANT]
> **Public beta (0.19).** The kitchen side has been in daily use for months.
> The parts that face other people, the Forager companion, remote access, and
> the Bandit Cub displays, are younger, so this goes out as a beta because that
> is what it is. Expect rough edges and occasional breaking changes; back up
> your data before updating. Upgrading never resets a kitchen that already
> works. Bug reports are very welcome in the issues tab, and they are the point
> of a beta.

Your data stays on your hardware; the few optional features that reach the
internet are spelled out in the [privacy policy](https://docs.pantryraider.app/privacy/).

[![CI](https://github.com/Syracuse3DPrintingOrg/PantryRaider/actions/workflows/ci.yml/badge.svg)](https://github.com/Syracuse3DPrintingOrg/PantryRaider/actions/workflows/ci.yml)

A self-hosted food tracker that helps you manage what's in your fridge, reduce waste, and plan meals. Built to run entirely on your own hardware with no cloud dependency required.

Licensed under [PolyForm Noncommercial 1.0](LICENSE) - free for personal, educational, and non-commercial use.

> **Arrived here from HACS?** This repository also ships the Pantry Raider
> **Home Assistant integration**. The integration is a companion, not the app:
> it needs a running Pantry Raider install to talk to. Start with the
> [installation guide](https://docs.pantryraider.app/first-run/) (a Raspberry
> Pi appliance or any Docker host), then add the integration and pair it from
> your kitchen screen; the
> [integration guide](https://docs.pantryraider.app/home-assistant-integration/)
> covers the rest.

---

![Inventory dashboard showing four storage panels with drag-and-drop](docs/screenshots/inventory.png)

*Inventory dashboard: four storage panels (Refrigerated, Frozen, Room Temp, Pantry) with drag-and-drop moves, inline edits, and expiry badges.*

---

## Why Pantry Raider?

[Grocy](https://grocy.info/) is an excellent, battle-tested self-hosted grocery and inventory manager. It handles product storage, stock levels, expiry tracking, and more. Pantry Raider uses Grocy as its inventory backbone.

What Pantry Raider adds on top:

- **AI-powered photo import**: photograph a pile of groceries and get them all queued for review at once, without typing anything
- **Barcode scanning with LLM enrichment**: scan barcodes via camera, USB scanner, or manual entry; [Open Food Facts](https://world.openfoodfacts.org) provides product data, and an optional LLM pass cleans up messy names and fills in gaps
- **Stream Deck kiosk**: a dedicated kitchen control surface with large buttons for the most common actions, auto-rotation support, and configurable text size; no phone required
- **Home Assistant integration**: a native HACS integration that pairs from the kitchen screen and exposes your food counts, timers, thermometer readings, printer status, and screen controls as entities, with notify entities and camera pop-up services; the older REST sensors and Lovelace dashboard still work as a manual alternative
- **Bandit Cubs**: small ESP32 kitchen displays you flash from your browser; they listen to your kitchen's Bluetooth broadcast to show expiring items, running timers, and probe temperatures, and you update one by flashing it again
- **Fridge, freezer, and room sensors**: Bluetooth temperature and humidity sensors, plus door sensors and stick-anywhere shelf buttons, with on-screen alarms when a fridge drifts warm or a door is left open
- **Recipe suggestions from what you have**: ranks your recipe library by how much of each recipe is already in stock; items expiring soon float to the top, and a step-by-step Cook wizard walks you to tonight's dish
- **Label and document printing**: print food and spice labels with a drag-and-drop label designer, or send a recipe to a regular printer
- **Bluetooth kitchen thermometers**: read meat and probe thermometers locally, with live temperatures and target alerts on the Timers page

**Try it:** there are two ways to look around before you install anything. [demo.pantryraider.app](https://demo.pantryraider.app) is a real, read-only Pantry Raider running in demo mode, so you can click through the app itself without setting anything up. There's also a self-contained [interactive demo](docs/demo/index.html) (no backend, runs entirely in your browser) that walks through the inventory, scanning, recipe suggestions, cameras, the unit converter, and the Stream Deck; it's deployed to Cloudflare and redeploys on every push (see [`docs/demo/README.md`](docs/demo/README.md)), and you can open `docs/demo/index.html` locally too.

We stand on the shoulders of giants. The full list is on the About & Credits page inside the app, under the Settings menu.

All AI features are optional. You can run Pantry Raider without any AI provider configured; photo analysis and barcode enrichment will not work, but everything else does.

## Features

- **Inventory dashboard**: panels for Refrigerated, Frozen, Room Temp, Pantry (plus custom storage locations you define), with drag-and-drop moves, inline edits, and sorting
- **Photo analysis**: photograph a food item and a vision model extracts name, brand, quantity, and any printed best-by date
- **Receipt import**: photograph a grocery receipt and every food line item is extracted and queued for review
- **Receipt prices**: photograph a receipt from the Shopping page and the prices you actually paid are matched to your pantry items and recorded on each product's newest stock entry in Grocy, so price history reflects real prices; you review the pairings before anything is written, and the photo is read in the background so you can leave the page
- **Barcode lookup**: scan barcodes via camera, a USB/wireless scanner, or manual entry; backed by Open Food Facts with optional AI cleanup for messy product names
- **Expiry defaults**: an editable rules table fills in best-by dates automatically based on product type; all values are overridable before import
- **Expiring list and waste tracking**: an urgency-sorted Expiring page with a sniff test that keeps still-good food 1, 3, or 5 more days (never offered on hard expiration dates), a toss button that records spoiled food as waste rather than eaten, a Waste summary of your most-tossed items, and a one-tap Opened button on the Inventory dashboard that switches an item to its after-opening shelf life
- **Recipe suggestions**: "What Can I Cook?" ranks your recipes by how much of them you already have in stock; items expiring soon float to the top
- **Recipe import**: import from any webpage, photograph a recipe card or handwritten note, load a recipe file (generic recipe JSON, a schema.org Recipe JSON-LD file, or a Mealie export), browse TheMealDB, or have the AI write a recipe from scratch
- **On the Line (Current Recipe)**: set any recipe (from your library, an import, or AI-generated) as the active one with a "Cook" button, and the app holds it server-side with servings scaling and step-by-step instructions; step durations like "simmer 20 minutes" become ready-to-start named timers shared across surfaces, surfaced in a floating timer window and on the Stream Deck's timer keys
- **Manage**: one page for the scanner with four mode tiles (Stock up, Use stock, Shopping list, Audit stock) that are the shared scanner mode itself, so picking a mode switches every scanner and Stream Deck mode key at once; consuming by barcode links unrecorded barcodes to their product on the fly, and an Open on phone button shows a QR code that jumps the page to your phone's camera and keyboard
- **Shared kitchen timers**: a Timers page (under Time & Temp) with one-tap presets and named custom timers; timers live on the server, so the page, the floating timer window, the Stream Deck keys, and every satellite screen show the same countdowns, with +1 min and Clear all buttons
- **Label and document printing**: print a food label (name, added date, best-by date, with an honest "est." or "AI" chip on estimated dates, and a scan-to-use-up code that takes that exact container off your stock when scanned) for any item or a whole batch, decorative spice labels, or a recipe to a document printer; a drag-and-drop label designer lays out your own label with fields and a QR code on a to-scale preview of rectangular, square, or round stock, printers are added from Settings (network, IPP, USB, and Zebra ZPL), and turning printing on shares each device's printers across the LAN with a server-set fleet default
- **Bluetooth kitchen thermometers**: read BLE meat and probe thermometers (Inkbird, ThermoPro, Combustion, ThermoWorks BlueDOT) locally with no cloud, either through a Bluetooth reader on the device or through Home Assistant; live probe temperatures and battery show on the Timers page with a per-probe target that pops an on-screen alert when reached
- **Kiosk screensaver**: a bouncing-logo, retro (flying toasters or starfield), or photo-slideshow screensaver with running timers floating along as countdown pills; the slideshow can draw from a USB drive, a device folder, an Immich album, or a list of image links, and a switch runs it in every browser viewing the install; an attached Stream Deck shows the raccoon logo across its keys while the display sleeps
- **On-screen keyboard**: in kiosk mode a touch keyboard slides up whenever a text field is tapped, so a wall-mounted panel needs no physical keyboard
- **Pantry audit**: a stock count where scanning never changes your stock, reached from the Manage page. Count the whole pantry (the default) or lock the count to one storage location; each scan is compared against Grocy's recorded stock so missing and unexpected items stand out. When a count turns up differences, an opt-in Apply corrections button sets each counted item's stock to the amount you actually scanned, leaving unscanned items untouched
- **Nutrition tracker**: log what you eat with calories and macros (protein, carbs, fat) on a Nutrition page under Kitchen Guide, with daily and recent-day totals; an optional AI estimate fills in macros from a food name when a provider is configured, and using an item up by scan offers a one-tap Log as eaten when the product's nutrition facts are on record
- **Weather page**: a full forecast page on the kiosk display, opened by a Stream Deck weather or forecast key and reachable from the nav. Forecasts come from Open-Meteo (free, no key) with wttr.in as a fallback, using the same location and units as the Stream Deck weather widget
- **Unit converter and kitchen guide**: a Convert page with a measurement cheat sheet, a calculator, and your own saved conversions, plus a Kitchen Guide reference page, grouped together under Kitchen Guide
- **Meal planning and shopping lists**: built in, with a week view, a shopping list with check-off kept in Grocy next to your inventory, and inventory-aware recipe suggestions; an existing [Mealie](https://mealie.io) can be connected and copied over
- **Custom storage locations**: add buckets beyond the four built-ins (Wine Cellar, Garage Fridge, etc.) from the setup wizard
- **Two-level navigation, fully customizable**: a short top menu of areas (Glance, Inventory, Manage, Review, Cook, Shopping, Kitchen Guide, Time & Temp, Home Hub) with each area's sub-pages as full-width tabs in the header; reorder or hide pages, add your own entries (label, icon, and a local or external URL), and regroup pages under different parents, all from Settings > Personalization > Appearance; navigation layout is per-device so each kiosk can arrange its own menu
- **Glance home screen**: the app opens on an at-a-glance home that builds itself from your navigation, big buttons to your main pages plus live-count pills for items to review, on-screen alerts, and food expiring this week; prefer a hand-arranged launcher? Switch the home style to the custom Start Page grid in Settings
- **Camera feeds**: configure network cameras (from Home Assistant, by IP with brand templates, or by hand) and view them on an on-screen Camera page; a connected Stream Deck can show a camera snapshot on a key or splash it across the whole deck
- **Home Assistant integration**: a native HACS integration that discovers your install, pairs from the kitchen screen, and exposes food counts, timers, thermometer probes, printer status, and screen controls as entities, with notify entities and camera pop-up services for on-screen messages; the original REST sensors, notification automations, and Lovelace dashboard remain as a manual alternative, and Stream Deck keys can toggle HA entities, run media_player transport controls, and discover cameras (the HA URL/token are stored once on the main server and shared with satellites)
- **Stream Deck kiosk**: kitchen control surface with large-text buttons, auto-rotation, and a drag-and-drop key editor; build your own custom keys (HA actions, timers, weather, cameras, media, macros) in a library and drop them onto the grid; a scan-mode key flips the barcode scanner between adding to inventory, consuming stock, adding to the shopping list, and running a read-only pantry audit
- **UI scale setting**: adjustable zoom for small screens or kitchen monitors
- **Themes**: built-in themes including Solarized, Midnight, and Forest, plus a custom theme builder to pick your own palette in Settings > Personalization > Appearance; Stream Deck key colours follow the theme with readable label contrast
- **Small-screen kiosk**: on a phone or narrow panel the top-level menu tucks into the menu button, which lists every area with larger touch targets and a single-column layout; on a Pi with a display attached, kiosk mode auto-enables
- **Web setup wizard**: configure everything at `/setup` with live connection tests; no config file editing required
- **Two-factor authentication**: optional TOTP (app-based 2FA) on top of password login; works offline with any authenticator app
- **Localhost auth bypass**: kiosk installs on the local machine can skip the login screen entirely

## Screenshots

| | |
|---|---|
| ![Inventory](docs/screenshots/inventory.png) | ![Add item / barcode scan](docs/screenshots/add.png) |
| **Inventory**: stock grouped by storage, drag-to-move | **Manage**: barcode scan, photo analysis, manual entry |
| ![Recipe suggestions](docs/screenshots/cook.png) | ![Meal plan](docs/screenshots/mealplan.png) |
| **Cook**: recipes ranked by what's in stock | **Meal plan**: plan the week from your library |
| ![Settings](docs/screenshots/setup.png) | ![Expiring items](docs/screenshots/expiring.png) |
| **Settings**: AI provider, integrations, and auth | **Expiring**: urgency-sorted view with HA sensor data |

## How AI works in this app

All AI features are optional. You can run Pantry Raider without any AI provider configured, though photo analysis and barcode enrichment will not work.

When AI is enabled you have four choices:

| Provider | Setup | Runs locally |
|---|---|---|
| [Ollama](https://ollama.com/) | Pull a vision model (e.g. `llava:7b`) | Yes, fully local |
| [Gemini](https://aistudio.google.com/) | Free API key from Google AI Studio | No |
| [OpenAI](https://platform.openai.com/) | API key, usage billed per token | No |
| [Anthropic](https://console.anthropic.com/) | API key, usage billed per token | No |

The default cloud model is Gemini 2.5 Flash, which is fast and has a generous free tier. For a fully local setup with no external dependencies, use Ollama for both vision and text. Photo analysis quality is lower than cloud models but functional for most food items.

## Install

Pick the path that matches where you're running it.

### Option 1 - Docker (server, NAS, Proxmox, TrueNAS, Unraid)

Needs [Docker](https://docs.docker.com/get-docker/) 24 or newer with Compose v2 (check with `docker version` and `docker compose version`). One command pulls the prebuilt image and starts Pantry Raider plus a bundled Grocy:

```bash
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/scripts/install.sh | bash
```

Then open **http://YOUR-HOST:9284/setup** and follow the wizard: set a UI password (required by default), add an AI provider key if you want photo and receipt scanning, test, save. You don't need a Grocy key. The bundled Grocy is set up for you on first run: Pantry Raider signs itself in, creates its own key, and secures the Grocy admin account.

Prefer to do it by hand? In an empty folder, download the compose file and the small Grocy start-up script it uses, then start the stack:

```bash
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/docker-compose.prod.yml -o docker-compose.yml
mkdir -p docker/grocy-init
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/docker/grocy-init/10-repair-auth-class.sh -o docker/grocy-init/10-repair-auth-class.sh
chmod 755 docker/grocy-init/10-repair-auth-class.sh
docker compose --profile with-grocy up -d
```

The start-up script must be executable (`chmod 755`), or Grocy skips it. The `with-grocy` profile is what starts the bundled inventory backend; leave it off only if you already run Grocy elsewhere.

**Bundled extras** are opt-in via profiles - add any you want to the `up` command:

| Profile | Starts | Notes |
|---|---|---|
| `with-grocy` | Grocy at `:9383` | Inventory backend (started by default in the install script) |
| `with-mealie` | Mealie at `:9285` | Optional, for people who already use Mealie; recipes, meal plan, and shopping are built in |
| `with-ollama` | Ollama at `:11434`, on this machine only | Fully local AI: then `docker exec foodassistant-ollama ollama pull llava:7b`. Other computers cannot reach it unless you publish the port (see [platforms](docs/platforms.md#pinned-backend-versions-and-ports)) |

```bash
docker compose --profile with-grocy --profile with-mealie --profile with-ollama up -d
```

For each, create an API key/token in that service and paste it into the setup wizard.

### Option 2 - Home Assistant add-on (HA OS / Supervised)

Runs inside Home Assistant with the UI in the sidebar and no separate login - HA handles auth through Ingress.

1. **Settings > Add-ons > Add-on Store**, open the three-dot menu, choose **Repositories**, and add `https://github.com/Syracuse3DPrintingOrg/PantryRaider`.
2. Install **Pantry Raider** and start it, then click **Open Web UI**.

Install the community **Grocy** add-on first and point Pantry Raider at it in the wizard. Full details, including low-power AI options: [add-on docs](homeassistant/addon/foodassistant/DOCS.md).

### Option 3 - Raspberry Pi appliance

Turn a Raspberry Pi into a dedicated Pantry Raider appliance (optionally with a kiosk display and a Stream Deck). Flash a stock Raspberry Pi OS Lite card with Raspberry Pi Imager (set wifi, hostname, and SSH there), boot, SSH in, and run:

```bash
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/install.sh | bash
```

The installer detects the board, any attached display, and any Stream Deck, then asks for the deployment mode (full **Pi Hosted** stack, or a thin **Pi Remote** that only drives a kiosk/Stream Deck for a server elsewhere) and which add-ons to enable. Nothing to edit on your PC. Full walkthrough: [docs/hardware/sd-image.md](docs/hardware/sd-image.md).

### Language, units, and timezone

The interface is English only for now. Temperatures, distances, and other
measurements default to US units, and the app starts on US Eastern time.

You can change the units and the timezone from Settings once the app is up.
For a scripted install, set `TZ` in `.env` (e.g. `TZ=Europe/London`); it
defaults to `America/New_York`. A Raspberry Pi appliance picks up the
timezone from the device itself during setup.

## Configuration

The web setup wizard at `/setup` is the recommended way to configure the app. Settings are saved to `service/data/settings.json` and persist across container restarts.

To pin values via environment variables (useful for scripted installs):

```bash
cp .env.example .env
# edit .env and set any values you want to override
```

## Offline / Air-Gapped Use

Pantry Raider can run entirely offline if you use Ollama. With Ollama configured:

- Photo analysis and receipt import work locally
- Scanning a barcode always asks Open Food Facts for the product name, since a barcode on its own tells you nothing; type item names in by hand to stay fully offline. Setting `BARCODE_ENRICHMENT=off` skips only the extra AI clean-up pass, not the lookup
- Recipe suggestions from TheMealDB are disabled if you set `RECIPE_SOURCE=off` in settings
- Grocy and Mealie run as local containers with no external calls

Startup is fully self-contained - no internet access is required to start or restart the app.

## Backup

Download a zip of Pantry Raider's data at **Settings > Backups & Updates > Download Backup**. API keys and passwords are stripped from the backup by default so it is safe to store off-box; tick "Include API keys & passwords" for a restore-complete copy you keep somewhere trusted.

To restore that backup, use **Settings > Backups & Updates > Restore** to rebuild the app's data (settings, database, staples) from a backup zip. Your current data is copied aside first, and a redacted backup keeps your existing API keys in place.

For a full backup including Grocy and Mealie data, run on the host:

```bash
./scripts/backup.sh /path/to/backup-destination
```

On a Pi appliance, a full Grocy and Mealie snapshot restore runs via the host bridge from a device path or an rclone remote (this is separate from the in-app app-data restore above).

For automated cloud backup, configure an [rclone](https://rclone.org) remote in **Settings > Backups & Updates**. Rclone supports S3, Backblaze B2, SFTP, Google Drive, Dropbox, and 40+ other backends.

For automatic local backups with no cloud at all, plug a formatted USB flash drive into the device: **Settings > Backups & Updates** can save backups to a `pantryraider-backups` folder on it, on a schedule in hours or with a Back up now button. The newest 14 backups are kept and nothing else on the drive is touched. A Pi appliance saves a full stack snapshot, a satellite saves its device settings, and a server saves the app-data zip.

## Troubleshooting logs

For support, turn on **Settings > Advanced > Debug logging** to raise the log level and write a rotating log file under the data directory, then use the Download control to grab that log. Secret values are redacted from the download. Leave it off in normal use.

## Home Assistant

**Running Home Assistant OS or Supervised?** Install Pantry Raider as an add-on so it lives in the HA sidebar with no separate login - HA authenticates the UI through Ingress. In HA go to **Settings > Add-ons > Add-on Store**, open the menu, choose Repositories, and add `https://github.com/Syracuse3DPrintingOrg/PantryRaider`, then install Pantry Raider. Full instructions: [homeassistant/addon/foodassistant/DOCS.md](homeassistant/addon/foodassistant/DOCS.md).

For a **standalone** install, see [homeassistant/README.md](homeassistant/README.md) for REST sensors, automations, and the Lovelace dashboard.

## Updating

**Docker (prebuilt image):** pull the latest image and recreate the container. Your data and settings persist in the `./data` volume.

```bash
docker compose pull
docker compose up -d
```

Pin a specific version instead of latest by setting `PANTRYRAIDER_TAG=0.19.6` in `.env`. The older name `FOODASSISTANT_TAG` still works, so an existing `.env` keeps doing what it always did. Published tags carry no leading `v`: use the plain release number (`0.19.6`) to freeze one build, or the minor line (`0.19`) to keep picking up patch releases.

> **Going back a version is not just pulling an older image.** Settings and database changes only move forward, so an older build can find data it does not understand. If you need to return to an earlier version, restore the backup you took on that version at the same time as you pin the tag. Take a backup before every update and this stays a two-minute job.

**Automatic updates (server):** the prod compose runs Watchtower (the maintained [`ghcr.io/nicholas-fedor/watchtower`](https://github.com/nicholas-fedor/watchtower)), which checks for a new Pantry Raider image and recreates the app container when one is published. This is **on by default** so a server install stays current without intervention, and updated Python dependencies come along for free because they are baked into the image. It polls daily (override with `WATCHTOWER_POLL_INTERVAL` seconds in `.env`). Server installs need Docker 24 or newer with Compose v2.

The bundled updater only manages Pantry Raider's own app container. Grocy, Mealie, Ollama and the rest stay on their pinned versions, and if you already run your own Watchtower on the same machine, the two leave each other alone.

To turn auto-updates off, stop the one service or pin to a fixed version:

```bash
docker compose stop watchtower        # disable auto-updates
# or pin a version in .env so no newer image is ever picked up:
# PANTRYRAIDER_TAG=0.19.6
```

**One-time step for existing server installs.** Compose files downloaded before this release use the original Watchtower image, which does not work with Docker 29, so a server on Docker 29 never updates itself. Replace the compose file once to switch to the maintained updater. The new file also moves the bundled Mealie to a newer version, and Mealie upgrades its database on first start with no way back, so take a backup first. In the folder with `docker-compose.yml` (`foodassistant` if you used the one-command installer), run the commands below, giving `stop` and `up` the same `--profile` flags you started the stack with. Without them, `stop` leaves Grocy and Mealie running while their folders are copied.

```bash
docker compose --profile with-grocy stop   # use the same --profile flags you started with
sudo tar -czf pantryraider-before-update.tar.gz data grocy mealie   # leave out mealie if you do not run it
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrintingOrg/PantryRaider/main/docker-compose.prod.yml -o docker-compose.yml
docker compose --profile with-grocy up -d   # use the same --profile flags you started with
```

Your `.env` and data are kept, and everything starts again on the new file. Keep the backup until everything works on the new version. If you edited your compose file by hand, change only the watchtower `image:` line to `ghcr.io/nicholas-fedor/watchtower:1.22.3` instead. The new file also scopes the updater to Pantry Raider with a `com.centurylinklabs.watchtower.scope=foodassistant` label on the app and on the updater, plus `WATCHTOWER_SCOPE=foodassistant` on the updater; if you copy those over, add them to both containers together, or updates stop.

Watchtower needs the Docker socket, which is host-root-equivalent access; that is the tradeoff for hands-off updates on a server with no host bridge. To keep that access narrow, Watchtower sits on its own private network shared only with Pantry Raider, so nothing else in the stack (Grocy, Mealie, Ollama, CUPS, Beszel) can reach it. The daily check runs on Watchtower's own schedule and is separate from the update switches in Settings, which control the app's own update notice.

**Home Assistant add-on:** the add-on page in Home Assistant offers an update when a new add-on version is published. New add-on versions are not yet published on the same cadence as the Docker releases, so for the newest features use the Docker install above.

**Built from source (development):** the dev `docker-compose.yml` mounts the code and runs with `--reload`, so a `git pull` applies changes live. Rebuild only when `requirements.txt` or the Dockerfile changes:

```bash
git pull
docker compose up -d --build service
```

**Raspberry Pi appliance:** the Pi over-the-air update helper redeploys the Stream Deck controller alongside the app, and is safe to re-run after a manual `git pull`. Pi appliances also auto-update on their own when the global "Install updates automatically" setting is on (the default), which you can toggle under Settings. The flag is shared with any Pi Remotes connected to a server, so a server and its remotes stay on the same version.

### Upgrading pinned images

Every bundled service other than Pantry Raider itself (Grocy, Mealie, Ollama, the updater, Beszel and CUPS) is pinned to a specific version in the compose files rather than `:latest`, so an unattended `docker compose pull` can't silently move you onto a breaking release. Current pins:

| Service | Image | Tag | Notes |
|---------|-------|-----|-------|
| Grocy   | `lscr.io/linuxserver/grocy` | `4.6.0` | See the Grocy note below before bumping |
| Mealie  | `ghcr.io/mealie-recipes/mealie` | `v3.28.0` | Upgrades its database on first start |
| Ollama  | `ollama/ollama` | `0.34.4` | Answers only on this machine |
| Updater (server only) | `ghcr.io/nicholas-fedor/watchtower` | `1.22.3` | Moves when you download the current `docker-compose.prod.yml` |
| Beszel hub and agent | `henrygd/beszel`, `henrygd/beszel-agent` | `0.20.0` | Always use the same tag for both; the hub's data upgrades in place |
| CUPS    | `olbat/cupsd` | pinned by digest | The digest of a dated tag; refresh it every few months |

To move a backend to a newer version, **back up first**, then bump the tag in `docker-compose.yml` (or `docker-compose.prod.yml`) and recreate just that service:

```bash
docker compose up -d grocy   # or mealie / ollama
```

**Back up before you bump Grocy or Mealie, and keep the backup.** Both upgrade their database the first time the new version starts, and neither can go back to an older version on the same data: Mealie runs its database migrations forward only, and Grocy 4.7 migrates its database and changes the `AUTH_CLASS` line in `config.php`. So before you bump either one, copy its data folder (`./grocy`, `./mealie`) somewhere safe, or run `./scripts/backup.sh` if you work from a clone of this repository. Keep that copy until the new version has been working for a while. To return to an older tag, stop the service, put the backed-up folder back, and only then start the older version.

Check each project's release notes before a major bump; Mealie in particular has had breaking schema migrations between major versions. Pantry Raider's own image is versioned separately via `PANTRYRAIDER_TAG` (see above).

**Grocy in particular:** a Grocy release can rename a class that its saved `config.php` points at. Grocy 4.7.0 did exactly that (`AUTH_CLASS` moved to `Grocy\Middleware\Auth\DefaultAuthMiddleware`), and a `config.php` written by an older Grocy makes every API call answer with an error page, so the inventory stops loading. The compose files therefore mount a small start-up script into the Grocy container (`docker/grocy-init`) that fixes that line on its own when Grocy starts, backing up `config.php` first. The one-command installer downloads the script for you; with a by-hand install it runs only if you downloaded it and made it executable (see Option 1 above). Before bumping the Grocy pin, compare the new version's `config-dist.php` against yours, and only bump in a release that carries that script. If Grocy moved and the inventory stopped loading, see [Grocy updated and inventory stopped loading](docs/platforms.md#grocy-updated-and-inventory-stopped-loading).

### Dependencies

`service/requirements.txt` is the hand-edited list, every line an exact `==` pin. `service/requirements.lock` is its full transitive resolution with a SHA-256 for every wheel, and it is the file the Docker image installs (`pip install --require-hashes -r requirements.lock`). Two builds of the same tag therefore resolve the same versions, and a wheel that changed underneath its version number fails the build instead of shipping.

The two files have to move together. Change a pin in `requirements.txt` and regenerate the lock in the same commit with [uv](https://docs.astral.sh/uv/):

```bash
uv pip compile service/requirements.txt --universal --python-version 3.12 --generate-hashes -o service/requirements.lock
# add --upgrade to also refresh the transitive versions
```

`--universal` is not optional: the published image is built for both `linux/amd64` and `linux/arm64`, and a lock compiled for one architecture omits the environment markers the other needs. `pip-compile` from pip-tools has no equivalent, so use uv here. `tests/test_requirements_lock.py` fails when a pin is missing from the lock, when a pin is not exact, or when the Dockerfile stops installing from the lock.

Check the result on both architectures before committing it:

```bash
uv pip install --python-platform x86_64-manylinux2014 --dry-run --require-hashes -r service/requirements.lock
uv pip install --python-platform aarch64-manylinux2014 --dry-run --require-hashes -r service/requirements.lock
```

To install exactly the locked set in a venv: `uv pip sync service/requirements.lock` (or `pip install --require-hashes -r service/requirements.lock`).

## Documentation

- [docs/maturity.md](docs/maturity.md) - a feature maturity matrix: which capabilities are Stable, Beta, or Experimental, and why.
- [docs/hardware.md](docs/hardware.md) - supported boards, displays and touch panels, Stream Deck models, accelerometer, and barcode scanners.
- [docs/platforms.md](docs/platforms.md) - deployment modes (server, Pi Hosted, Pi Remote), hosting, pinned versions and ports, AI providers, and Home Assistant.
- [docs/what-needs-internet.md](docs/what-needs-internet.md) - a cloud-dependency matrix: what runs fully offline and what reaches out to the internet, and why.
- [docs/settings-matrix.md](docs/settings-matrix.md) - which settings are editable, inherited from the server, or device-local in each deployment mode.
- [docs/hardware/supported-hardware.md](docs/hardware/supported-hardware.md) - minimum specs and the board test matrix.
- [docs/hardware/sd-image.md](docs/hardware/sd-image.md) - flashing the ready-made SD-card image.
- [docs/api.md](docs/api.md) - REST endpoint reference.
- [docs/AI_DECLARATIONS.md](docs/AI_DECLARATIONS.md) - how AI tools were used to build Pantry Raider.

These pages are also wired into an MkDocs site (`mkdocs.yml`) for browsable local docs. MkDocs and its theme are dev-only tools and are not part of the runtime requirements. To preview the site:

```bash
pip install mkdocs mkdocs-material
mkdocs serve   # then open http://127.0.0.1:8000
```

## API

See [docs/api.md](docs/api.md) for endpoint reference, including the Current Recipe and timer endpoints, recipe file import, and app-data restore. Interactive docs are at `/docs` when the app is running.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow, local smoke
test, and how to run the tests. Participation is covered by our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Support the project

Pantry Raider is free for home use. If it has earned a spot on your counter,
you can [buy the developer a coffee](https://www.buymeacoffee.com/syracuse3dprinting) ☕.

## Security

Please report security vulnerabilities privately. See [SECURITY.md](SECURITY.md)
for the disclosure process; do not open a public issue for security problems.

## Changelog

Release notes are in [CHANGELOG.md](CHANGELOG.md). For where the project is
headed, see the [roadmap](ROADMAP.md).

## License

[PolyForm Noncommercial 1.0](LICENSE) - free for personal, hobby, educational, and non-commercial use. Contact for commercial licensing.

The Bandit Cub firmware under [`esphome/`](esphome/) is the exception: it is built with ESPHome and licensed under GPL-3.0-or-later. See [esphome/NOTICE.md](esphome/NOTICE.md) for what that means.
