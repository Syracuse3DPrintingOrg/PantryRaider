# Roadmap

Pantry Raider is pre-1.0, self-hosted, and moves fast. This page is a snapshot
of direction, not a promise: priorities can shift as hardware arrives, users
weigh in, or something turns out harder (or easier) than expected. There are
no dates and no version numbers here on purpose. For the detailed, dated
history of what's actually shipped, see the [changelog](CHANGELOG.md).

## Recently shipped

- Bandit Cubs: small ESP32 companion displays you flash right from the
  browser, paired over Bluetooth broadcast
- Fridge and freezer protection: hygrometer threshold alarms and door
  contact sensors, with grace periods before anything alerts
- Shelf buttons: a physical press that adds to the shopping list or runs a
  chosen action
- Community shelf-life learning: opt-in, anonymized best-by data shared
  across installs to sharpen expiry suggestions
- The Home Assistant integration: sensors, automations, and a Lovelace
  dashboard alongside the app
- A published privacy policy and self-serve account controls (manage or
  cancel a subscription, delete your account entirely) for anyone using the
  optional cloud features

## In motion

- A STEMMA QT / Qwiic hardware ecosystem for small add-on sensors and
  controls, starting with a NeoKey scan-mode selector (a physical button
  to flip the barcode scanner between adding, using, shopping, and audit)
- An integrations registry that makes inventory backends and device support
  modular, instead of each one being wired in by hand
- A relay so Bandit Cub sensors can reach the server through another Cub
  when they're out of direct range

## Ahead

- An installable phone experience (a proper PWA you can add to your home
  screen)
- A dashboard builder for arranging your own home screen layout
- NFC tags for tracking leftovers
- Smart scale support
- Appliance orchestration, built safety-first: the app will only ever be
  allowed to turn something off, never on
- Kitchen air quality monitoring
- E-ink displays as a lower-power, always-visible option alongside Bandit
  Cubs
- Native iOS and Android apps, if there's enough demand beyond the PWA

## Durability and infrastructure

- Faster kiosk navigation
- A push channel for live updates, instead of the screen waiting to poll
- USB SSD storage for Pi appliances (an upgrade over the SD card), shelved
  for later

Have an idea or run into something broken? [GitHub issues](https://github.com/Syracuse3DPrintingOrg/PantryRaider/issues)
are open and welcome.
