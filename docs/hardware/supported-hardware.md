# Supported Hardware

FoodAssistant runs as a set of Docker containers, so it works on most 64-bit Linux
hardware. This page lists what's officially tested and the minimum specs to expect a
good experience.

> Looking to flash a ready-to-go image instead of installing manually? See the
> [SD-card image guide](sd-image.md).

## Minimum requirements

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| Architecture | ARM64 (aarch64) or x86-64 | — |
| RAM | 2 GB | 4 GB |
| Storage | 16 GB | 32 GB+ (Mealie recipe images add up) |
| OS | 64-bit Linux with Docker + Compose v2 | Raspberry Pi OS Lite (64-bit) / Debian / Ubuntu Server |
| Network | Ethernet or Wi-Fi | Ethernet for the always-on box |

**RAM guidance:** FoodAssistant + Grocy run comfortably in 2 GB. Adding **Mealie**
(recipes/meal plan/shopping) pushes the practical floor to 4 GB, especially during meal
planning. Local AI via **Ollama** is **not** recommended on low-RAM SBCs — use a cloud
AI provider, or a machine with 16 GB+ if you want fully local inference.

## Tested boards

| Board | RAM | Status | Notes |
|-------|-----|--------|-------|
| Raspberry Pi 5 | 4 GB / 8 GB | ✅ Supported | Recommended. DSI touch display support. |
| Raspberry Pi 4B | 4 GB / 8 GB | ✅ Supported | Solid; 2 GB works for Grocy-only setups. |
| Raspberry Pi 4B | 2 GB | 🟡 Limited | FoodAssistant + Grocy only. Mealie may be tight. |
| Generic x86-64 mini PC (N100, etc.) | 8 GB+ | ✅ Supported | Runs everything; best for local Ollama. |
| Raspberry Pi 3B+ | 1 GB | ❌ Unsupported | Insufficient RAM for the full stack. |
| Raspberry Pi Zero 2 W | 512 MB | ❌ Unsupported | Insufficient RAM. |

> **Status key:** ✅ Supported (tested, expected to work well) · 🟡 Limited (works with
> caveats) · ❌ Unsupported (known inadequate).

This matrix is filled in from real testing on the SD-card image. If you've run
FoodAssistant on hardware not listed here, please open an issue with your results.

## Peripherals

### Barcode scanners

Any **USB HID ("keyboard wedge") barcode scanner** works with no configuration — it
types the scanned code into the focused field. This covers most wired and wireless USB
scanners, including compact OEM scan-engine modules. 1D (UPC/EAN) and 2D (QR/DataMatrix)
are both supported as long as the scanner reads them.

The camera-based scanner in the web UI also works on any device with a camera (e.g. your
phone), no dedicated hardware required.

### Displays

A display is optional — FoodAssistant is a web app you can reach from any browser on your
network. For a dedicated touchscreen setup, DSI and HDMI capacitive touch panels both
work; see the [SD-card image guide](sd-image.md) for kiosk-mode setup.

### Stream Deck controllers

An Elgato Stream Deck, or an embedded Stream Deck Module, can act as a physical
controller. Its keys show live counts (items expiring soon, scans waiting to
commit) and trigger actions such as committing pending scans or opening a page
on the attached display. It can sit next to a touchscreen or be the only
interface on a headless box.

| Device | Keys | Status | Notes |
|--------|------|--------|-------|
| Stream Deck Mini / Module 6 | 6 | Supported | Extra actions move to further pages via a "More" key. |
| Stream Deck / MK.2 / Module 15 | 15 | Supported | Roomy default layout. |
| Stream Deck XL / Module 32 | 32 | Supported | Plenty of spare keys. |

Setup, configuration, and the controller service live in
[`streamdeck/`](../../streamdeck/README.md). The connection is plain USB and
the driver is pure Python, so no Elgato software is involved.

### Cameras / photos

No dedicated camera is needed. Use your phone's browser to photograph food items and
receipts, the native camera opens directly from the web UI.

## AI providers and hardware

AI features are optional. If you want **fully local** AI (Ollama), you need significantly
more RAM (16 GB+ recommended) and ideally x86-64 with a capable CPU/GPU; vision models are
slow on SBCs. Otherwise, configure a cloud provider (Gemini/OpenAI/Anthropic) in the setup
wizard and any supported board above is fine.
