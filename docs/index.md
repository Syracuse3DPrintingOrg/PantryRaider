# Pantry Raider

A self-hosted food tracker that helps you manage what is in your fridge, reduce
waste, and plan meals. It runs entirely on your own hardware with no required
cloud dependency, built on [Grocy](https://grocy.info/) for inventory, with
recipes, meal planning, and shopping built in and an optional local LLM.

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
  much of each recipe is already in stock, surfacing items that expire soon,
  with a step-by-step Cook wizard to land you on tonight's dish.
- **Label and document printing.** Print food and spice labels with a
  drag-and-drop label designer, and send a recipe to a regular printer.
- **Bluetooth kitchen thermometers.** Read meat and probe thermometers locally,
  with live temperatures and target alerts on the Timers page.

## Where to go next

- [First run and zero-touch setup](first-run.md): what a new install sets up
  for you, so you never have to sign in to Grocy or Mealie by hand.
- [Platforms and deployment](platforms.md): the server and Raspberry Pi
  appliance modes, hosting the stack, AI providers, Home Assistant, and HTTPS.
- [Recipes: built in, Mealie optional](recipe-backend-comparison.md) and the
  [Cook page and Cook wizard](cooking.md): where recipes live and how the app
  turns your stock into something to make.
- [Printing labels and documents](printing.md): the label designer, adding a
  printer, and sharing printers across your devices.
- [Bluetooth kitchen thermometers](thermometers.md): reading probes on a Pi or
  through Home Assistant, with targets and alerts.
- [Personalization and on-screen features](personalization.md): themes,
  background images, the navigation editor, the Start Page, the screensaver
  (including your own photos), shared kitchen timers, weather, and on-screen
  Home Assistant events.
- [Device resources](device-resources.md): the live view of what your machine
  is doing.
- [Settings matrix](settings-matrix.md): every persisted setting and where it
  can be edited across the three deployment modes.
- [Hardware](hardware.md): the appliance hardware, supported peripherals, and
  building the SD-card image.
- The [HTTP API](api.md) reference.

The project source and issues live on
[GitHub](https://github.com/Syracuse3DPrintingOrg/PantryRaider). Pantry Raider
is free for home use; if it has earned a spot on your counter, you can
[buy the developer a coffee](https://www.buymeacoffee.com/syracuse3dprinting) ☕.
