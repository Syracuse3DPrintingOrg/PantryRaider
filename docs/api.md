# API Reference

Pantry Raider exposes a REST API used by the UI, Home Assistant, and any external integrations.

Interactive docs (Swagger UI) are available at `/docs` when the app is running.

## Authentication

A browser signs in with the password and gets a session cookie. Everything
else, such as Home Assistant, a Bandit, a Bandit Cub, an ESP panel or your own
script, sends a key in the `X-API-Key` header:

```bash
curl -H "X-API-Key: YOUR_KEY" http://192.0.2.10:9284/expiring/summary
```

Keys are created in Settings, Security, Create a key, and each one is limited
to one job. A new key is shown once, so copy it then.

| What it can do (as Settings shows it) | Scope | Reaches |
|---|---|---|
| A second screen (satellite) | `satellite` | Every kitchen route, including Grocy and Mealie through `/api/proxy/`, but no Settings or admin routes |
| The Home Assistant integration | `ha` | The `/ha/` routes the integration uses and the `/events/` notification routes |
| Home Assistant sensors, buttons and ESPHome panels | `ha_rest` | Reading kitchen data (any `GET` outside Settings and admin), timers, `/pending/scan`, the scanner mode, `/events/`, and `/gadgets/esp-action` |
| A Bandit Cub or other small display | `display` | `/cub/summary`, the Cub firmware, timers, `/gadgets/esp-action` and `/gadgets/esp-screen` |
| Everything, including Settings | `admin` | Every route, including `/setup/`, `/admin/` and backups |
| Everything except Settings (an older key) | `legacy` | Keys from before 0.21: every route except Settings and admin routes |

- Settings (`/setup/...`) and the `/admin/` routes take a signed-in owner or an
  admin key. Any other key gets `403` with a message saying so. An admin key
  can only be created on an install that has a password, needs that password
  to create, and expires after 90 days.
- On an install with no password, any device on the home network can create a
  key without a password, and requests from the home network work without a
  key unless that kind of device is set to need one (Settings, Security,
  Require a key). Requests through a proxy or a tunnel are refused there.
- `GET /api/credentials` lists the keys (never their values) with what each
  can do, when it was last used and where from. `POST /api/credentials` creates
  one (`{"name": ..., "scope": ..., "password": ...}`) and returns the key once.
  `POST /api/credentials/{id}/revoke` revokes one and
  `POST /api/credentials/{id}/scope` changes what it can do. These take a
  signed-in owner or an admin key.
- Pairing lets a device ask for its own key instead of you copying one:
  `POST /api/pairing/request` with `{"hostname": ..., "kind": ...}` (a kind of
  `satellite`, `ha`, `ha_rest` or `display`) returns a short code shown on the
  device, the owner approves the matching code on the kitchen screen or in
  Settings, Bandit Remotes, and the device collects its key from
  `GET /api/pairing/status/{request_id}`. Pairing answers only the local
  network, and only while Settings, Security, Device pairing is on.
- `POST /api/credentials/exchange` lets a device that still holds an older
  full-access key swap it for a key of its own kind (today, the Home Assistant
  integration), from the home network only.

## UI Routes

