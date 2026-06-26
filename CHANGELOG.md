# Changelog

All notable changes to FoodAssistant are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project follows
[semantic versioning](https://semver.org/).

> **For releases:** use the entry below as the GitHub Release description rather
> than the auto-generated commit list, so notes stay focused on user-facing
> changes.

## [Unreleased]

### Added
- **Version bump tooling.** `scripts/bump-version.sh [patch|minor|major]` edits the single source-of-truth `APP_VERSION`, and `scripts/install-git-hooks.sh` installs a pre-commit hook that auto-bumps the patch on each commit so every commit changes at least the patch number. The hook stays out of the way during rebases, merges, beads-only commits, and explicit minor/major bumps.
- **Kiosk display sleep with shared wake.** The kiosk screen now blanks after its idle timeout (previously a stored but unused setting) and wakes on a touch, key press, mouse move, or a Stream Deck button press. The display and the Stream Deck keep separate timeouts, but activity on either surface wakes both: the host bridge owns the display blanking and brokers activity between the screen and the deck. Manual blank/wake and the idle timeout are exposed through the bridge.
- **On-screen floating navigation menu.** An optional column of nav icons docked to a screen corner, handy on touch screens. Drag its handle to reposition it; it snaps to the nearest corner and remembers the spot per-device. A setting in Interface picks the default corner (or off), and it can auto-hide when a Stream Deck is connected, since the deck already provides navigation. The menu can be laid out vertically or horizontally per-device, and the page content is padded so the menu never sits on top of the text.
- **Restore from backup.** The Backup pane now has a Restore control that rebuilds this app's data (settings, database, staples) from a backup zip, the counterpart to Download Backup. The current data is copied aside first, archive paths are validated against zip-slip, and a redacted backup keeps the API keys you already have. Grocy and Mealie data are not touched (use the host script for a full snapshot).

### Changed
- **Settings menu reorganized into logical groups.** The Settings sidebar now groups its sections under Services, App, Devices & Hardware, and System headers instead of a flat list, so related settings sit together and the flow reads top to bottom. The satellite (Pi Remote) and Pi-only visibility rules are unchanged: a satellite still shows Main Server in place of the backend services, and Display/Stream Deck/Network stay Pi-only.
- **Per-section saving in Settings.** The single "Save All" button is gone. Each settings section now has its own Save button that stores only that section's fields, so saving is explicit and scoped and one section's edits never carry another's. Options that apply instantly (like the theme preview and per-device toggles) keep working as before.
- **Cook suggestions match pantry staples better.** Recipes whose ingredients carry descriptor words (for example "parmesan cheese" or "grated parmesan" against a "Parmesan" staple) now match, so the "Ready to cook" and "With pantry staples" buckets populate from current stock instead of coming back empty.
- **Readable Stream Deck key labels.** Label text colour is chosen from each key's background brightness, so themed keys (for example a light-green Commit) stay legible instead of washing out white-on-light.
- **Co-hosted Grocy/Mealie browser links use the LAN IP.** The "Open Grocy/Mealie" links now prefer the device's LAN IP over its `.local` name, so they work on networks where mDNS does not resolve. The loopback address is still used for the behind-the-scenes API wiring.

### Fixed
- **Over-the-air Pi updates now refresh the Stream Deck too.** The update helper redeploys both the web app and the Stream Deck controller package (previously only the app), reinstalls Python dependencies only when they changed, restarts both services, and is safe to re-run after a manual `git pull`.

## [0.6.0] - 2026-06-26

This is the first version under the project's pre-1.0 scheme. Earlier `1.x`
tags were retired: everything to date is pre-launch, and `1.0.0` is reserved
for the public release. See the note at the bottom of this file.

### Added
- **On-device Pi installer.** A new `install.sh` (run on the device over SSH: `curl -fsSL .../install.sh | bash`) replaces the old "edit config on your PC and copy a payload onto the boot partition" flow. Flash a stock Raspberry Pi OS Lite card with Imager, boot, SSH in, and run one line. The installer detects the board and attached hardware, asks only for the deployment mode (Pi Hosted or Pi Remote), then hands off to the web setup wizard for all further configuration. Pi Remote installs nothing heavy. Supports unattended use with `NONINTERACTIVE=1` plus env vars.
- **Web-based appliance configuration.** After the one-line SSH install, the terminal prints the `http://foodassistant.local:9284/setup` URL and exits. All remaining setup (password, Grocy API key, AI provider, display orientation, Stream Deck, Mealie, etc.) happens in the browser. This makes pre-ship configuration possible: configure before shipping, customer plugs in and opens the URL.
- **Appliance settings panes (Pi only).** Three new sections appear in Settings when running on a Raspberry Pi: **Display** (kiosk scale, CSS rotation, and KMS framebuffer rotation with optional immediate reboot), **Stream Deck** (enable/disable, model selection, service restart), and **Network** (current Wi-Fi SSID, connect to a new network, change hostname). All accessible at any time after first setup.
- **Host bridge service.** A small Python helper (`foodassistant-host-bridge`, installed by `firstboot.sh` at `/usr/local/bin/`) runs on the Pi host at `127.0.0.1:9299`. It lets the Docker container call host-level operations (Wi-Fi via `nmcli`, hostname via `hostnamectl`, KMS rotation via `foodassistant-set-rotation`, Stream Deck service restart via `systemctl`) without running privileged inside Docker. Reachable from the container because `docker-compose.appliance.yml` uses `network_mode: host`.
- **Hardware settings pane.** Barcode scanner configuration moved out of the Inventory and Interface panes into a dedicated Hardware section, visible on all devices, with a global-capture switch and Waveshare scanner setup link.
- **Navbar health warnings (Pi).** The navbar surfaces a warning icon when the Pi reports undervoltage, throttling, high temperature, or low disk, read from the host bridge. The tooltip lists the active warnings.
- **Stream Deck size auto-detect.** Setup detects the attached deck (6/15/32 keys) and prefills the model, with a hint when it was filled from the hardware.
- **Stream Deck weather sync.** A satellite's deck mirrors the main server's weather location and units automatically, so the widget matches without separate local setup.
- **Stream Deck themed keys.** Key colors follow the active web UI theme (light, darkly, cyborg, flatly, synthwave); the default dark theme keeps the existing per-action colors.
- **Named Stream Deck profiles.** Save key layouts as named profiles on the main server, each targeting a deck size (6/15/32). A profile picker in the Stream Deck settings filters to the current deck, and satellites mirror the profile list on sync.
- **Deployment modes in setup.** The first setup step now asks how the device is used. On a Raspberry Pi you choose **Pi Hosted** (everything runs on the Pi, with or without a screen) or **Pi Remote** (a thin control surface that drives a Stream Deck and/or kiosk pointed at a FoodAssistant server already running elsewhere); on other hardware it stays **Server hosted**. Pi Remote installs no local Grocy or Docker, so it runs on a Pi 3, and the wizard skips the Grocy and AI steps for it. The choice is detected and offered automatically based on the board.
- **Shopping list without Mealie.** The Shopping tab is now always visible. When Mealie is not configured it is backed by Grocy's built-in shopping list: add, check off, and delete items. Multi-list selector appears when more than one list exists. A "Clear checked" button removes done items. When Mealie is configured the existing Mealie-backed view is unchanged.
- **Stock journal.** A new Stock Journal page (link in the Inventory header) shows the last 50/100/200 stock transactions from Grocy: date, product name, transaction type (Added, Consumed, Moved, Corrected), quantity, and note. A live text filter narrows by product name.
- **KMS display rotation.** Set `DISPLAY_ROTATION=90` (or 180, 270) in `image/config.env` before flashing to rotate the framebuffer at the OS level. Rotates the boot console and kiosk browser, unlike the CSS-only setting in the app. A `foodassistant-set-rotation` helper script is installed for runtime changes without reflashing.
- **Barcode scanner type** selector in the setup wizard (USB HID or Camera). USB HID includes a test input to confirm the scanner sends Enter after each code.
- **Settings status indicators.** The Settings sidebar shows colored icons for each section so misconfigured areas are visible at a glance. A warning banner appears at the top of Settings when Grocy is unreachable or no password is set.
- **Nav unlock hints.** When Mealie or another optional service is not configured, a small lock icon appears in the navbar with a tooltip listing the locked tabs and a link to the relevant Settings pane.
- **Stream Deck timers.** Three independent countdown timer keys (`timer_1`, `timer_2`, `timer_3`). Press to cycle through 5, 10, 15, 30, and 60 minute presets; press again to cancel. The key shows MM:SS while counting down, turns amber under 1 minute, and flashes red with "Done!" when the timer expires. Press once more to dismiss.
- **Targeted provisioner re-runs.** `STEPS=rotation,kiosk bash firstboot.sh` re-runs only the named steps, bypassing the done-marker check. Valid step names: `hostname`, `timezone`, `mdns`, `docker`, `stack`, `rotation`, `kiosk`, `streamdeck`.

### Changed
- **SD-card guide rewritten** around the SSH installer; nothing to edit on your PC and no repo clone on your PC. The pre-built turnkey image remains documented as an advanced no-SSH alternative.
- **Installer is now minimal at the terminal.** `install.sh` asks one question (deployment mode) and auto-detects kiosk/Stream Deck from attached hardware. Display rotation, Mealie, Ollama, and other add-ons are configured via the web UI after the install completes, not in the terminal.
- **Kiosk mode auto-hides reference pages** (phone QR, Defaults, About, AI Declarations, API docs, keyboard shortcuts) from the navbar so the touchscreen surface stays focused.

### Removed
- `scripts/image-build/prepare-image.ps1` and the documented boot-partition payload flow it implemented. Use the on-device installer instead. The pre-built image pipeline (`prepare-image.sh --image`, used by CI) is unchanged.

### Fixed
- QR modal now closes reliably on dark themes (modal was nested inside the collapsible navbar, causing backdrop conflicts).
- Grocy URL in the setup wizard now uses the browser's host instead of `localhost` when viewed from a different machine on the network.
- Navigation unlock hints show all tab names for a locked service, not just the last one.

## [0.5.0]

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

## [0.4.0]

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

## [0.3.1]

### Added
- **Synthwave theme**: a new neon-on-dark theme (hot pink, electric cyan, purple) with glow accents, in **Settings → Interface**.

### Fixed
- Corrected badge text contrast in the Cyborg and Darkly themes, where status labels (Today, Refrigerated, etc.) could be hard to read.

## [0.3.0]

### Added
- Theme switcher in **Settings → Interface**: choose between Dark and Light (and extra built-in themes), applied across the whole app.

### Changed
- Reorganized the Settings menu into clearer sections. Storage categories now live under **Inventory**, recipe-suggestion tuning under **Recipes**, and backup/update tools under a dedicated **Backup & Updates** section.
- "What can I cook?" now matches your stock against the external recipe database (TheMealDB) much more reliably, so web recipe ideas show up alongside your own Mealie recipes.

### Fixed
- Corrected a Settings toggle that could fail to update its hint text.

## [0.2.0]

### Added
- **Grocy public URL**: set a separate external address for Grocy so the in-app links work through a reverse proxy while internal API calls stay on the local network.
- **Auto-check shopping list**: optionally tick items off your Mealie shopping list automatically when you scan and commit a matching item.

### Fixed
- The app no longer fails to start when its data directory is read-only on first launch.
- Corrected a Home Assistant automation sensor reference so the "expiring in 3 days" alert fires reliably.

## [0.1.0]

### Added
- **Custom storage locations**: define your own storage buckets beyond the four built-ins (Refrigerated, Frozen, Room Temp, Pantry), such as Wine Cellar or Garage Fridge.
- Screenshots and an expanded setup guide in the README.

### Changed
- Pinned the bundled Grocy, Mealie, and Ollama images to specific versions so an unattended update can't move you onto a breaking release. Documented how to upgrade them safely.

## [0.0.1]

First working build (the original pre-launch baseline).

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

---

## A note on versioning

This project was briefly tagged `1.0.0` through `1.5.0` during early
development before it had any real users. Those tags were retired and the
history re-anchored under a pre-1.0 scheme: the versions above are all
pre-launch milestones, and `1.0.0` is reserved for the first public release.
The mapping from the old tags was a straight subtract-one-major (old `1.6.0`
became `0.6.0`), with the genesis release floored to `0.0.1`.
