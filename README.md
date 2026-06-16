# FoodAssistant

A self-hosted food tracker that helps you manage what's in your fridge, reduce waste, and plan meals. Built to run entirely on your own hardware with no cloud dependency required.

Uses [Grocy](https://grocy.info/) as the inventory backend. All AI features are optional and can run fully locally using [Ollama](https://ollama.com/).

Licensed under [PolyForm Noncommercial 1.0](LICENSE) - free for personal, educational, and non-commercial use.

## Features

- **Inventory dashboard** - four-panel view (Refrigerated, Frozen, Room Temp, Pantry) with drag-and-drop moves, inline edits, and sorting
- **Photo analysis** - photograph a food item and a vision model extracts name, brand, quantity, and any printed best-by date
- **Receipt import** - photograph a grocery receipt and every food line item is extracted and queued for review
- **Barcode lookup** - scan barcodes via camera, a USB/wireless scanner, or manual entry; backed by Open Food Facts with optional AI cleanup for messy product names
- **Expiry defaults** - an editable rules table fills in best-by dates automatically based on product type; all values are overridable before import
- **Recipe suggestions** - "What Can I Cook?" ranks your recipes by how much of them you already have in stock; items expiring soon float to the top
- **Recipe import** - import from any webpage, photograph a recipe card or handwritten note, browse TheMealDB, or have the AI write a recipe from scratch
- **Meal planning and shopping lists** - optional [Mealie](https://mealie.io) integration with a week view, shopping list with check-off, and inventory-aware recipe suggestions
- **Home Assistant integration** - REST sensors, notification automations, and a Lovelace dashboard with inventory panels
- **Web setup wizard** - configure everything at `/setup` with live connection tests; no config file editing required
- **Two-factor authentication** - optional TOTP (app-based 2FA) on top of password login; works offline with any authenticator app

## How AI works in this app

All AI features are optional. You can run FoodAssistant without any AI provider configured, though photo analysis and barcode enrichment will not work.

When AI is enabled you have four choices:

| Provider | Setup | Runs locally |
|---|---|---|
| [Ollama](https://ollama.com/) | Pull a vision model (e.g. `llava:7b`) | Yes, fully local |
| [Gemini](https://aistudio.google.com/) | Free API key from Google AI Studio | No |
| [OpenAI](https://platform.openai.com/) | API key, usage billed per token | No |
| [Anthropic](https://console.anthropic.com/) | API key, usage billed per token | No |

For a fully local setup with no external dependencies, use Ollama for both vision and text. Photo analysis quality is lower than cloud models but functional for most food items.

## Quick Start

**Prerequisites:** [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) on any Linux, macOS, or Windows machine (including Proxmox LXC, TrueNAS SCALE, and Unraid). Running Home Assistant OS instead of a general-purpose server? See [Home Assistant add-on](#home-assistant) below for a one-click install with no separate login.

### Fastest - prebuilt image, no build step

```bash
curl -fsSL https://raw.githubusercontent.com/Syracuse3DPrinting/FoodAssistant/main/scripts/install.sh | bash
```

This pulls the published image, writes a `docker-compose.yml`, and starts FoodAssistant plus a bundled Grocy. Then open **http://YOUR-HOST:9284/setup**. Prefer to do it by hand? Download [`docker-compose.prod.yml`](docker-compose.prod.yml), rename it to `docker-compose.yml`, and run `docker compose up -d`.

### Option A - FoodAssistant only (you already have Grocy)

```bash
git clone https://github.com/Syracuse3DPrinting/FoodAssistant.git
cd FoodAssistant
docker compose up -d --build
```

Open **http://YOUR-HOST:9284/setup**, set a UI password (required by default), enter your Grocy URL and API key plus an AI provider key, test the connections, then save.

### Option B - FoodAssistant with Grocy included

```bash
docker compose --profile with-grocy up -d --build
```

Grocy starts at **http://YOUR-HOST:9383**. Open it, set a password, generate an API key under Profile > Manage API Keys, and paste it into the FoodAssistant setup wizard.

### Option C - Fully local with Ollama

```bash
docker compose --profile with-ollama up -d --build
docker exec foodassistant-ollama ollama pull llava:7b
```

In the setup wizard choose Ollama as the provider and set the URL to `http://ollama:11434`. No external AI calls are made.

### Option D - With Mealie for recipes and meal planning

```bash
docker compose --profile with-mealie up -d --build
```

Mealie starts at **http://YOUR-HOST:9285**. Default login: `changeme@example.com` / `MyPassword` - change it on first login. Create an API token under User Profile > API Tokens and paste it into the FoodAssistant setup wizard.

You can combine profiles:

```bash
docker compose --profile with-grocy --profile with-ollama --profile with-mealie up -d
```

### Timezone

Set your timezone in `.env` (copy from `.env.example`):

```
TZ=America/Chicago
```

Common values: `Europe/London`, `Asia/Tokyo`, `Australia/Sydney`. Defaults to `America/New_York` if not set.

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

Download a zip of FoodAssistant's data at Settings > Security > Download Backup. API keys and passwords are stripped from the backup by default so it is safe to store off-box; tick "Include API keys & passwords" for a restore-complete copy you keep somewhere trusted.

For a full backup including Grocy and Mealie data, run on the host:

```bash
./scripts/backup.sh /path/to/backup-destination
```

For automated cloud backup, configure an [rclone](https://rclone.org) remote in Settings > Security. Rclone supports S3, Backblaze B2, SFTP, Google Drive, Dropbox, and 40+ other backends.

## Home Assistant

See [homeassistant/README.md](homeassistant/README.md) for sensors, automations, and the Lovelace dashboard.

## Updating

App code is volume-mounted with `--reload` so changes apply after a `git pull` without restarting the container. A rebuild is only needed when `requirements.txt` or the Dockerfile changes:

```bash
git pull
docker compose up -d --build service
```

## API Endpoints

| Endpoint | Purpose |
|---|---|
| `/setup` | Web setup wizard |
| `/ui/` | Inventory dashboard |
| `/ui/expiring` | Expiring items view |
| `/ui/add` | Add food (barcode, photo, manual) |
| `/ui/defaults` | Expiry defaults editor |
| `/ui/cook` | Recipe suggestions ranked by inventory |
| `/ui/recipes` | Browse and import recipes |
| `/ui/mealplan` | Week meal plan (Mealie) |
| `/ui/shopping` | Shopping list (Mealie) |
| `GET /admin/backup` | Download data as zip |
| `GET /expiring/summary` | Urgency counts for HA sensors |
| `GET /inventory/dashboard` | Full stock grouped by storage |
| `GET /health` | Connectivity status |

Full interactive API docs at `/docs`.

## License

[PolyForm Noncommercial 1.0](LICENSE) - free for personal, hobby, educational, and non-commercial use. Contact for commercial licensing.
