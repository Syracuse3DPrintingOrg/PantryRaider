# Feature maturity

Pantry Raider does a lot, and not every part is equally seasoned. Some
capabilities have been in daily use since the first release; others shipped
recently and are still settling in. This page rates each feature so you know
what to lean on and what to treat as newer ground.

Ratings reflect how long a feature has existed, how much of the app depends on
it, how broad its automated test coverage is, and how much it varies with your
specific hardware or an outside service. They are not a promise: a Stable
feature can still have a bug, and an Experimental one may work perfectly for
you.

## The scale

- **Stable.** Long-established and used in production installs. Well covered by
  tests and unlikely to change under you. Rely on it.
- **Beta.** Works and is in real use, but it is newer or has fewer miles on it.
  Expect the occasional rough edge, and watch the changelog for refinements.
- **Experimental.** Recent, narrow, or dependent on hardware or an outside
  service that varies. Try it, but keep a fallback and do not build a routine
  around it yet.

The rating is about how proven the feature is, not how useful it is. A feature
can be genuinely handy and still be marked Beta because it only shipped a few
releases ago.

## Inventory and recipes

| Capability | Maturity | Notes |
|---|---|---|
| Inventory dashboard (Grocy) | Stable | The oldest and most exercised part of the app: storage panels, drag-and-drop moves, inline edits, expiry badges. Grocy itself is the battle-tested backbone. |
| Custom storage locations | Stable | Buckets beyond the four built-ins have been in since 0.1.0 and are widely used. |
| Expiry defaults (rules table) | Stable | The editable best-by rules by product type; every scan path runs through them and they are covered by tests. |
| Barcode lookup (Open Food Facts) | Stable | Camera, USB/wireless scanner, or manual entry, backed by Open Food Facts. Established and well tested; product data quality depends on the Open Food Facts catalog. |
| Consume by barcode | Stable | Scanning to consume stock, including linking an unrecorded barcode to its product on the fly. Covered by tests. |
| Recipe suggestions (what can I cook) | Stable | Ranking your recipes by in-stock coverage, with expiring items floated up. Long-standing; the staple-matching was refined in 0.7.0. |
| Mealie integration (meal plan, shopping) | Stable | Week view, shopping list with check-off, inventory-aware suggestions. The client auto-detects Mealie v1 and v2 API paths. |
| Grocy-backed shopping list (no Mealie) | Stable | The Shopping tab falls back to Grocy's own list when Mealie is not configured. |
| Recipe import (URL, photo, file, TheMealDB) | Beta | Import from a webpage, a photo, a recipe file (generic JSON, schema.org JSON-LD, or a Mealie export), or TheMealDB. Broad by design, so results vary by source; the file-import paths arrived in 0.7.0. |
| On the Line (Current Recipe) | Beta | The active-recipe surface with servings scaling and step timers, shared across screens. Landed in 0.7.0 and refined since; newer than the core inventory flow. |
| Nutrition tracker | Beta | Logging calories and macros with daily totals arrived in 0.7.0. The optional AI macro estimate needs a provider and is best treated as a starting point. |
| Web recipe suggestions (Spoonacular) | Experimental | External recipe suggestions depend on a third-party API and your key; useful, but the least predictable of the recipe sources. |
| AI recipe generation | Experimental | Writing a recipe from a dish name depends entirely on your AI provider; quality varies by model. |

## Scanning and AI

