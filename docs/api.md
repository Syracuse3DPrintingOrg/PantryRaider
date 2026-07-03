# API Reference

Pantry Raider exposes a REST API used by the UI, Home Assistant, and any external integrations.

Interactive docs (Swagger UI) are available at `/docs` when the app is running.

## UI Routes

| Endpoint | Description |
|---|---|
| `GET /setup` | Web setup wizard |
| `GET /ui/` | Inventory dashboard |
| `GET /ui/expiring` | Expiring items view |
| `GET /ui/add` | Manage Pantry (add, consume, shopping list, audit; barcode, photo, manual) |
| `GET /ui/pending` | Pending scans inbox |
| `GET /ui/audit` | Pantry audit (read-only, location-scoped stock count) |
| `GET /ui/journal` | Stock journal (recent Grocy transactions) |
| `GET /ui/defaults` | Expiry defaults editor |
| `GET /ui/cook` | Recipe suggestions ranked by inventory (requires Mealie) |
| `GET /ui/recipes` | Browse and import recipes (requires Mealie) |
| `GET /ui/current-recipe` | On the Line: active recipe view with timers (requires Mealie) |
| `GET /ui/mealplan` | Week meal plan (requires Mealie) |
| `GET /ui/shopping` | Shopping list (Mealie when configured, else Grocy's built-in list) |
| `GET /ui/nutrition` | Nutrition / food-intake tracker |
| `GET /ui/camera` | Live camera feeds (shown when a camera is configured) |
| `GET /ui/weather` | Full forecast page for the kiosk |
| `GET /ui/convert` | Unit converter and measurement cheat sheet |
| `GET /ui/kitchen-guide` | Kitchen reference guide |
| `GET /ui/timers` | Standalone timer page |
| `GET /ui/about` | About and credits |

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | Connectivity and service status |
| `GET /expiring/summary` | Urgency counts for Home Assistant sensors |
| `GET /inventory/dashboard` | Full stock grouped by storage location |
| `GET /admin/version` | Running version string |
| `GET /admin/check-update` | Compare running version against latest GitHub tag |
| `GET /admin/backup` | Download app data as a zip archive |
| `POST /admin/restore` | Restore app data from an uploaded backup zip |
| `POST /admin/backup/remote` | Push backup to configured rclone remote |
| `POST /admin/backup/test-remote` | Test that rclone can reach the configured remote |
| `GET /admin/logging` | Report whether debug logging is on |
| `POST /admin/logging` | Turn debug logging on or off |
| `GET /admin/logs/download` | Download the debug log bundle with secret values redacted |

## Current Recipe and Timers

The active recipe and timers live in server memory so every surface (web UI,
Stream Deck, satellites) shares one state.

| Endpoint | Description |
|---|---|
| `GET /current-recipe` | Return the active recipe (or null) |
| `POST /current-recipe` | Replace the active recipe |
| `DELETE /current-recipe` | Clear the active recipe |
| `POST /current-recipe/scale` | Set the servings-scale multiplier |
| `GET /current-recipe/timer-suggestions` | Timer suggestions parsed from the recipe's step durations |
| `POST /current-recipe/timers/start` | Start a real timer from a suggestion |
| `POST /current-recipe/from-mealie` | Load a Mealie recipe (by slug) as the active recipe |
| `GET /timers` | List every timer with fresh remaining/state |
| `POST /timers` | Create and start a timer for `seconds` |
| `GET /timers/{id}` | Return one timer's current state |
| `DELETE /timers/{id}` | Cancel and remove a timer |

## Barcode Scanning and Scanner Mode

A single physical scanner can mean different things depending on what the user is
doing. The mode is process-local and in-memory, like the active recipe and timers.

| Endpoint | Description |
|---|---|
| `POST /pending/scan` | Submit a scanned barcode; routed by the active scanner mode (inventory, consume, shopping, or audit) |
| `GET /pending/scanner-mode` | Return the current scanner mode and its label |
| `POST /pending/scanner-mode` | Set the scanner mode |
| `POST /pending/scanner-mode/cycle` | Advance to the next mode (inventory then consume then shopping then audit, wrapping) |

## Pantry Audit

A read-only, location-scoped stock count. Scans are recorded against the active
session and compared to the location's Grocy stock; nothing is written to Grocy.
On a satellite these forward to the main server.

| Endpoint | Description |
|---|---|
| `GET /audit/locations` | List storage locations to pick from |
| `POST /audit/start` | Begin an audit session locked to one location |
| `POST /audit/scan` | Record a scanned item as seen for the session |
| `GET /audit/status` | Expected vs scanned, with missing and unexpected items (polled by the page) |
| `POST /audit/stop` | End the session |

## Nutrition / Food Intake

Logs what was eaten with calories and macros so the Nutrition page can show
totals.

| Endpoint | Description |
|---|---|
| `POST /nutrition/log` | Record an intake entry (name, servings, calories, protein, carbs, fat) |
| `GET /nutrition/today` | Today's entries and totals |
| `GET /nutrition/recent?days=N` | Recent days with per-day totals |
| `DELETE /nutrition/{id}` | Delete an entry |
| `POST /nutrition/estimate` | Ask the AI provider to estimate macros for a food name (needs a provider) |

## Weather

| Endpoint | Description |
|---|---|
| `GET /ui/weather/data` | Server-side forecast for the kiosk weather page (Open-Meteo, with wttr.in as a fallback); `?location=` overrides the saved location |

## Home Assistant On-Screen Events

Turned on by `ha_events_enabled`. Automations push toasts and camera pop-ups to
the device screen; the kiosk polls for them.

| Endpoint | Description |
|---|---|
| `POST /events/notify` | Show a notification toast on the screen |
| `POST /events/camera-popup` | Pop a named camera up full-screen for a few seconds |
| `POST /events/navigate` | Navigate the kiosk to a page |
| `POST /events/test` | Send a test notification |
| `GET /events/poll` | Long-poll for queued on-screen events (used by the kiosk) |

## Recipe Import (requires Mealie)

| Endpoint | Description |
|---|---|
| `POST /mealie/recipes/import-url` | Import a recipe from a webpage (Mealie scraper, then LLM fallback) |
| `POST /mealie/recipes/import-file` | Import from a generic JSON / schema.org JSON-LD / Mealie export file |
| `POST /mealie/recipes/import-external` | Save an external-source recipe into Mealie |
| `POST /mealie/recipes/extract-photo` | Vision-LLM extraction from a photographed recipe |
| `POST /mealie/recipes/generate` | Ask the LLM to write a full recipe for a dish name |

## Appliance (Pi-only)

These call the host bridge on a Pi appliance and return a clear error elsewhere.

| Endpoint | Description |
|---|---|
| `POST /setup/restore` | Full Grocy + Mealie + app snapshot restore via the host bridge |

`POST /setup/restore` is distinct from `POST /admin/restore`: the former restores
the whole stack (Grocy, Mealie, app data) from a snapshot already on the device
(an absolute `.tar.gz` path or `rclone:<remote-path>`), while `/admin/restore`
rewrites only this app's data directory from an uploaded zip. The host bridge
itself exposes a `POST /restore` it proxies to, but this file documents the app API.

## Query Parameters

`GET /ui/expiring?days=N`: show items expiring within N days (default 7).

`GET /admin/backup?include_secrets=true`: include API keys and passwords in the backup zip (omit for a safe-to-store redacted copy).

`GET /inventory/dashboard` returns JSON matching the Grocy stock structure, grouped by storage category. This is the endpoint the Home Assistant Lovelace dashboard polls.

`GET /expiring/summary` returns urgency bucket counts:

```json
{
  "expired": 2,
  "today": 0,
  "3d": 3,
  "ok": 14
}
```

See the live `/docs` page for full request/response schemas.
