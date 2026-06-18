# FoodAssistant

[![CI](https://github.com/Syracuse3DPrinting/FoodAssistant/actions/workflows/ci.yml/badge.svg)](https://github.com/Syracuse3DPrinting/FoodAssistant/actions/workflows/ci.yml)

A self-hosted food tracker that helps you manage what's in your fridge, reduce waste, and plan meals. Built to run entirely on your own hardware with no cloud dependency required.

Licensed under [PolyForm Noncommercial 1.0](LICENSE) - free for personal, educational, and non-commercial use.

---

![Inventory dashboard showing four storage panels with drag-and-drop](docs/screenshots/inventory.png)

*Inventory dashboard — four storage panels (Refrigerated, Frozen, Room Temp, Pantry) with drag-and-drop moves, inline edits, and expiry badges.*

---

## Why FoodAssistant?

[Grocy](https://grocy.info/) is an excellent, battle-tested self-hosted grocery and inventory manager. It handles product storage, stock levels, expiry tracking, and more. FoodAssistant uses Grocy as its inventory backbone.

What FoodAssistant adds on top:

- **AI-powered photo import** -- photograph a pile of groceries and get them all queued for review at once, without typing anything
- **Barcode scanning with LLM enrichment** -- scan barcodes via camera, USB scanner, or manual entry; [Open Food Facts](https://world.openfoodfacts.org) provides product data, and an optional LLM pass cleans up messy names and fills in gaps
- **Stream Deck kiosk** -- a dedicated kitchen control surface with large buttons for the most common actions, auto-rotation support, and configurable text size; no phone required
- **Home Assistant integration** -- REST sensors, barcode scanner automations via keyboard_remote, and a Lovelace inventory dashboard
- **Recipe suggestions from what you have** -- ranks your Mealie recipe library by how much of each recipe is already in stock; items expiring soon float to the top

We stand on the shoulders of giants. See [About & Credits](/ui/about) in the app for the full list.

All AI features are optional. You can run FoodAssistant without any AI provider configured; photo analysis and barcode enrichment will not work, but everything else does.

## Features

- **Inventory dashboard** — panels for Refrigerated, Frozen, Room Temp, Pantry (plus custom storage locations you define), with drag-and-drop moves, inline edits, and sorting
- **Photo analysis** — photograph a food item and a vision model extracts name, brand, quantity, and any printed best-by date
- **Receipt import** — photograph a grocery receipt and every food line item is extracted and queued for review
- **Barcode lookup** — scan barcodes via camera, a USB/wireless scanner, or manual entry; backed by Open Food Facts with optional AI cleanup for messy product names
- **Expiry defaults** — an editable rules table fills in best-by dates automatically based on product type; all values are overridable before import
- **Recipe suggestions** — "What Can I Cook?" ranks your recipes by how much of them you already have in stock; items expiring soon float to the top
- **Recipe import** — import from any webpage, photograph a recipe card or handwritten note, browse TheMealDB, or have the AI write a recipe from scratch
- **Meal planning and shopping lists** — optional [Mealie](https://mealie.io) integration with a week view, shopping list with check-off, and inventory-aware recipe suggestions
- **Custom storage locations** — add buckets beyond the four built-ins (Wine Cellar, Garage Fridge, etc.) from the setup wizard
- **Home Assistant integration** — REST sensors, notification automations, and a Lovelace dashboard with inventory panels
- **Stream Deck kiosk** — kitchen control surface with large-text buttons, auto-rotation, and configurable layout
- **UI scale setting** — adjustable zoom for small screens or kitchen monitors
- **Web setup wizard** — configure everything at `/setup` with live connection tests; no config file editing required
- **Two-factor authentication** — optional TOTP (app-based 2FA) on top of password login; works offline with any authenticator app
- **Localhost auth bypass** — kiosk installs on the local machine can skip the login screen entirely

## Screenshots

| | |
|---|---|
| ![Inventory](docs/screenshots/inventory.png) | ![Add item / barcode scan](docs/screenshots/add.png) |
| **Inventory** — stock grouped by storage, drag-to-move | **Add item** — barcode scan, photo analysis, manual entry |
| ![Recipe suggestions](docs/screenshots/cook.png) | ![Meal plan](docs/screenshots/mealplan.png) |
| **Cook** — recipes ranked by what's in stock | **Meal plan** — week view with Mealie integration |
| ![Setup wizard](docs/screenshots/setup.png) | ![Expiring items](docs/screenshots/expiring.png) |
| **Setup wizard** — configure providers and auth | **Expiring** — urgency-sorted view with HA sensor data |

## How AI works in this app

All AI features are optional. You can run FoodAssistant without any AI provider configured, though photo analysis and barcode enrichment will not work.

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

Needs [Docker](https://docs.docker.com/get-docker/) with Compose v2. One command pulls the prebuilt image and starts FoodAssistant plus a bundled Grocy:

```bash
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrinting/FoodAssistant/main/scripts/install.sh | bash
```

Then open **http://YOUR-HOST:9284/setup** and follow the wizard: set a UI password (required by default), add your Grocy and AI provider keys, test, save.

Prefer to do it by hand? Grab [`docker-compose.prod.yml`](docker-compose.prod.yml), save it as `docker-compose.yml`, and run `docker compose up -d`.

**Bundled extras** are opt-in via profiles - add any you want to the `up` command:

| Profile | Starts | Notes |
|---|---|---|
| `with-grocy` | Grocy at `:9383` | Inventory backend (started by default in the install script) |
| `with-mealie` | Mealie at `:9285` | Recipes, meal plan, shopping list |
| `with-ollama` | Ollama at `:11434` | Fully local AI - then `docker exec foodassistant-ollama ollama pull llava:7b` |

```bash
docker compose --profile with-grocy --profile with-mealie --profile with-ollama up -d
```

For each, create an API key/token in that service and paste it into the setup wizard.

### Option 2 - Home Assistant add-on (HA OS / Supervised)

Runs inside Home Assistant with the UI in the sidebar and no separate login - HA handles auth through Ingress.

1. **Settings > Add-ons > Add-on Store**, open the three-dot menu, choose **Repositories**, and add `https://github.com/Syracuse3DPrinting/FoodAssistant`.
2. Install **FoodAssistant** and start it, then click **Open Web UI**.

Install the community **Grocy** add-on first and point FoodAssistant at it in the wizard. Full details, including low-power AI options: [add-on docs](homeassistant/addon/foodassistant/DOCS.md).

### Timezone

Set `TZ` in `.env` (e.g. `TZ=Europe/London`); defaults to `America/New_York`.

## Configuration

The web setup wizard at `/setup` is the recommended way to configure the app. Settings are saved to `service/data/settings.json` and persist across container restarts.

To pin values via environment variables (useful for scripted installs):

```bash
cp .env.example .env
# edit .env and set any values you want to override
```

## Offline / Air-Gapped Use

FoodAssistant can run entirely offline if you use Ollama. With Ollama configured:

- Photo analysis and receipt import work locally
- Barcode lookup still contacts Open Food Facts by default; set `BARCODE_ENRICHMENT=off` to disable this (items will need manual names)
- Recipe suggestions from TheMealDB are disabled if you set `RECIPE_SOURCE=off` in settings
- Grocy and Mealie run as local containers with no external calls

Startup is fully self-contained - no internet access is required to start or restart the app.

## Backup

Download a zip of FoodAssistant's data at **Settings > Security > Download Backup**. API keys and passwords are stripped from the backup by default so it is safe to store off-box; tick "Include API keys & passwords" for a restore-complete copy you keep somewhere trusted.

For a full backup including Grocy and Mealie data, run on the host:

```bash
./scripts/backup.sh /path/to/backup-destination
```

For automated cloud backup, configure an [rclone](https://rclone.org) remote in **Settings > Security**. Rclone supports S3, Backblaze B2, SFTP, Google Drive, Dropbox, and 40+ other backends.

## Home Assistant

**Running Home Assistant OS or Supervised?** Install FoodAssistant as an add-on so it lives in the HA sidebar with no separate login - HA authenticates the UI through Ingress. In HA go to **Settings > Add-ons > Add-on Store**, open the menu, choose Repositories, and add `https://github.com/Syracuse3DPrinting/FoodAssistant`, then install FoodAssistant. Full instructions: [homeassistant/addon/foodassistant/DOCS.md](homeassistant/addon/foodassistant/DOCS.md).

For a **standalone** install, see [homeassistant/README.md](homeassistant/README.md) for REST sensors, automations, and the Lovelace dashboard.

## Updating

**Docker (prebuilt image):** pull the latest image and recreate the container. Your data and settings persist in the `./data` volume.

```bash
docker compose pull
docker compose up -d
```

Pin a specific version instead of latest by setting `FOODASSISTANT_TAG=v1.3.1` in `.env`.

**Home Assistant add-on:** update from the add-on page in Home Assistant when a new version is offered.

**Built from source (development):** the dev `docker-compose.yml` mounts the code and runs with `--reload`, so a `git pull` applies changes live. Rebuild only when `requirements.txt` or the Dockerfile changes:

```bash
git pull
docker compose up -d --build service
```

### Upgrading pinned images

The bundled backends (Grocy, Mealie, Ollama) are pinned to specific versions in the compose files rather than `:latest`, so an unattended `docker compose pull` can't silently move you onto a breaking release. Current pins:

| Service | Image | Tag |
|---------|-------|-----|
| Grocy   | `lscr.io/linuxserver/grocy` | `4.6.0` |
| Mealie  | `ghcr.io/mealie-recipes/mealie` | `v3.19.2` |
| Ollama  | `ollama/ollama` | `0.30.8` |

To move a backend to a newer version, **back up first** (`./scripts/backup.sh` plus the relevant `./grocy` / `./mealie` data dir), then bump the tag in `docker-compose.yml` (or `docker-compose.prod.yml`) and recreate just that service:

```bash
docker compose up -d grocy   # or mealie / ollama
```

Check each project's release notes before a major bump - Mealie in particular has had breaking schema migrations between major versions. FoodAssistant's own image is versioned separately via `FOODASSISTANT_TAG` (see above).

## API

See [docs/api.md](docs/api.md) for endpoint reference. Interactive docs are at `/docs` when the app is running.

## Changelog

Release notes are in [CHANGELOG.md](CHANGELOG.md).

## License

[PolyForm Noncommercial 1.0](LICENSE) - free for personal, hobby, educational, and non-commercial use. Contact for commercial licensing.
