# Changelog

All notable changes to FoodAssistant are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project follows
[semantic versioning](https://semver.org/).

> **For releases:** use the entry below as the GitHub Release description rather
> than the auto-generated commit list, so notes stay focused on user-facing
> changes.

## [Unreleased]

### Added
- **On-device Pi installer.** A new `install.sh` (run on the device over SSH: `curl -fsSL .../install.sh | bash`) replaces the old "edit config on your PC and copy a payload onto the boot partition" flow. Flash a stock Raspberry Pi OS Lite card with Imager, boot, SSH in, and run one line. The installer detects the board, attached display, and Stream Deck, asks for the deployment mode (Pi Hosted, Pi Remote, or Server) and add-ons (Mealie, Ollama, kiosk, Stream Deck), then provisions only what you picked. Pi Remote installs nothing heavy. Supports unattended use with `NONINTERACTIVE=1` plus env vars.

### Changed
- **SD-card guide rewritten** around the SSH installer; nothing to edit on your PC and no repo clone on your PC. The pre-built turnkey image remains documented as an advanced no-SSH alternative.

### Removed
- `scripts/image-build/prepare-image.ps1` and the documented boot-partition payload flow it implemented. Use the on-device installer instead. The pre-built image pipeline (`prepare-image.sh --image`, used by CI) is unchanged.

## [1.6.0]

### Added
- **Deployment modes in setup.** The first setup step now asks how the device is used. On a Raspberry Pi you choose **Pi Hosted** (everything runs on the Pi, with or without a screen) or **Pi Remote** (a thin control surface that drives a Stream Deck and/or kiosk pointed at a FoodAssistant server already running elsewhere); on other hardware it stays **Server hosted**. Pi Remote installs no local Grocy or Docker, so it runs on a Pi 3, and the wizard skips the Grocy and AI steps for it. The choice is detected and offered automatically based on the board.
- **Shopping list without Mealie.** The Shopping tab is now always visible. When Mealie is not configured it is backed by Grocy's built-in shopping list: add, check off, and delete items. Multi-list selector appears when more than one list exists. A "Clear checked" button removes done items. When Mealie is configured the existing Mealie-backed view is unchanged.
- **Stock journal.** A new Stock Journal page (link in the Inventory header) shows the last 50/100/200 stock transactions from Grocy: date, product name, transaction type (Added, Consumed, Moved, Corrected), quantity, and note. A live text filter narrows by product name.
- **KMS display rotation.** Set `DISPLAY_ROTATION=90` (or 180, 270) in `image/config.env` before flashing to rotate the framebuffer at the OS level. Rotates the boot console and kiosk browser, unlike the CSS-only setting in the app. A `foodassistant-set-rotation` helper script is installed for runtime changes without reflashing.
- **Barcode scanner type** selector in the setup wizard (USB HID or Camera). USB HID includes a test input to confirm the scanner sends Enter after each code.
- **Settings status indicators.** The Settings sidebar shows colored icons for each section so misconfigured areas are visible at a glance. A warning banner appears at the top of Settings when Grocy is unreachable or no password is set.
- **Nav unlock hints.** When Mealie or another optional service is not configured, a small lock icon appears in the navbar with a tooltip listing the locked tabs and a link to the relevant Settings pane.
- **Stream Deck timers.** Three independent countdown timer keys (`timer_1`, `timer_2`, `timer_3`). Press to cycle through 5, 10, 15, 30, and 60 minute presets; press again to cancel. The key shows MM:SS while counting down, turns amber under 1 minute, and flashes red with "Done!" when the timer expires. Press once more to dismiss.
- **Targeted provisioner re-runs.** `STEPS=rotation,kiosk bash firstboot.sh` re-runs only the named steps, bypassing the done-marker check. Valid step names: `hostname`, `timezone`, `mdns`, `docker`, `stack`, `rotation`, `kiosk`, `streamdeck`.

### Fixed
- QR modal now closes reliably on dark themes (modal was nested inside the collapsible navbar, causing backdrop conflicts).
- Grocy URL in the setup wizard now uses the browser's host instead of `localhost` when viewed from a different machine on the network.
- Navigation unlock hints show all tab names for a locked service, not just the last one.

## [1.5.0]

### Added
- **Ready-to-flash appliance image.** A prebuilt Raspberry Pi OS Lite image with the FoodAssistant provisioner baked in is published to the Releases page. Flash it with Raspberry Pi Imager, set wifi in the Imager GUI, and boot: no config files, no terminal. The device auto-detects an attached display (launches the kiosk) and a plugged-in Stream Deck, and takes its timezone from the OS.
- **Stream Deck controller** (`streamdeck/`). Drive an Elgato Stream Deck or embedded Stream Deck Module (6, 15, or 32 keys) as a physical control surface. Keys show live counts (items expiring soon, scans waiting to commit) and trigger actions like committing pending scans or steering the attached kiosk browser. Key text is large and legible, scales to each deck's pixel density, and the deck can be rotated 0/90/180/270 degrees. Includes a systemd unit, udev rule, and example config. See `streamdeck/README.md`.
- **Optional AI.** FoodAssistant now works without an AI provider. Inventory, expiry tracking, manual entry, and barcode lookup via Open Food Facts all keep working; photo import, receipt scanning, and recipe suggestions are simply off until you add a provider. Choose "None" in the setup wizard or **Settings → AI**.
- **Guided setup wizard.** First-time setup is now a step-by-step flow (welcome, security, Grocy, AI, optional integrations, done) with clear required fields, instead of one dense form.
- **Attached-display settings.** A scale (Small to Extra large) and orientation (0/90/180/270) control for a hardware screen wired to the appliance. These apply only to the kiosk display, never to a phone or laptop browsing the app.
- **About & Credits page** listing the open-source projects FoodAssistant builds on (Grocy, Mealie, Open Food Facts, TheMealDB, and more), with links and a note to support them.

### Changed
- Default Gemini model is now `gemini-2.5-flash` (the old `gemini-1.5-flash` default is no longer available).
- README rewritten with a "Why FoodAssistant?" section; the full API reference moved to `docs/api.md`.

### Fixed
- **Theme and display scale save immediately.** Picking a theme or scale in **Settings → Interface** applies and persists right away instead of waiting for **Save All**.
- QR code is now scannable on dark themes (white background), and the QR modal closes reliably.
- The appliance first boot no longer clobbers Raspberry Pi Imager's wifi/SSH/user setup (our provisioner script was renamed to avoid the collision).

## [1.4.0]

### Added
- **Remote Access**: expose your FoodAssistant to the internet without port-forwarding. In **Settings → Remote Access**, choose Cloudflare Tunnel (free, bring your own token) or FoodAssistant Cloud (managed subscription, coming soon). The tunnel runs as a sidecar container; your public URL appears in the UI once the connection is established.
- **Phone QR code**: a QR icon in the navbar opens a modal with a scannable code that jumps your phone's browser directly to the add-item page. Use your phone's camera for food photos without typing the server address.
- **Kiosk / touch mode**: a tablet icon in the navbar toggles touch-optimised sizing (48 px minimum tap targets on buttons, inputs, and list items). Useful on a countertop touchscreen; the preference is remembered in the browser.
- **SD-card image tooling**: `scripts/image-build/` and `image/config.env` for building a flashable Raspberry Pi appliance image. First boot auto-installs Docker, starts the full stack, configures mDNS (`foodassistant.local`), and optionally launches a Chromium kiosk. Supports Pi 4, Pi 5, and generic ARM64/x86-64 Linux. See `docs/hardware/sd-image.md`.
- **Supported hardware guide**: `docs/hardware/supported-hardware.md` lists tested boards, minimum RAM requirements, and peripheral compatibility (barcode scanners, displays, cameras).

### Changed
- **Cook page preference panel**: complexity, spice, max-cook-time, portions sliders and dietary-preference pills (Vegetarian, Vegan, Keto, Gluten Free, etc.) now also filter web recipe suggestions (Spoonacular via `complexSearch`), not just AI suggestions. Cuisine picker (Asian, Italian, Thai, Mexican, and more) added with broad-region expansion for TheMealDB.
- AI-only preference hints added alongside sliders that cannot filter recipe databases (Complexity, Spice, Portions).

### Fixed
- TheMealDB dietary post-filter now correctly blocks compound ingredient names (e.g. "parmesan cheese" catches the vegan exclusion for "cheese").

## [1.3.1]

### Added
- **Synthwave theme**: a new neon-on-dark theme (hot pink, electric cyan, purple) with glow accents, in **Settings → Interface**.

### Fixed
- Corrected badge text contrast in the Cyborg and Darkly themes, where status labels (Today, Refrigerated, etc.) could be hard to read.

## [1.3.0]

### Added
- Theme switcher in **Settings → Interface**: choose between Dark and Light (and extra built-in themes), applied across the whole app.

### Changed
- Reorganized the Settings menu into clearer sections. Storage categories now live under **Inventory**, recipe-suggestion tuning under **Recipes**, and backup/update tools under a dedicated **Backup & Updates** section.
- "What can I cook?" now matches your stock against the external recipe database (TheMealDB) much more reliably, so web recipe ideas show up alongside your own Mealie recipes.

### Fixed
- Corrected a Settings toggle that could fail to update its hint text.

## [1.2.0]

### Added
- **Grocy public URL**: set a separate external address for Grocy so the in-app links work through a reverse proxy while internal API calls stay on the local network.
- **Auto-check shopping list**: optionally tick items off your Mealie shopping list automatically when you scan and commit a matching item.

### Fixed
- The app no longer fails to start when its data directory is read-only on first launch.
- Corrected a Home Assistant automation sensor reference so the "expiring in 3 days" alert fires reliably.

## [1.1.0]

### Added
- **Custom storage locations**: define your own storage buckets beyond the four built-ins (Refrigerated, Frozen, Room Temp, Pantry), such as Wine Cellar or Garage Fridge.
- Screenshots and an expanded setup guide in the README.

### Changed
- Pinned the bundled Grocy, Mealie, and Ollama images to specific versions so an unattended update can't move you onto a breaking release. Documented how to upgrade them safely.

## [1.0.0]

First public release.

### Added
- **Inventory dashboard** with storage panels, drag-and-drop moves, inline edits, and expiry badges, backed by Grocy.
- **Photo analysis**: photograph a food item to extract name, brand, quantity, and printed best-by date.
- **Receipt import**: photograph a grocery receipt to queue every food line item for review.
- **Barcode lookup** via camera, USB/wireless scanner, or manual entry, backed by Open Food Facts with optional AI name cleanup.
- **Expiry defaults**: an editable rules table that fills in best-by dates by product type.
- **Recipe suggestions** ("What can I cook?") ranked by what you already have in stock, with items expiring soon floated to the top.
- **Recipe import** from a webpage, a photographed recipe card, TheMealDB, or AI-generated from a dish name.
- **Meal planning and shopping lists** through optional Mealie integration, including a week view and check-off shopping list.
- **Home Assistant integration**: REST sensors, notification automations, and a Lovelace dashboard.
- **Web setup wizard** with live connection tests.
- **Two-factor authentication** (TOTP) on top of password login.
- **Backups**: download your data as a zip, with optional scheduled off-box backup via rclone.
- Optional fully-local operation using Ollama for vision and text.
- Docker, Docker Compose, and Home Assistant add-on installation paths.
