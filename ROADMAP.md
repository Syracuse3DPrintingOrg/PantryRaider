# Roadmap

Pantry Raider is pre-1.0, self-hosted, and moves fast. This page is a snapshot
of direction, not a promise: priorities can shift as hardware arrives, users
weigh in, or something turns out harder (or easier) than expected. There are
no dates and no version numbers here on purpose. For the detailed, dated
history of what's actually shipped, see the [changelog](CHANGELOG.md).

## Recently shipped

- Bandit Cubs: small ESP32 companion displays you flash right from the
  browser, fed by a Bluetooth status broadcast with nothing to pair
- Counter keys: an Adafruit NeoKey 1x4 by the scanner that switches what a
  scan does, opens pages, and runs timers
- Install it like an app: add Pantry Raider to a phone or tablet home screen
  and it opens in its own window
- Fridge and freezer protection: hygrometer threshold alarms and door
  contact sensors, with grace periods before anything alerts
- Shelf buttons: a physical press that adds to the shopping list or runs a
  chosen action
- Community shelf-life learning: opt-in, anonymized best-by data shared
  across installs to sharpen expiry suggestions
- The Home Assistant integration: a HACS integration that finds your
  install, pairs with an on-screen code, and adds live entities,
  notifications, and camera pop-ups
- A published privacy policy and self-serve account controls (manage or
  cancel a subscription, delete your account entirely) for anyone using the
  optional cloud features

## In motion

- More STEMMA QT / Qwiic add-ons: small plug-in sensors and controls on
  the same one-cable connector the counter keys use
- An integrations registry that makes inventory backends and device support
  modular, instead of each one being wired in by hand
- A relay so Bandit Cub sensors can reach the server through another Cub
  when they're out of direct range

## Ahead

- A dashboard builder for arranging your own home screen layout
- NFC tags for tracking leftovers
- Smart scale support
- Appliance orchestration, built safety-first: the app will only ever be
  allowed to turn something off, never on
- Kitchen air quality monitoring
- E-ink displays as a lower-power, always-visible option alongside Bandit
  Cubs
- Native iOS and Android apps, if there's enough demand beyond the installable web app

## Durability and infrastructure

- Faster kiosk navigation
- Live push for everything: key presses and alerts already reach the screen
  instantly, and the rest of the display still checks in on a timer
- USB SSD storage for Pi appliances (an upgrade over the SD card), shelved
  for later

Have an idea or run into something broken? [GitHub issues](https://github.com/Syracuse3DPrintingOrg/PantryRaider/issues)
are open and welcome.
