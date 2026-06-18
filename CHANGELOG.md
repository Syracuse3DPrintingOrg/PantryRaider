# Changelog

All notable changes to FoodAssistant are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project follows
[semantic versioning](https://semver.org/).

> **For releases:** use the entry below as the GitHub Release description rather
> than the auto-generated commit list, so notes stay focused on user-facing
> changes.

## [Unreleased]

### Added
- **Stream Deck controller** (`streamdeck/`). Drive an Elgato Stream Deck or embedded Stream Deck Module (6, 15, or 32 keys) as a physical control surface. Keys show live counts (items expiring soon, scans waiting to commit) and trigger actions like committing pending scans or opening a page on the attached display. Works alongside a touchscreen or as the only interface on a headless appliance. Includes a systemd unit, udev rule, and example config. See `streamdeck/README.md`.

### Fixed
- **Theme now saves immediately.** Picking a theme in **Settings, Interface** applies and persists right away instead of waiting for **Save All**, so the choice sticks across pages and devices.

## [1.4.0]

### Added
- **Remote Access** — expose your FoodAssistant to the internet without port-forwarding. In **Settings → Remote Access**, choose Cloudflare Tunnel (free, bring your own token) or FoodAssistant Cloud (managed subscription, coming soon). The tunnel runs as a sidecar container; your public URL appears in the UI once the connection is established.
- **Phone QR code** — a QR icon in the navbar opens a modal with a scannable code that jumps your phone's browser directly to the add-item page. Use your phone's camera for food photos without typing the server address.
- **Kiosk / touch mode** — a tablet icon in the navbar toggles touch-optimised sizing (48 px minimum tap targets on buttons, inputs, and list items). Useful on a countertop touchscreen; the preference is remembered in the browser.
- **SD-card image tooling** — `scripts/image-build/` and `image/config.env` for building a flashable Raspberry Pi appliance image. First boot auto-installs Docker, starts the full stack, configures mDNS (`foodassistant.local`), and optionally launches a Chromium kiosk. Supports Pi 4, Pi 5, and generic ARM64/x86-64 Linux. See `docs/hardware/sd-image.md`.
- **Supported hardware guide** — `docs/hardware/supported-hardware.md` lists tested boards, minimum RAM requirements, and peripheral compatibility (barcode scanners, displays, cameras).

### Changed
- **Cook page preference panel** — complexity, spice, max-cook-time, portions sliders and dietary-preference pills (Vegetarian, Vegan, Keto, Gluten Free, etc.) now also filter web recipe suggestions (Spoonacular via `complexSearch`), not just AI suggestions. Cuisine picker (Asian, Italian, Thai, Mexican, and more) added with broad-region expansion for TheMealDB.
- AI-only preference hints added alongside sliders that cannot filter recipe databases (Complexity, Spice, Portions).

### Fixed
- TheMealDB dietary post-filter now correctly blocks compound ingredient names (e.g. "parmesan cheese" catches the vegan exclusion for "cheese").

## [1.3.1]

### Added
- **Synthwave theme** — a new neon-on-dark theme (hot pink, electric cyan, purple) with glow accents, in **Settings → Interface**.

### Fixed
- Corrected badge text contrast in the Cyborg and Darkly themes, where status labels (Today, Refrigerated, etc.) could be hard to read.

## [1.3.0]

### Added
- Theme switcher in **Settings → Interface** — choose between Dark and Light (and extra built-in themes), applied across the whole app.

### Changed
- Reorganized the Settings menu into clearer sections. Storage categories now live under **Inventory**, recipe-suggestion tuning under **Recipes**, and backup/update tools under a dedicated **Backup & Updates** section.
- "What can I cook?" now matches your stock against the external recipe database (TheMealDB) much more reliably, so web recipe ideas show up alongside your own Mealie recipes.

### Fixed
- Corrected a Settings toggle that could fail to update its hint text.

## [1.2.0]

### Added
- **Grocy public URL** — set a separate external address for Grocy so the in-app links work through a reverse proxy while internal API calls stay on the local network.
- **Auto-check shopping list** — optionally tick items off your Mealie shopping list automatically when you scan and commit a matching item.

### Fixed
- The app no longer fails to start when its data directory is read-only on first launch.
- Corrected a Home Assistant automation sensor reference so the "expiring in 3 days" alert fires reliably.

## [1.1.0]

### Added
- **Custom storage locations** — define your own storage buckets beyond the four built-ins (Refrigerated, Frozen, Room Temp, Pantry), such as Wine Cellar or Garage Fridge.
- Screenshots and an expanded setup guide in the README.

### Changed
- Pinned the bundled Grocy, Mealie, and Ollama images to specific versions so an unattended update can't move you onto a breaking release. Documented how to upgrade them safely.

## [1.0.0]

First public release.

### Added
- **Inventory dashboard** with storage panels, drag-and-drop moves, inline edits, and expiry badges, backed by Grocy.
- **Photo analysis** — photograph a food item to extract name, brand, quantity, and printed best-by date.
- **Receipt import** — photograph a grocery receipt to queue every food line item for review.
- **Barcode lookup** via camera, USB/wireless scanner, or manual entry, backed by Open Food Facts with optional AI name cleanup.
- **Expiry defaults** — an editable rules table that fills in best-by dates by product type.
- **Recipe suggestions** ("What can I cook?") ranked by what you already have in stock, with items expiring soon floated to the top.
- **Recipe import** from a webpage, a photographed recipe card, TheMealDB, or AI-generated from a dish name.
- **Meal planning and shopping lists** through optional Mealie integration, including a week view and check-off shopping list.
- **Home Assistant integration** — REST sensors, notification automations, and a Lovelace dashboard.
- **Web setup wizard** with live connection tests.
- **Two-factor authentication** (TOTP) on top of password login.
- **Backups** — download your data as a zip, with optional scheduled off-box backup via rclone.
- Optional fully-local operation using Ollama for vision and text.
- Docker, Docker Compose, and Home Assistant add-on installation paths.