| Endpoint | Description |
|---|---|
| `GET /setup` | Web setup wizard |
| `GET /ui/` | Inventory dashboard |
| `GET /ui/expiring` | Expiring items view |
| `GET /ui/add` | Manage (add, consume, shopping list, audit; barcode, photo, manual) |
| `GET /ui/pending` | Review: scanned and photographed items waiting for confirmation |
| `GET /ui/audit` | Pantry audit (stock count; scanning never writes back, corrections are opt-in) |
| `GET /ui/journal` | Stock journal (recent Grocy transactions) |
| `GET /ui/defaults` | Expiry defaults editor |
| `GET /ui/cook` | Recipe suggestions ranked by inventory |
| `GET /ui/recipes` | Browse and import recipes |
| `GET /ui/current-recipe` | On the Line: active recipe view with timers |
| `GET /ui/mealplan` | Week meal plan |
| `GET /ui/shopping` | Shopping list (kept in Grocy; Mealie where one is still in use) |
| `GET /ui/nutrition` | Nutrition / food-intake tracker |
| `GET /ui/camera` | Live camera feeds (shown when a camera is configured) |
| `GET /ui/weather` | Full forecast page for the kiosk |
| `GET /ui/convert` | Unit converter and measurement cheat sheet |
| `GET /ui/kitchen-guide` | Kitchen reference guide |
| `GET /ui/timers` | Time & Temp: shared kitchen timers and thermometer readings (`?view=timers` or `?view=thermometers` for one or the other) |
| `GET /ui/start` | The home screen: the Glance grid by default, or the custom Start Page launcher |
| `GET /ui/about` | About and credits |

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | Connectivity and service status (the Grocy and AI checks are reused for up to 30 seconds) |
| `GET /expiring/summary` | Urgency counts for Home Assistant sensors |
| `GET /inventory/dashboard` | Full stock grouped by storage location |
| `GET /admin/version` | Running version string |
| `GET /admin/check-update` | Compare running version against the latest GitHub release |
| `GET /admin/backup` | Download app data as a zip archive |
| `POST /admin/restore` | Restore app data from an uploaded backup zip |
| `POST /admin/backup/remote` | Push backup to configured rclone remote |
| `POST /admin/backup/test-remote` | Test that rclone can reach the configured remote |
| `GET /admin/logging` | Report whether debug logging is on |
| `POST /admin/logging` | Turn debug logging on or off |
| `GET /admin/logs/download` | Download the debug log bundle with secret values redacted |

## Current Recipe and Timers

The active recipe and timers live in server memory so every surface (web UI,
Stream Deck, satellites) shares one state. On a Pi Remote the timer endpoints
forward to the main server, so a timer started at any screen or deck shows up,
can be extended, and can be cancelled everywhere.

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
| `POST /timers/{id}/extend` | Add seconds to a running timer (the +1 min button; an expired timer cannot be extended) |
| `DELETE /timers/{id}` | Cancel and remove a timer |
| `DELETE /timers` | Clear every timer at once (the Clear all button) |

## Barcode Scanning and Scanner Mode

A single physical scanner can mean different things depending on what the user is
doing. The mode is shared by every screen and device and survives an app restart, so
the scanner comes back in the mode it was left in. The Manage page's four mode
tiles (Stock up, Use stock, Shopping list, Audit stock) are this same mode.

| Endpoint | Description |
|---|---|
| `POST /pending/scan` | Submit a scanned barcode; routed by the active scanner mode (inventory, consume, shopping, or audit) |
| `POST /pending/consume-link` | Link a barcode to a product, then use one up. A consume scan of an unlinked barcode replies with a suggested product and the body to send here once you confirm (`barcode`, `product_id`, `quantity`) |
| `GET /pending/scanner-mode` | Return the current scanner mode and its label |
| `POST /pending/scanner-mode` | Set the scanner mode |
| `POST /pending/scanner-mode/cycle` | Advance to the next mode (inventory then consume then shopping then audit, wrapping) |

## Pantry Audit

A stock count. Scans are recorded against the active session and compared to
Grocy's stock for the chosen scope (all areas by default, or one storage
location); scanning never writes to Grocy. The one deliberate write is
`POST /audit/apply`, run only from the page's Apply corrections button after
a confirmation. On a satellite these forward to the main server.

| Endpoint | Description |
|---|---|
| `GET /audit/locations` | List storage locations to pick from |
| `POST /audit/start` | Begin an audit session (all areas, or locked to one location) |
| `POST /audit/scan` | Record a scanned item as seen for the session |
| `GET /audit/status` | Expected vs scanned, with missing and unexpected items (polled by the page) |
| `POST /audit/apply` | Opt-in corrections: set each counted item's stock to the scanned amount; items never scanned are left as they were |
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
| `POST /events/navigate` | Navigate the kiosk to a page: `{"path": "ui/cook"}` or a page name such as `"expiring"`; anything that is not a page is refused with a 400 |
| `POST /events/test` | Send a test notification |
| `GET /events/poll` | Long-poll for queued on-screen events (used by the kiosk) |

