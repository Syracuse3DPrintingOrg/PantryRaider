# Changelog

All notable changes to Pantry Raider are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project follows
[semantic versioning](https://semver.org/).

> **For releases:** use the entry below as the GitHub Release description rather
> than the auto-generated commit list, so notes stay focused on user-facing
> changes.

## [Unreleased]

### Added
- **Start Page action keys fire on-screen.** Home Assistant toggle, media, and macro custom keys, plus the built-in HA slot keys (ha_1 to ha_5), now execute when pressed on the Start Page instead of asking for a connected Stream Deck. The server makes the Home Assistant call with the shared Stream Deck HA settings and the key shows the result in a toast. Macros run their HA slot and preset kitchen-timer steps (timers become shared server timers visible on every surface); deck-hardware steps are skipped and named in the toast.
- **AI token usage and budget.** Settings, AI now tracks the tokens your AI provider spends through the app (this month, all time, and per provider) and lets you set a monthly token budget for your own API key. When the budget is reached, AI photo import and barcode enrichment are declined until the next month or you raise it. Usage is metered locally per instance and is the foundation for cloud per-user quotas.
- **Per-device Home Assistant on-screen events + connection status.** Each device now decides for itself whether to show Home Assistant notifications and camera pop-ups (a per-device choice, handy for a headless server or picking which Pi Remote displays them), overriding the server default. The Home Assistant settings show a connection status badge (configured / connected / not reachable) on both the server and satellites, and satellites get a Test connection button.
- **On-screen Start Page (optional).** A new full-screen launcher that works like an on-screen Stream Deck, at /ui/start. Choose 6, 15, or 32 keys (the keys scale to fill the screen without scrolling), arrange them by dragging actions from a palette onto the grid, and it replaces the fixed and floating menus while shown. Custom buttons are shared with the physical Stream Deck. Off by default; enable it in Settings, Personalization, Start Page.
- **Weather page and settings.** The Stream Deck Weather settings are now just "Weather" and add an advanced weather-server option (the Open-Meteo API base, default the public service, so you can point it at a self-hosted instance). Weather also has its own navigation tab, so the forecast page is reachable without a Stream Deck.
- **Camera scan shows brand and resolution, and handles logins.** Scanning for IP cameras now labels each result with the detected brand and snapshot resolution, and a password-protected camera gets an inline username and password form that finds a working snapshot and lets you preview and add it.
- **Background image.** Set a photo behind the whole UI from Theme settings: upload an image or paste a URL, with an opacity slider so the interface stays readable. Applies on every page and device.
- **Named custom themes.** The custom theme builder now takes a name and saves your palette as its own entry in the Theme dropdown, so you can keep several and switch between them. A saved theme applies everywhere, including the Settings page itself, and can be deleted from the builder.
- **Reset navigation to defaults.** The navigation editor has a Reset to defaults button that restores the original tab order, folder grouping, and visibility in one click.
- **Hide the on-screen nav menu.** Settings, Navigation now has an On-screen nav menu option: Auto (the default) hides the nav bar on a Stream-Deck kiosk at large or extra-large scale, where the deck does the navigating and a small panel is better used for content; Always show and Always hide are also available. The top bar keeps a hamburger menu so Settings is always reachable.
- **Update "last checked" time, timezone, and maintenance controls.** Backup & Updates now shows when updates were last checked, adds a Date & time section to set the timezone used for timestamps (default follows the system NTP-synced clock; Pi Remotes inherit the timezone from the main server), and a Maintenance section with Reload settings (re-reads settings and rebuilds the AI/Mealie clients without a restart), Reboot now, and an optional nightly reboot schedule for a kiosk appliance.

### Changed
- **Project moved to the Syracuse3DPrintingOrg organization.** The repos now live under github.com/Syracuse3DPrintingOrg and the published Docker image moved to ghcr.io/syracuse3dprintingorg/pantryraider. Existing devices keep working: GitHub redirects the old repo URLs, and the updater rewrites a deployed compose file's legacy image reference on the next update (a device updating from a pre-move version needs two Update presses; the first refreshes the updater, the second migrates and pulls the new image).
- **Interactive demo refreshed.** The browser demo (docs/demo, deployed to Cloudflare) now carries the Pantry Raider branding (raccoon logo, favicon, watermark background), uses the current recipe tier names, and adds a Start Page screen showing the on-screen launcher whose Home Assistant, media, and macro keys fire without a Stream Deck.
- **README screenshots refreshed.** All six screenshots now show the current Pantry Raider UI (raccoon branding, current navigation, watermark background) and are regenerated by a new scripts/capture-screenshots.py, which boots the app against a built-in mock Grocy/Mealie with demo data and captures the pages with headless Chromium.
- **/ui shows the first page in your nav menu.** Visiting the app no longer always lands on the inventory dashboard; it opens whatever page leads your navigation menu. Combined with the Start Page defaulting to the top of the nav when enabled, you can make the Start Page your home screen.
- **Weather settings moved onto the Weather page.** Weather no longer has its own settings menu section; set the location, units, and (advanced) weather server with the gear button on the Weather page. The Stream Deck weather key still uses the same values.
- **Start Page editor matches the Stream Deck.** The Start Page editor now uses the same key catalog (all the same keys, grouped and coloured), the same shared custom-key library, and the same key style and icon options as the Stream Deck, with the roomier Start Page key grid. Both editors now read the exact same action catalog (the live one from the deck host bridge), so the palettes and keys are identical. The full-screen Start Page renders the keys in the same coloured deck style. Custom keys are one shared library: build or edit a key on either side and it shows up on both, with the Stream Deck placements preserved.
- **Combined Start Page and Stream Deck menu.** Settings now has one "Start & Stream Deck" item with a toggle at the top (like Settings vs Personalization) to switch between the on-screen Start Page and the physical Stream Deck, which share the same custom-button library. The Start tab also appears in the main navigation automatically when the Start Page is enabled.
- **Settings and Personalization split into clearer menus.** The Interface pane is now two separate menu items, Theme and Navigation. Display and Stream Deck moved from Settings into the Personalization menu, and the live attached-hardware detection moved from the Stream Deck pane to the Hardware pane where hardware detection belongs.

### Fixed
- **A portrait kiosk no longer runs off the right edge of the screen.** On a rotated 7-inch panel the bottom navigation bar overflowed sideways with half its icons unreachable, and the Inventory and Cook toolbars pushed the whole page onto a horizontal scroll. The nav bar now wraps its icons onto extra rows on a narrow screen, the toolbars wrap instead of overflowing, and in kiosk mode the page itself never scrolls sideways at any rotation or interface scale.
- **No more Wi-Fi setup banner on a wired device.** A Pi connected over ethernet could still show "Running in Wi-Fi setup mode" (and keep broadcasting the FoodAssistant hotspot) if the fallback hotspot had ever come up. The device now double-checks its real connectivity when the settings page asks about setup mode: with a wired link or any working network route, the hotspot shuts down on its own and the banner stays hidden.
- **Fresh Pi installs get Mealie by default again.** The install script quietly turned Mealie off on a fresh Pi Hosted (or server) install even though a hosted device is meant to ship with recipes and meal planning ready to go. Mealie now installs by default on a full local stack; set ENABLE_MEALIE=false at install time to skip it. New installs also record their enabled services in the stack's .env, so later docker compose commands and updates keep Mealie in the stack.
- **The first Mealie install survives interruptions.** Starting Mealie from Settings downloads its app image, which can take several minutes on a Pi. That download now keeps running on the device if you leave the setup page, the page reconnects to a start already in progress when you come back (instead of sitting idle until you click Start again), and if the device reboots or the helper restarts mid-download, the install resumes by itself. The Mealie section also says what to expect before you press Start.
- **Barcode scans no longer yank the kiosk to another page.** A USB barcode scanner types its code as a fast keystroke burst, and the keyboard nav shortcuts treated the first digit as a "jump to tab" press, so every scan flung the screen to Inventory (or whatever tab owned that number) before the scan could be processed. Number shortcuts now wait for a brief quiet moment before navigating, so scans are captured and routed by the scanner mode while deliberate single key presses still work.
- **Stream Deck weather tiles no longer show "No signal" while the weather page works.** The deck fetched wttr.in directly, which is often rate-limited; the tiles now get their forecast from the app, which prefers Open-Meteo (including a self-hosted weather server) and falls back to wttr.in. Per-key weather overrides also honor their own units now.
- **Display rotation survives a restart on devices imaged before late June.** Older images have a kiosk service that never re-applies the saved rotation on startup, and updates never refreshed it; the updater now patches that in. The rotation helper also waits longer for the display to come up on slow boots, where a 10 second window silently lost the rotation.
- **Clearer message when a device helper is out of date.** Pressing Reboot now on a device with an old helper daemon showed a bare "not found"; it now says to run Update and try again. (The reboot itself was fixed by the update fixes above: the helper daemon was frozen at an old version.)
- **Missing device tools now install themselves.** On a device imaged before a helper tool existed, display rotation, the Stream Deck screen-off key, and restore could fail because the tool was never installed. The device now reinstalls a missing tool from its own update source on the spot and carries on; only when that is impossible does it show a message saying what to press instead. The screen-off key also stops pretending it worked: when the display truly cannot be switched, the key shows Failed instead of Off, and touch calibration on a not-yet-updated device now says to press Update rather than "Helper not installed".
- **Touch now follows display rotation on never-calibrated screens.** On panels whose touch is accurate out of the box (like the official 7-inch DSI touchscreen), rotating the display left touch in the original orientation, because the counter-rotation was only applied on top of a saved calibration. Rotation now writes the correct touch matrix even when the screen was never calibrated, applies it on the reboot path too, keeps it consistent if the rotation command fails, and re-applies the right orientation after a calibration reset on a rotated display.
- **Updates now refresh every device helper, not just some.** The on-device updater used to refresh only itself, the restore helper, and the host bridge; the display power and rotation helpers were never installed or updated, so a device imaged before a helper existed never received it (stale rotation and missing screen-off controls on older Pis). Every update now syncs the full helper set, including helpers added after the device was imaged.
- **Updates recover from a rewritten GitHub history.** If the project history is force-pushed, the updater's fast-forward pull could never succeed again and the device was silently stuck on the old version forever. The updater now detects this, resets its update source to match GitHub exactly (the running app and its data are untouched), and reports the recovery in the update result.
- **Update check no longer shows a stale "up to date".** The version check now bypasses the GitHub raw CDN cache, so Check for updates sees a new release right after it is pushed instead of a few minutes later.
- **Theme contrast fixes.** Several bundled themes had unreadable spots: the selected side-menu item showed accent-on-blue (worst on iOS Light), the Save buttons were a washed-out cyan on light themes, and status badges used white text on bright backgrounds. These now use legible pairings on every theme. Also removed stray markup that had leaked into three theme stylesheets and was breaking their later rules.
- **Custom themes now take effect.** Selecting or saving a custom theme correctly recolours the whole app, including the Settings page, instead of appearing to do nothing.
- **Settings opens straight to the right menu.** The page no longer flashes the Settings menu and then jumps to Personalization on load.
- **Dragging a tab back into a folder works.** The navigation editor now nests reliably: the middle of a row drops a tab into a folder (the top and bottom edges reorder), and dropping onto any item already inside a folder adds it to that folder, so a tab moved out of a group can be dragged back in.
- **Waveshare resistive HDMI touchscreens now register.** A resistive Waveshare 3.5-4 inch HDMI panel uses an ADS7846 SPI touch controller that stays invisible until SPI and its overlay are enabled. Choosing the display type in setup after first boot never wrote those, so touch was dead and the kiosk reported "No touch device detected". Saving the display type now applies the overlay (with an Apply touch driver button under Settings, Display), and the wizard help points resistive panels at the ADS7846 option instead of the USB-touch one. A reboot loads the overlay.

## [0.7.0] - 2026-06-30

### Added
- **Quiet mode and a timer chime.** A finished kitchen timer now plays a short chime in the on-screen timer window so it carries across the room. A per-device Quiet mode toggle (Settings, Interface) silences it, leaving the highlighted timer row as the only signal, so one kiosk can be loud and another silent.
- **Release notes link and a manual server update.** The Settings Updates card links to the GitHub release notes on every deployment mode. A non-Pi server, which runs Watchtower on a daily poll, gains an Update now button that triggers Watchtower immediately so an available image is applied at once instead of waiting for the next poll, with the copy-paste commands kept as a fallback.
- **Recommended kitchen products (Shop tab).** A new Shop page recommends common kitchen products (appliances, cookware, gadgets, storage) as Amazon links. Items you have not marked as owned in your kitchen appliance list, and any equipment your active recipe needs but you lack, are pinned to the top; the rest are popular general picks. Add your own Amazon Associates tag under Settings > Recipes to monetize the links (qualifying purchases earn a commission); the tag is shared to satellites. The page carries the required Amazon Associate disclosure, and links open in a new tab. This is not an AI feature, so it works without any provider configured.
- **Drag-and-drop navigation editor with folders.** The Settings nav editor replaces the per-row parent dropdown with a tree you can rearrange: drag a row to reorder it, drop it onto a top-level tab (or use the indent button) to nest it, and outdent to bring it back. Because the kiosk is touch-only, every row also has move up, move down, indent, and outdent buttons. A new Add a heading control creates a folder (a label and icon with no page of its own) that groups other tabs into a dropdown; an empty folder stays hidden until it has children, and every page remains reachable.
- **Pending duplicate hint.** When a scanned item is already in Grocy inventory, the Pending page shows a small "Already in inventory (duplicate)" info badge on that row. It is informational only: the item can still be committed, and an item scanned on a different day lands as its own Grocy stock entry (Grocy keys entries by best-before date) so each keeps its own expiration.
- **Home Assistant on-screen notifications.** Turn on the event channel under Settings > Home Assistant and a Home Assistant automation can push notifications to this device's screen (a `rest_command` to `/events/notify`); they appear as toasts on the kiosk and in any open browser tab, coloured by level. The settings page shows the exact rest_command and automation YAML and has a Send test notification button.
- **Home Assistant camera pop-ups.** An automation can pop a camera up full-screen on the display (`/events/camera-popup` with a camera name), for example the doorbell camera when a person is detected. It shows for a configurable few seconds, then closes; it reuses the same camera proxy as the Camera page.
- **Convert has its own tab, and is customizable.** The Conversions page is now a normal navigation tab (hideable like any other), and a "My conversions" section lets you add your own quick-reference rows (for example "1 stick butter = 113 g") that stay on the device alongside the built-in cheat sheet and the calculator.
- **Stream Deck custom keys are a drag-and-drop library.** Custom keys are now created once in their own section (no slot number to type), and each appears as a chip in the palette under the grid. Drag it onto any key to place it, exactly like a built-in action; a custom key left unplaced is kept in the library for later. The grid shows each placed custom key's real face, and its row notes which key it sits on.
- **Stream Deck Home Assistant media keys.** A Media override type binds a key to a Home Assistant media_player and a transport action (play/pause, next, previous, volume up/down, stop). It fires the service on press with no on/off polling, reusing the shared Home Assistant connection.
- **Pantry audit.** A new Audit tab runs a read-only, location-scoped stock count: lock it to one storage location, scan the items there, and the page shows the expected stock (from Grocy) against what you scanned so missing and unexpected items stand out. Nothing is written back to Grocy. On a satellite the scans forward to the main server, so every surface sees one session. Audit is also a fourth barcode scanner mode (see below).
- **Nutrition tracker.** A new Nutrition tab logs what you eat with calories and macros (protein, carbs, fat) and shows daily and recent-day totals. When an AI provider is configured, an estimate button fills in the macros from a food name.
- **Kitchen Guide reference page.** A new Kitchen Guide tab collects quick kitchen reference material alongside the Convert tab.
- **Satellite update badge.** The Satellite Devices pane in Settings shows each remote's reported version against the main server's, with an up-to-date or behind badge so it is obvious which satellites need a `sudo foodassistant-update`. Each satellite reports its version on every config pull.
- **Finish setup from your phone.** When the setup wizard is opened on an attached kiosk display before setup is finished, it offers a phone or laptop URL (with a QR code) so you can fill the many text fields on a real keyboard instead of the touchscreen, with a Continue on this screen button if you prefer the kiosk. The URL uses the device's LAN host, not the kiosk's localhost.
- **Stream Deck barcode scanner modes.** A scan-mode key cycles the scanner context (Stock, Use, Shop, Audit) and shows the active mode on its face, so one physical scanner can add to inventory, consume stock, add to the shopping list, or run a pantry audit. The mode lives on the main server, so a satellite's deck and the server agree.
- **Version bump tooling.** `scripts/bump-version.sh [patch|minor|major]` edits the single source-of-truth `APP_VERSION`, and `scripts/install-git-hooks.sh` installs a pre-commit hook that auto-bumps the patch on each commit so every commit changes at least the patch number. The hook stays out of the way during rebases, merges, beads-only commits, and explicit minor/major bumps.
- **Kiosk display sleep with shared wake.** The kiosk screen now blanks after its idle timeout (previously a stored but unused setting) and wakes on a touch, key press, mouse move, or a Stream Deck button press. The display and the Stream Deck keep separate timeouts, but activity on either surface wakes both: the host bridge owns the display blanking and brokers activity between the screen and the deck. Manual blank/wake and the idle timeout are exposed through the bridge.
- **On-screen floating navigation menu.** An optional column of nav icons docked to a screen corner, handy on touch screens. Drag its handle to reposition it; it snaps to the nearest corner and remembers the spot per-device. A setting in Interface picks the default corner (or off), and it can auto-hide when a Stream Deck is connected, since the deck already provides navigation. The menu can be laid out vertically or horizontally per-device, and the page content is padded so the menu never sits on top of the text.
- **Restore from backup.** The Backup pane now has a Restore control that rebuilds this app's data (settings, database, staples) from a backup zip, the counterpart to Download Backup. The current data is copied aside first, archive paths are validated against zip-slip, and a redacted backup keeps the API keys you already have. Grocy and Mealie data are not touched (use the host script for a full snapshot).
- **Full Grocy + Mealie restore on a Pi.** On a Pi appliance the Backup pane gains a full-stack restore that runs through the host bridge: point it at a `.tar.gz` already on the device or an `rclone:` remote path, and the bridge stops the stack, swaps the data dirs aside, unpacks, and restarts. The archive is validated before the stack is stopped, and a failure mid-restore still brings the stack back up. This is distinct from the in-app app-data restore above.
- **Current Recipe.** A new On the Line tab (the active recipe) loads one active recipe (from a Mealie recipe, an imported recipe, or an AI-generated one) and keeps it on the server so every surface agrees. It shows the ingredients and steps, scales servings, and turns durations written in the steps (for example "simmer 20 minutes") into ready-to-start named timers. Timers live on the main server, so the web UI, an attached Stream Deck, and satellites share the same countdowns. A floating on-screen timer window shows running timers and steps aside when a Stream Deck is present, and the deck's timer keys auto-populate from the active recipe, labelled per step.
- **Launch a recipe as the Current Recipe.** A Cook button on each Recipes row and on the Cook page suggestion tiles (and a Cook this action in the AI recipe preview) makes that recipe the active Current Recipe, instead of Recipes only linking out to Mealie.
- **Recipe import from a file.** Import a recipe from a generic recipe JSON, a schema.org Recipe JSON-LD file, or a Mealie export, in addition to the existing import from URL and from a photo.
- **Custom AI prompt on the Cook page.** An optional, collapsible prompt box steers the AI suggestions and the full recipe the AI generates; empty means the default prompt.
- **More themes and a theme builder.** Three new built-in themes (Solarized, Midnight, Forest) plus a Custom theme builder in Settings > Interface that lets you pick your own palette swatches. Stream Deck key palettes follow the active theme.
- **Richer Stream Deck override editor.** The per-key override editor now previews each override on the grid in place, lets Home Assistant action keys set their own on/off colours and an icon, and lets a weather key show its forecast (high/low) tile.
- **On-demand camera feeds.** Configure camera feeds (a live HLS or MJPEG stream plus a still snapshot) under Settings > Interface > Cameras. A new on-screen Camera page shows the live feed, and a connected Stream Deck can show a snapshot key or splash the snapshot across the whole deck (a periodic still, not live video, since the deck is a slow USB-HID surface). Any key press exits the full-deck view.
- **Home Assistant lives on the server, with camera discovery.** The Home Assistant URL and long-lived access token are now set once under Settings > Interface > Home Assistant and stored on the main server, so they can be entered from the server or a Pi, are one source of truth, and are inherited by a second Pi remote without re-entering them. A Discover from Home Assistant button lists the instance's camera entities and adds them with their stream and snapshot URLs built for you. The Stream Deck Home Assistant keys reuse the same shared credentials.
- **Camera in the navigation.** When at least one camera is configured, a Camera entry appears in the navigation bar, the floating nav, and the overflow menu, so the live feed page is reachable without typing its URL. It hides itself again when no cameras are set.
- **Dedicated Home Assistant and Cameras settings pages.** Home Assistant and Cameras moved out of the Interface pane into their own entries in the Settings menu, so they are easy to find and have room to grow.
- **Add a camera by IP.** The Cameras page can build a network camera's stream and snapshot URLs from its address, with brand templates for Generic MJPEG, Generic snapshot, Reolink, Amcrest/Dahua, Hikvision, and ONVIF, plus a Custom path. It fills a camera row you can review and edit before saving. RTSP-only cameras still need an MJPEG/HLS source or a transcoder, which the page notes.
- **Choose which camera a Stream Deck key shows.** A new Camera override type in the per-key editor binds a key to a specific configured camera (by name) instead of always the first one, and an optional Full deck flag makes that key splash the chosen camera across the whole deck on press. Several camera keys can each show a different feed.
- **Weather page on the display.** Pressing a Stream Deck weather or forecast key now opens a full forecast page on the attached kiosk display (in addition to cycling the key face), so the deck doubles as a remote for the screen. The page is reachable at /ui/weather and uses the same location and units as the deck weather widget. The forecast comes from Open-Meteo (free, no key) with wttr.in kept as a fallback, since wttr.in is frequently rate-limited and was the likely cause of the page reading "unavailable".
- **Custom navigation tabs and nested submenus.** Settings > Interface can now add your own top-level navigation entries (a label, icon, and a root-relative or external URL) and nest tabs under a parent so the bar groups into dropdown menus. Both built-in tabs and custom tabs can be nested one level deep, and the existing order and hidden-tab controls still apply. Navigation layout is per-device, so each kiosk can arrange its own menu.
- **Fleet-wide automatic updates.** An "Install updates automatically" setting (on by default) now drives updates across a whole deployment. A Pi appliance applies updates through the host-bridge over-the-air helper; a non-Pi server applies them through the bundled Watchtower container. The flag is a single global setting that Pi Remotes inherit from their main server, so a server and its satellites converge on the same version instead of drifting apart.
- **In-app updater on Pi Hosted.** The in-app update control now works on a Pi Hosted appliance, not only a Pi Remote, so a full-stack Pi can check for and apply an over-the-air update from its own Settings page.
- **Debug logging with a downloadable bundle.** A debug logging toggle under Settings > Security raises the app log level and writes a rotating log file under the data directory. A Download control hands you that log for support, with secret values redacted. It is off by default.

### Fixed
- **Custom camera Stream Deck keys open their own camera.** A camera key set to a specific camera showed the right glyph but opened the first camera on the kiosk screen, because the press dropped the camera name and the Camera page always started on camera 0. The key now carries its camera through as a `?cam=` query param and the Camera page opens that feed (by name or index), falling back to the first camera only when none is requested.
- **"With pantry staples" recipes show up now.** That Cook bucket was usually empty because real recipe ingredients carry measurement and quantity words ("3 tablespoons unsalted butter", "1 teaspoon kosher salt"); those extra words stopped common pantry items from being recognised as staples, so the recipe fell into "needs shopping" instead. Measurement and quantity words are now ignored when matching staples, and the built-in staples list is broader, so recipes you can make from stock plus pantry basics land in the right bucket.
- **Wired Pi no longer drops into Wi-Fi setup mode.** The fallback setup hotspot used to start whenever Wi-Fi was not associated, even on a Pi with a working Ethernet connection. It now stays off when any other interface provides connectivity (a default route, or a wired interface that is up with an IP). The Network pane also shows an Ethernet "Connected" badge instead of reading as offline when Wi-Fi is idle.
- **Home Assistant cameras now display.** HA camera feeds showed "Camera unavailable" because the discovered URLs put the long-lived token in the query string, which Home Assistant rejects (it wants an Authorization header a browser cannot send). Cameras are now bound to their HA entity and fetched with the proper bearer header: the app proxies them for the on-screen Camera page, and the Stream Deck fetches them directly with the header. Cameras you already added are recovered automatically from their stored URL, so no re-adding is needed.
- **Phone QR code stays in kiosk mode.** The QR code that opens the UI on a phone is no longer hidden in kiosk mode, where it is most useful (scan the wall-mounted screen to control it from your phone).
- **Home Assistant and cameras now sync to a Pi Remote.** A satellite mirrors the main server's Home Assistant credentials and camera feeds, and its Settings show them read-only with a "configured on the main server" note (like the Stream Deck weather), so the values are visible and clearly server-managed instead of looking unset. Update the satellite (`sudo foodassistant-update`) so it pulls the new fields.
- **Kiosk overflow menu was nearly empty.** In kiosk mode the three-dots More menu hid everything except Settings (the reference links are kiosk-hidden and the secondary-tab copies only appeared under 820px). The secondary destinations (Recipes, Cook, On the Line, Meal Plan, Camera) now show in that menu in kiosk mode at any width, so every page stays reachable from the kebab.
- **Display scale applies without a reboot.** Changing the display scale or orientation from a phone or laptop now restarts the kiosk browser so the attached display picks it up right away, instead of waiting for a reboot.
- **Touch calibration page loads.** The full-screen touch-calibration page was crashing (a deprecated template call), so calibration could never start; it renders now.
- **Satellites survive a flaky mDNS.** A Pi Remote caches its main server's LAN IP on each successful sync and falls back to it automatically when the configured `.local` name stops resolving, so the satellite stays wired to its server on networks that block or drop multicast DNS. Device discovery (the Scan LAN button) was already IP-based, and the co-hosted Grocy/Mealie browser links already prefer the LAN IP.
- **Over-the-air Pi updates now refresh the Stream Deck too.** The update helper redeploys both the web app and the Stream Deck controller package (previously only the app), reinstalls Python dependencies only when they changed, restarts both services, and is safe to re-run after a manual `git pull`.
- **No mouse cursor on a touch kiosk.** Fullscreen Chromium painted its own arrow over the page on a touch-only kiosk display. The kiosk stylesheet now hides the cursor over web content, so a wall-mounted touchscreen shows no stray pointer.

### Changed
- **Settings menu reorganized into logical groups.** The Settings sidebar now groups its sections under Services, App, Devices & Hardware, and System headers instead of a flat list, so related settings sit together and the flow reads top to bottom. The satellite (Pi Remote) and Pi-only visibility rules are unchanged: a satellite still shows Main Server in place of the backend services, and Display/Stream Deck/Network stay Pi-only.
- **Per-section saving in Settings.** The single "Save All" button is gone. Each settings section now has its own Save button that stores only that section's fields, so saving is explicit and scoped and one section's edits never carry another's. Options that apply instantly (like the theme preview and per-device toggles) keep working as before.
- **Cook suggestions match pantry staples better.** Recipes whose ingredients carry descriptor words (for example "parmesan cheese" or "grated parmesan" against a "Parmesan" staple) now match, so the "Ready to cook" and "With pantry staples" buckets populate from current stock instead of coming back empty.
- **Readable Stream Deck key labels.** Label text colour is chosen from each key's background brightness, so themed keys (for example a light-green Commit) stay legible instead of washing out white-on-light.
- **Co-hosted Grocy/Mealie browser links use the LAN IP.** The "Open Grocy/Mealie" links now prefer the device's LAN IP over its `.local` name, so they work on networks where mDNS does not resolve. The loopback address is still used for the behind-the-scenes API wiring.
- **AI options hide when no AI is configured.** The Ask AI button, recipe-from-photo import, the Add page's Photo/Receipt tab, and other AI-only affordances are hidden across the UI until a vision/LLM provider is set up, so the interface never offers actions that cannot work.
- **Small-screen kiosk view.** On small screens (for example an 800x480 panel) the secondary nav tabs collapse into the overflow menu with larger touch targets, and the layout simplifies to a single column. On a Pi with a display attached, kiosk mode now enables itself (respecting an explicit choice to turn it off).
- **Barcode enrichment model picker.** The enrichment model is now a provider-aware dropdown (matching the main AI model picker) with a free-text override, instead of a plain text box.
- **Cook suggestion factors can be toggled.** Each suggestion factor on the Cook page has an on/off checkbox; an unchecked factor is dropped from the request.
- **Stream Deck weather and forecast keys cycle.** Pressing the weather key cycles through stats and the forecast key cycles through days, each returning to its default after a short idle.
- **AI Declarations moved.** The standalone AI Declarations page is gone; the same content now lives in a section of the About page and in `docs/AI_DECLARATIONS.md`.
- **Cook icon unified.** Cook uses a flame icon consistently across the web UI and the Stream Deck.
- **Pi setup matches the board.** On a low-RAM or older Raspberry Pi (a Pi 3, Zero, or under about 4 GB of RAM), the setup wizard now offers Pi Remote only and hides Pi Hosted, since a full local stack (Grocy plus optional Mealie and Ollama) needs more than those boards have. A capable Pi 4 or 5 still offers both, and uncertain detection never over-restricts a box.

### Security
- **Web-UI password and kiosk PIN hashed at rest.** The login password and kiosk PIN are now stored as salted scrypt hashes instead of plaintext, so a leaked settings.json or backup does not expose them. Existing plaintext values still work and are upgraded to a hash on the next successful login. API keys and the TOTP secret stay as they are, since they are bearer secrets that must be presented verbatim.
- **Community health and supply-chain hardening.** Added SECURITY.md (a private vulnerability disclosure policy), CONTRIBUTING.md, CODE_OF_CONDUCT.md, issue and pull request templates, a Dependabot config (pip, GitHub Actions, Docker), a pre-commit config running ruff, test-coverage reporting in CI, and pinned every GitHub Actions reference to a commit SHA.

### Build
- **Hash-locked dependency file for reproducible builds.** A new `service/requirements.lock` resolves the full transitive dependency tree with `--generate-hashes`, so installs can be verified against known checksums. It also pins the previously floating `anthropic` dependency to a concrete version. The lockfile is additive: the Docker image still installs from `service/requirements.txt`. The README documents how to regenerate it with uv or pip-tools.
- **MkDocs site over the existing docs.** A root `mkdocs.yml` wires the files under `docs/` into a browsable site with the Material theme and a nav. It does not change any documentation content. MkDocs and its theme are dev-only tools and are not added to the runtime requirements; preview locally with `mkdocs serve`.

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
- **Deployment modes in setup.** The first setup step now asks how the device is used. On a Raspberry Pi you choose **Pi Hosted** (everything runs on the Pi, with or without a screen) or **Pi Remote** (a thin control surface that drives a Stream Deck and/or kiosk pointed at a Pantry Raider server already running elsewhere); on other hardware it stays **Server hosted**. Pi Remote installs no local Grocy or Docker, so it runs on a Pi 3, and the wizard skips the Grocy and AI steps for it. The choice is detected and offered automatically based on the board.
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
- **Ready-to-flash appliance image.** A prebuilt Raspberry Pi OS Lite image with the Pantry Raider provisioner baked in is published to the Releases page. Flash it with Raspberry Pi Imager, set wifi in the Imager GUI, and boot: no config files, no terminal. The device auto-detects an attached display (launches the kiosk) and a plugged-in Stream Deck, and takes its timezone from the OS.
- **Stream Deck controller** (`streamdeck/`). Drive an Elgato Stream Deck or embedded Stream Deck Module (6, 15, or 32 keys) as a physical control surface. Keys show live counts (items expiring soon, scans waiting to commit) and trigger actions like committing pending scans or steering the attached kiosk browser. Key text is large and legible, scales to each deck's pixel density, and the deck can be rotated 0/90/180/270 degrees. Includes a systemd unit, udev rule, and example config. See `streamdeck/README.md`.
- **Optional AI.** Pantry Raider now works without an AI provider. Inventory, expiry tracking, manual entry, and barcode lookup via Open Food Facts all keep working; photo import, receipt scanning, and recipe suggestions are simply off until you add a provider. Choose "None" in the setup wizard or **Settings → AI**.
- **Guided setup wizard.** First-time setup is now a step-by-step flow (welcome, security, Grocy, AI, optional integrations, done) with clear required fields, instead of one dense form.
- **Attached-display settings.** A scale (Small to Extra large) and orientation (0/90/180/270) control for a hardware screen wired to the appliance. These apply only to the kiosk display, never to a phone or laptop browsing the app.
- **About & Credits page** listing the open-source projects Pantry Raider builds on (Grocy, Mealie, Open Food Facts, TheMealDB, and more), with links and a note to support them.

### Changed
- Default Gemini model is now `gemini-2.5-flash` (the old `gemini-1.5-flash` default is no longer available).
- README rewritten with a "Why Pantry Raider?" section; the full API reference moved to `docs/api.md`.

### Fixed
- **Theme and display scale save immediately.** Picking a theme or scale in **Settings → Interface** applies and persists right away instead of waiting for **Save All**.
- QR code is now scannable on dark themes (white background), and the QR modal closes reliably.
- The appliance first boot no longer clobbers Raspberry Pi Imager's wifi/SSH/user setup (our provisioner script was renamed to avoid the collision).

## [0.4.0]

### Added
- **Remote Access**: expose your Pantry Raider to the internet without port-forwarding. In **Settings → Remote Access**, choose Cloudflare Tunnel (free, bring your own token) or Pantry Raider Cloud (managed subscription, coming soon). The tunnel runs as a sidecar container; your public URL appears in the UI once the connection is established.
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
