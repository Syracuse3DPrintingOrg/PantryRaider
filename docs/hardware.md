# Hardware

This page covers the physical hardware FoodAssistant runs on and the peripherals
it integrates with: single-board computers, displays and touch panels, Stream
Deck controllers, the optional accelerometer, and barcode scanners.

For minimum specs and the board test matrix, see also
[Supported Hardware](hardware/supported-hardware.md). For flashing the ready-made
SD-card image, see the [SD-card image guide](hardware/sd-image.md). For where each
piece runs (server vs Pi Hosted vs Pi Remote), see [Platforms](platforms.md).

## How to read the support levels

Two levels are used below:

- **Officially supported / tested.** Exercised on the SD-card image and the
  first-boot provisioner, and expected to work without extra fiddling.
- **Should work / community.** Standard hardware classes that follow the same
  interface (USB HID, generic HDMI, SPI ADS7846). These usually work but have not
  all been individually tested. If you run something not listed here, please open
  an issue with your results.

## Single-board computers

The SD-card image targets Raspberry Pi OS Lite (64-bit), so the boards below are
the officially supported path. The first-boot provisioner detects a Pi by reading
the device-tree model and degrades gracefully on other ARM64 / x86-64 Debian or
Ubuntu systems.

Officially supported / tested:

- Raspberry Pi 5 (4 GB / 8 GB). Recommended.
- Raspberry Pi 4B (4 GB / 8 GB). 2 GB works for Grocy-only setups.
- Generic x86-64 mini PC (for example an N100 with 8 GB or more). Best choice if
  you want fully local AI with Ollama.

Should work / community:

- Other aarch64 boards running 64-bit Debian or Ubuntu with Docker and Compose v2.

Not adequate for the full stack: Raspberry Pi 3B+ (1 GB) and Pi Zero 2 W
(512 MB). A Pi Zero or Pi 3 can still serve as a thin **Pi Remote** control
surface (kiosk and/or Stream Deck only, no local backend), since that mode runs
no Docker stack.

The architecture is 64-bit only. The image is aarch64, and the Docker images
(FoodAssistant, Grocy, Mealie, Ollama) are published for arm64 and amd64.

## Displays

A display is optional. FoodAssistant is a web app reachable from any browser on
your network, so a headless box is a perfectly normal setup. For a dedicated
kitchen panel, the appliance image runs a Chromium kiosk (via the `cage` Wayland
compositor) pointed at the local UI.

Display options:

- **HDMI panels.** Any standard HDMI display. The kiosk renders through DRM/KMS,
  so the boot console and the browser share one output.
- **Touch panels.** Configured by the first-boot `configure_touch` step, which
  supports two driver types plus auto-detection:
  - `ads7846`: SPI resistive touch, used on Waveshare HDMI LCD panels and many
    small Pi HAT screens. When active, the provisioner adds `dtoverlay=ads7846`
    and `dtparam=spi=on` to the Pi boot `config.txt`. Defaults are tuned for the
    Waveshare 3.5 inch to 4 inch HDMI LCD; the overlay cs, penirq, and speed
    values can be overridden in `config.env` for other layouts.
  - `usb`: USB HID touch, used by larger HDMI touch monitors that connect their
    touch surface over USB. These need no kernel overlay.
  - `auto` (the default): probes for an SPI bus / ADS7846, then for an existing
    HID touch input device, and picks the matching driver.

### Display rotation

`DISPLAY_ROTATION` accepts 0, 90, 180, or 270 degrees. The kiosk compositor
applies the matching transform, and the web UI / host bridge can change the
framebuffer rotation later without a reflash.

### Touch calibration

Touch axes are mapped with a libinput quirk written to
`/etc/libinput/local-overrides.quirks` using `AttrCalibrationMatrix`. This works
for both Wayland (cage / wlroots reads libinput directly) and X11, so there is no
need to run `xinput_calibrator`. The 6-value matrix is auto-derived from
`DISPLAY_ROTATION`, or you can set `TOUCH_CALIBRATION_MATRIX` in `config.env` to
override it. The default identity matrix is `1 0 0 0 1 0`.

## Stream Deck controllers

An Elgato Stream Deck (or an embedded Stream Deck Module) can act as a physical
control surface. Keys show live counts such as items expiring soon and pending
scans, and trigger actions like committing scans or opening a page on the
attached display. The connection is plain USB and the driver is pure Python, so
no Elgato desktop software is involved.

Models, by key count (matching the setup wizard options):

- Stream Deck Mini / Module 6: 6 keys. Extra actions move to further pages via a
  "More" key.
- Stream Deck MK.2 / Classic / Module 15: 15 keys. The roomy default layout.
- Stream Deck XL / Module 32: 32 keys.

The provisioner installs a udev rule matching Elgato USB vendor id `0fd9` so the
service user can open the device without root:

```
SUBSYSTEM=="usb", ATTR{idVendor}=="0fd9", GROUP="plugdev", MODE="0660"
```

The Python controller pins `streamdeck>=0.9.8`, because 0.9.5 does not recognise
the USB product id used on current XL / Module 32 hardware. Setup and the
controller service live in [`streamdeck/`](../streamdeck/README.md).

## Accelerometer (optional auto-rotation)

If an Adafruit LSM6DSOX accelerometer is wired to the Pi's default I2C-1 bus (it
answers at address 0x6A or 0x6B), the first-boot provisioner detects it and
installs an auto-rotation helper. The kiosk service can then call the helper to
orient the display to match how the panel is physically mounted. This is purely
optional; nothing breaks when the sensor is absent.

## Barcode scanners

Two ways to scan, neither needing special hardware:

- **USB HID keyboard-wedge scanner.** Almost every wired or wireless USB barcode
  scanner presents itself as a HID keyboard and "types" the scanned code into the
  focused field, so it works with no configuration. 1D (UPC/EAN) and 2D
  (QR/DataMatrix) both work as long as the scanner reads them.
- **Camera scanner.** The web UI can scan with any device camera, for example
  your phone, with no dedicated hardware.

For a fully headless scanner that submits directly without a focused browser
field, the Home Assistant integration captures the scanner with the
`keyboard_remote` integration and posts the barcode to FoodAssistant. That path
requires Home Assistant OS or Supervised; see
[homeassistant/barcode-scanner.md](../homeassistant/barcode-scanner.md) and
[Platforms](platforms.md).