## Recipe Import

| Endpoint | Description |
|---|---|
| `POST /mealie/recipes/import-url` | Import a recipe from a webpage (structured-data scraper, then LLM fallback) |
| `POST /mealie/recipes/import-file` | Import from a generic JSON / schema.org JSON-LD / Mealie export file |
| `POST /mealie/recipes/import-external` | Save an external-source recipe into the library |
| `POST /mealie/recipes/extract-photo` | Vision-LLM extraction from a photographed recipe |
| `POST /mealie/recipes/generate` | Ask the LLM to write a full recipe for a dish name |

## Grocy Locations

The buttons on the two Grocy location items in the Pending inbox. Both are
Settings routes, so they take a signed-in owner or an admin key, and both
answer `403` on a Bandit, whose Grocy is the main server's. Each reads Grocy
again and does only what is still needed, so calling it twice is safe.

| Endpoint | Description |
|---|---|
| `POST /setup/grocy/merge-fridges` | Move everything in Grocy's Fridge to Refrigerator, point products at Refrigerator, then hide Fridge (never deleted) |
| `POST /setup/grocy/mark-freezer` | Mark the Freezer location as a freezer in Grocy |

Both answer `{"ok": true, "message": ...}` when done. A merge that could not
hide Fridge yet (stock Grocy would not move) answers `409`, and one Grocy
refused answers `502`; both carry the reason in `error`.

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

`GET /inventory/dashboard?dates=breakdown`: the Inventory page's own reading of the same rows. A row whose stock in that place has more than one best-by date also carries `best_by_dates`, a list of `{"best_before_date", "amount"}` with the earliest date first and undated stock (`null`) last. Each row's `amount`, `amount_opened` and `best_before_date` then come from that place's own stock entries, and a product with no stock of its own (a parent product whose stock all sits in its sub-products) is left out. Without `dates`, or with any other value, the reply is unchanged.

`GET /inventory/dashboard?dates=split`: a place that holds more than one best-by date for a product gets a row for each date instead. Such a row carries `"by_date": true`, its own `amount`, `amount_opened`, `best_before_date`, `days_remaining`, `urgency` and `added_date`, and `stock_entry_id`, the Grocy stock entry a label printed from the row points at; `product_amount` stays the product's total. A place with a single date, and stock with no location, keep one row without these keys, as in `breakdown`. `dates=auto`, which the Inventory page sends, reads like `split` while One row per best-by date (Settings, Inventory) is on, the default, and like `breakdown` when it is off.

The buttons on a `by_date` row name its place and date, so they touch only that stock: `POST /inventory/consume/{id}?amount=N&location_id=L&best_before_date=D`, `POST /inventory/open/{id}?location_id=L&best_before_date=D`, `PATCH /inventory/edit/{id}` with `from_location_id` and `from_best_before_date` in the body (the new date stays in `best_before_date`, and a `category` still applies to the whole product), and `POST /inventory/move/{id}` with `from_best_before_date` next to `from_location_id`. The date `D` is `YYYY-MM-DD`, or `none` for the stock with no best-by date, and always comes with a location; a malformed date, or one without a location, answers 422. When Grocy cannot act on exactly that stock, for example because it would use an opened item or an earlier date first, the call answers 409 with the reason in `detail` (such as "Grocy would use an opened one first, so nothing was used.") and changes nothing; when the row's stock is gone it answers 404. A consume answers how much was `consumed` of the `requested` amount, with `complete` false when Grocy stopped partway; an open answers the `new_best_by` of the opened item, and a move what was `left_behind`. The page's Edit window also sends `from_location_id` and `from_best_before_date` for a plain row whose place holds one best-by date, so a changed date reaches only that place's stock. `POST /inventory/move/{id}` also takes an optional `amount` next to `from_location_id`, with or without `from_best_before_date`, and then moves only that much: the stock Grocy would use first goes first, only the part that moves gets a new best-by date, and `left_behind` counts only what was asked for and did not move. An `amount` of at least what is there moves all of it, and one of 0 or less, or one sent without `from_location_id`, answers 422. Without these parameters every call works as before.

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