| Capability | Maturity | Notes |
|---|---|---|
| Manual entry and barcode scanning | Stable | Core intake paths, in since the first release and heavily tested. |
| Scanner modes (stock / use / shop / audit) | Beta | One scanner context shared across every surface and Stream Deck. Solid and tested, but the shared-mode plumbing is newer than the scan itself. |
| Pantry audit (read-only stock count) | Beta | A location-scoped count compared against Grocy stock, never written back. Shipped in 0.7.0 with test coverage; still gathering real-world miles. |
| Photo analysis (vision) | Beta | Extracting name, brand, quantity, and printed dates from a photo. Reliable with a good cloud model; quality drops with smaller local models. Requires an AI provider. |
| Receipt import | Beta | Extracting food line items from a photographed receipt. Works well on clean receipts, less so on faded or unusual ones. Requires an AI provider. |
| Barcode name enrichment (LLM) | Beta | An optional LLM pass that cleans up messy product names. Optional and falls back cleanly when off. |
| LLM shelf-life and storage estimate | Experimental | Asking the AI for a realistic best-by window and storage location (added in 0.13.0). Off by default, falls back to the category rule, and a printed date still wins. Genuinely new. |
| AI providers: Gemini / OpenAI / Anthropic | Beta | The hosted vision and text providers. Well structured and swappable; each depends on that vendor's API and your key. |
| AI provider: Ollama (fully local) | Experimental | Fully local vision and text with no external calls. Works, but quality and speed depend heavily on your model and hardware. |
| AI token usage and cost estimate | Beta | Usage counters with an approximate cost, added in 0.8.0. The cost figure is an estimate, not a bill. |

## Kiosk and display

| Capability | Maturity | Notes |
|---|---|---|
| Kiosk / touch mode | Stable | Touch-optimized sizing and auto-enable on a Pi with a display. In wide use across appliance installs. |
| On-screen keyboard | Beta | A touch keyboard for wall-mounted panels, added in 0.8.0. |
| Weather page | Beta | A full forecast page from Open-Meteo with a wttr.in fallback. The parse logic is tested; forecasts depend on those free services being reachable. |
| Camera feeds | Beta | On-screen network camera viewing, including a proxy for Home Assistant cameras. Broad camera-brand support means results vary by camera; RTSP-only feeds still need an MJPEG/HLS source. |
| Kiosk screensaver (logo / photo slideshow) | Beta | The bouncing-logo and USB photo-slideshow screensaver with floating timer pills, built out across 0.7.0 and 0.8.0. |
| Floating nav and timer chips | Beta | The optional on-screen nav column and floating timer window, per-device. Newer conveniences on top of the stable page nav. |
| Display sleep and shared wake | Beta | Screen blanking with wake shared between the display and Stream Deck, brokered by the host bridge. Pi-specific and tied to the bridge. |
| On-screen Start Page | Beta | The optional full-screen action grid mirroring the Stream Deck. Its live key content was only completed in 0.13.1. |
| Custom navigation (reorder, hide, folders) | Beta | Per-device nav layout with nested submenus and custom entries, matured through 0.7.0. |
| Themes and custom theme builder | Beta | Built-in themes plus a palette builder. The Pantry Raider brand theme became the default only in 0.13.0, so the default look is recent. |
| Shared kitchen timers | Stable | Server-side timers shared across the page, floating window, Stream Deck, and satellites. The registry is well tested and derives countdowns from epoch deadlines so every surface agrees. |
| Display rotation and touch calibration | Experimental | Framebuffer and CSS rotation and resistive-touch calibration. These depend on the exact display and panel, and have needed the most per-device fixing (see the changelog). Expect to fine-tune for your screen. |

## Stream Deck

| Capability | Maturity | Notes |
|---|---|---|
| Stream Deck controller (6 / 15 / 32 keys) | Beta | The physical control surface with live counts, nav keys, and large legible labels. Mature in design and tested, but tied to specific Elgato and module hardware, so real-world behavior varies by model. |
| Timer keys | Beta | Countdown keys shared with the server timers; refined repeatedly through 0.7.0 and 0.8.0. |
| Custom key library (drag-and-drop) | Beta | Building your own keys (HA actions, timers, weather, cameras, media, macros) in a palette and dropping them on the grid. Powerful and tested; the drag-and-drop editor is newer. |
| Named key profiles | Beta | Saved per-size layouts on the main server, mirrored to satellites. |
| Themed keys and readable labels | Beta | Key colors follow the active theme with contrast-aware label text. |
| Camera and media keys | Experimental | Showing a camera snapshot on a key (or across the whole deck) and firing Home Assistant media transport actions. These lean on cameras and HA, which vary by setup. |

## Home Assistant

