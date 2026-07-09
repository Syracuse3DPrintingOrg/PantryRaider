# Pantry Raider

A self-hosted food tracker that helps you manage what is in your fridge, reduce
waste, and plan meals. It runs entirely on your own hardware with no required
cloud dependency, built on [Grocy](https://grocy.info/) for inventory with
optional [Mealie](https://mealie.io/) recipes and a local LLM.

What Pantry Raider adds on top of Grocy:

- **AI photo import.** Photograph a pile of groceries and queue them all for
  review at once, without typing.
- **Barcode scanning with LLM enrichment.** Scan by camera, USB scanner, or
  manual entry; Open Food Facts provides product data and an optional LLM pass
  cleans up messy names.
- **Kitchen kiosk and Stream Deck.** A dedicated countertop control surface and
  an optional on-screen Start Page that works like an on-screen Stream Deck.
- **Home Assistant integration.** REST sensors, barcode automations, a Lovelace
  dashboard, and on-screen notifications and camera pop-ups.
- **Recipe suggestions from what you have.** Ranks your recipe library by how
  much of each recipe is already in stock, surfacing items that expire soon.

## Where to go next

- [Platforms and deployment](platforms.md): the server and Raspberry Pi
  appliance modes, hosting the stack, AI providers, Home Assistant, and HTTPS.
- [Personalization and on-screen features](personalization.md): themes,
  background images, the navigation editor, the Start Page, the screensaver,
  shared kitchen timers, weather, and on-screen Home Assistant events.
- [Settings matrix](settings-matrix.md): every persisted setting and where it
  can be edited across the three deployment modes.
- [Hardware](hardware.md): the appliance hardware, supported peripherals, and
  building the SD-card image.
- [Recipe backend comparison](recipe-backend-comparison.md) and the
  [HTTP API](api.md).

The project source and issues live on
[GitHub](https://github.com/Syracuse3DPrintingOrg/PantryRaider). Pantry Raider
is free for home use; if it has earned a spot on your counter, you can
[buy the developer a coffee](https://www.buymeacoffee.com/syracuse3dprinting) ☕.
