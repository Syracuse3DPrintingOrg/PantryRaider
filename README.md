# FoodAssistant

Self-hosted food spoilage tracker with LLM-powered photo/receipt import,
barcode lookup, and Home Assistant integration. Uses
[Grocy](https://grocy.info/) as the inventory backend.

## Features

- **Photo analysis** — snap a food item; a vision LLM (Gemini now, Ollama-ready)
  extracts name, brand, quantity, and any printed best-by date
- **Receipt import** — photograph a grocery receipt; every food item is
  extracted and queued for import
- **Barcode lookup** — camera scanner or manual entry, backed by Open Food Facts
- **Expiry defaults** — editable rules table ("fresh chicken: 5 days,
  frozen: 365") fills in best-by dates automatically; everything is
  overridable before import
- **Web UI** — expiring-items dashboard with one-click consume, add-food page,
  defaults editor (`/ui/`)
- **Home Assistant** — REST sensors, notification automations, Lovelace dashboard
- **Auth** — optional password login for the UI + API key for headless clients

## Architecture

```
Browser/Phone ──► FoodAssistant service (FastAPI, :9284)
                    ├─► Gemini or Ollama (vision LLM)
                    ├─► Open Food Facts (barcode lookup)
                    └─► Grocy (:9383) — inventory, stock, consumption log
Home Assistant ◄── REST sensors ◄── /expiring endpoints
```

## Setup

1. **Grocy** — run your own instance (e.g. Unraid app / linuxserver image) and
   generate an API key: user icon → Manage API Keys.

2. **Configure:**
   ```bash
   cp .env.example .env
   # fill in GEMINI_API_KEY, GROCY_BASE_URL, GROCY_API_KEY
   # recommended: set AUTH_PASSWORD and API_KEY (openssl rand -hex 24)
   ```

3. **Run:**
   ```bash
   docker compose up -d --build
   curl http://localhost:9284/health
   # {"status":"ok","vision_provider":"ok","grocy":"ok"}
   ```

4. **Home Assistant** — see [homeassistant/README.md](homeassistant/README.md)
   for sensors, automations, and the Lovelace dashboard.

## Development notes

- App code is volume-mounted with uvicorn `--reload`: after `git pull`, changes
  apply automatically. **Rebuild required** only when `requirements.txt` or the
  Dockerfile change: `docker compose up -d --build service`
- Switching to a local LLM later: uncomment the `ollama` service in
  `docker-compose.yml` and set `VISION_PROVIDER=ollama` in `.env`.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `/ui/` | Web UI (expiring, add food, defaults) |
| `POST /analyze/food` | Photo → parsed item(s) |
| `POST /analyze/receipt` | Receipt → parsed item list |
| `GET /analyze/barcode/{code}` | Open Food Facts lookup |
| `POST /inventory/import` | Import items to Grocy |
| `GET /expiring/?days=N` | Expiring items (JSON) |
| `GET /expiring/summary` | Urgency counts for HA sensors |
| `GET /expiring/display` | Plain text for ESPHome/TFT displays |
| `GET /health` | Provider + Grocy connectivity |

Interactive API docs at `/docs`.