| Capability | Maturity | Notes |
|---|---|---|
| REST sensors and Lovelace dashboard | Stable | The sensor config, automations, and dashboard have been part of the project since early on. Remember to use the LAN URL for headless sensor requests. |
| Barcode scanner automations | Stable | The keyboard_remote based scanner path, established and documented. |
| Shared HA credentials on the server | Beta | Storing the HA URL and token once on the main server and inheriting them on satellites, added in 0.7.0. |
| On-screen HA notifications and camera pop-ups | Beta | Pushing toasts and full-screen camera pop-ups to the display through the event channel. Newer (0.7.0) and off by default. |
| HA camera discovery | Experimental | Listing HA camera entities and adding them automatically. Depends on your HA setup and how each camera exposes its stream. |

## Forager cloud

Forager is Pantry Raider's optional hosted companion. The self-hosted app works
fully without it; Forager only adds to a linked install. The whole platform is
recent (it built out across 0.4.0 and 0.9.0), so nothing here is rated Stable
yet.

| Capability | Maturity | Notes |
|---|---|---|
| Managed AI proxy | Beta | AI photo analysis, receipt parsing, and barcode enrichment without your own key, metered against a token quota. Functional and tested, but young. |
| Accounts, sign-in, kitchen linking | Beta | Password login, optional Google sign-in, and one-step linking of an install to an account. |
| Two-factor authentication (Forager) | Beta | TOTP on the Forager account, including for sign-in from outside your home network. Recent. |
| Billing and subscriptions (Stripe) | Experimental | The trial, plan tiers, and Stripe webhook entitlements. It works, but it is the newest money-handling path and should be treated as early. |
| Remote-access tunnel | Experimental | Reaching your kitchen from anywhere over Forager, on a server or a Pi. Depends on the tunnel and your network; newer and less proven than local access. |

Note: local, self-hosted two-factor authentication (TOTP on the app's own
password login) is separate from Forager and is more established. It has been in
since the first release.

## Provisioning and updates

| Capability | Maturity | Notes |
|---|---|---|
| Docker Compose stack (server) | Stable | The plain Compose deployment on a NAS, mini PC, or VM. The most reproducible way to run the app. |
| Web setup wizard | Stable | Guided first-time setup with live connection tests. Reworked into steps in 0.5.0 and well exercised. |
| On-device Pi installer (`install.sh`) | Beta | The one-line SSH installer that detects the board and hardware. Newer than the server path and inherently more variable across boards. |
| Pi appliance and host bridge | Beta | The full Pi Hosted appliance with the root helper at `127.0.0.1:9299`. Solid on the tested boards; behavior depends on your exact Pi and peripherals. |
| Satellite (Pi Remote) mode | Beta | A thin client that pulls backend config from the main server. Tested, including the sync and read-only panes, but depends on a healthy LAN and mDNS. |
| Ready-made SD-card image | Beta | The prebuilt flashable image. Convenient, but tied to the boards it was built and tested for. |
| Over-the-air updates (Watchtower / host bridge) | Beta | Fleet-wide auto-update: Watchtower on a server, the host-bridge OTA on a Pi, satellites following the server. Reworked and hardened through 0.8.0, so still maturing. |
| Backup and app-data restore | Stable | Downloading a backup zip and restoring app data (zip-slip guarded, secrets preserved). Established and tested. |
| Full Grocy + Mealie restore (Pi) | Beta | The host-bridge full-stack restore from a path or `rclone:` source. Newer and Pi-specific. |
| Off-box scheduled backup (rclone) | Beta | Optional scheduled backups to a remote. Depends on your rclone remote being configured and reachable. |

## Other

| Capability | Maturity | Notes |
|---|---|---|
| Unit converter and Kitchen Guide | Stable | The measurement cheat sheet, calculator, and saved conversions. Self-contained and low-risk. |
| Recommended products (Shop tab) | Beta | Amazon product recommendations with affiliate links; added in 0.7.0. Not an AI feature and works without a provider. |
| Interactive browser demo | Experimental | The self-contained demo under `docs/demo`. It is a walkthrough with no backend, kept in sync by hand, so it can lag the app and is not a test of the real features. |

## Keeping this current

This matrix is maintained as features harden. When a capability gains broad test
coverage and a track record across real installs, its rating moves up; when a
new feature ships, it starts at Beta or Experimental and earns its way to
Stable. If your experience differs from a rating here, that is useful signal, so
please open an issue.
