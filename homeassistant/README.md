# Home Assistant Integration

## 1. REST Sensors

Add the contents of `configuration.yaml` to your HA `configuration.yaml`, then restart HA.

This creates four sensors:
- `sensor.food_expired` — count of expired items
- `sensor.food_expiring_today` — count expiring today  
- `sensor.food_expiring_3d` — count expiring within 3 days
- `sensor.food_expiring_7d` — count expiring within 7 days
- `sensor.food_expiring_soon_list` — full list with attributes (used by the dashboard card)

## 2. Automations

Import `automations.yaml` entries into your HA automations. Adjust the `notify.notify` service
to match your notification target (mobile app, etc.).

## 3. Lovelace Dashboard

### Option A — New Dashboard
1. HA → Settings → Dashboards → Add Dashboard
2. Give it a name, set URL path to `food`
3. Open it → three-dot menu → Edit → Raw configuration editor
4. Paste the contents of `lovelace/food-dashboard.yaml`

### Option B — Existing Dashboard
Copy individual card configs from `lovelace/food-dashboard.yaml` into your existing views.

## 4. Grocy HACS Integration (optional but recommended)

Adds native Grocy sensors directly into HA entity registry.

1. HACS → Integrations → search "Grocy"
2. Install and restart HA
3. Settings → Devices & Services → Add Integration → Grocy
4. URL: `http://192.168.1.170:9383`  API key: (from your .env)
