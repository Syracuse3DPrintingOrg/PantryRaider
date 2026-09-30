# EspControl wall panels as kitchen controllers

[EspControl](https://github.com/jtenniswood/espcontrol) is a no-code touch
panel for cheap ESP32 touchscreens (4 to 10 inches, most under $50): flash it
from a browser, join it to WiFi, add it to Home Assistant, and lay out buttons
and readings from the screen's own setup page. Because it can show and press
anything Home Assistant can see, it makes a great low-cost wall controller for
Pantry Raider: start kitchen timers, flip the barcode scanner mode, ping the
kitchen screen, and watch what is expiring, all from a panel by the stove.

EspControl is a separate project under the PolyForm Noncommercial license, so
Pantry Raider does not bundle or ship any part of it. The connector is a
plain Home Assistant package that turns Pantry Raider's actions and counts
into ordinary Home Assistant scripts and sensors; EspControl then places
those on a panel like anything else.

## What you get on the panel

Buttons (Home Assistant scripts):

- Kitchen timers: 5 min, 10 min, pasta, soft egg (same presets as the Timers
  page; they show up on every Pantry Raider surface and the Stream Deck)
- Clear kitchen timers
- Scanner: next mode (cycles inventory, consume, shopping, audit)
- Ping the kitchen screen (an on-screen note on the Pantry Raider kiosk)

Readings (sensors):

- Timers running, and the next timer's label and remaining time
- Items expiring within the week
- Items waiting in Review

## Setup

1. Get EspControl itself running first: flash a supported screen and add it
   to Home Assistant, following the EspControl documentation.
2. Copy [`homeassistant/espcontrol.yaml`](https://github.com/Syracuse3DPrintingOrg/PantryRaider/blob/main/homeassistant/espcontrol.yaml)
   from the Pantry Raider repository into your Home Assistant config as
   `packages/pantry_raider_espcontrol.yaml`. If you have never used packages,
   enable them once in `configuration.yaml`:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

3. Edit the file: replace the example address with your Pantry Raider LAN
   address (the `http://192.168.x.x:9284` one, never a public URL). If your
   install has a password, uncomment the `headers:` blocks and put the API
   key from Settings, Security into `secrets.yaml` as
   `pantry_raider_api_key`.
4. Restart Home Assistant. The new `script.pantry_raider_*` buttons and
   `sensor.pantry_raider_*` readings appear as regular entities.
5. Open the EspControl screen's setup page and drag those entities onto your
   layout.

## Notes

- The package is self-contained: it does not touch the sensors or automations
  from the main [Home Assistant integration](../index.md), so you can use
  either or both.
- Timers started from the panel are real Pantry Raider timers: they ring on
  the kiosk, float on the screensaver, and show on the Stream Deck.
- The panel polls counts gently (timers every 15 seconds, the rest every few
  minutes), so a small kitchen server never notices it.
