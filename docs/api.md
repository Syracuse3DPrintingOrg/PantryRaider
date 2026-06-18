# API Reference

FoodAssistant exposes a REST API used by the UI, Home Assistant, and any external integrations.

Interactive docs (Swagger UI) are available at `/docs` when the app is running.

## UI Routes

| Endpoint | Description |
|---|---|
| `GET /setup` | Web setup wizard |
| `GET /ui/` | Inventory dashboard |
| `GET /ui/expiring` | Expiring items view |
| `GET /ui/add` | Add food (barcode, photo, manual) |
| `GET /ui/pending` | Pending scans queue |
| `GET /ui/defaults` | Expiry defaults editor |
| `GET /ui/cook` | Recipe suggestions ranked by inventory |
| `GET /ui/recipes` | Browse and import recipes |
| `GET /ui/mealplan` | Week meal plan (requires Mealie) |
| `GET /ui/shopping` | Shopping list (requires Mealie) |
| `GET /ui/about` | About and credits |

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | Connectivity and service status |
| `GET /expiring/summary` | Urgency counts for Home Assistant sensors |
| `GET /inventory/dashboard` | Full stock grouped by storage location |
| `GET /admin/backup` | Download app data as a zip archive |
| `GET /admin/version` | Running version string |
| `GET /admin/check-update` | Compare running version against latest GitHub tag |
| `POST /admin/backup/remote` | Push backup to configured rclone remote |

## Query Parameters

`GET /ui/expiring?days=N` — show items expiring within N days (default 7).

`GET /admin/backup?include_secrets=true` — include API keys and passwords in the backup zip (omit for a safe-to-store redacted copy).

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
