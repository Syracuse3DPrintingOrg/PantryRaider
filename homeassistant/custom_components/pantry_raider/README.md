# Pantry Raider Home Assistant integration

A native Home Assistant integration for Pantry Raider. Add your install once and
it appears as a device with its counts, timers, thermometer probes, printer
queue, and display controls as entities. If your install is a server or Pi
appliance, every bandit (satellite) it knows about shows up as its own device
underneath it.

This supersedes the old `configuration.yaml` REST sensors for exposing entities.
Those YAML files still work and remain the manual alternative; see the note at
the bottom of `homeassistant/README.md`.

## Install

### Option A: HACS (recommended)

1. In HACS, open the three-dot menu and choose **Custom repositories**.
2. Add `https://github.com/Syracuse3DPrintingOrg/PantryRaider` as an
   **Integration**.
3. Search HACS for **Pantry Raider**, install it, and restart Home Assistant.
4. Go to **Settings, Devices & Services, Add Integration**, search for
   **Pantry Raider**, and enter your address.

### Option B: Manual copy

1. Copy `homeassistant/custom_components/pantry_raider/` into your Home
   Assistant `config/custom_components/` folder (the result should be
   `config/custom_components/pantry_raider/manifest.json`).
2. Restart Home Assistant.
3. Add the integration from **Settings, Devices & Services**.

## Setup

- **Host**: the LAN address of your install, for example `192.168.1.170`. Use
  the LAN address, not a public reverse-proxy URL: Home Assistant polls the app
  directly, and a login proxy would answer with its own page instead.
- **Port**: `9284` by default.
- **API key**: leave blank if your install has no password. Otherwise copy it
  from Pantry Raider under **Settings, Security**.

The update interval (30 seconds by default) is under the integration's
**Configure** button.

## What you get

The entities offered depend on what the install is:

- A **server** or **appliance** exposes the food counts, pending scans, action
  items, timers, thermometer probes, printer queue, presence, display sleep and
  screensaver controls, and an "expiring attention" problem sensor.
- A **bandit** (added directly, or discovered under a server) exposes only its
  own device controls: presence, display sleep, screensaver delay and style,
  wake-on-presence, printer queue, and version.

Bandits discovered under a server are added automatically as they check in. A
bandit that later drops off the server's list keeps its entities until you
reload the integration, so a brief network blip never disturbs your automations.

## Manual test checklist

Home Assistant's own integration tests need the full `homeassistant` package,
which this repository does not carry. The repo instead ships pure-logic tests
for the payload helpers (`tests/test_ha_integration_helpers.py`). Verify the
rest by hand against a running install:

- [ ] Add the integration with a correct host and port. A device named after
  your install's hostname appears with sensors showing numbers, not
  `Unknown`.
- [ ] Enter a wrong port or a stopped host: the flow reports "could not reach".
- [ ] With an API key set on the install, enter the wrong key: the flow reports
  the key was rejected. Enter the right key: setup succeeds.
- [ ] On a server or appliance, add an item expiring today and confirm
  `Expiring today` and the `Expiring attention` binary sensor react within one
  poll.
- [ ] Change **Display sleep** and **Screensaver style** from Home Assistant and
  confirm the kiosk reflects the change.
- [ ] Pair a Bluetooth thermometer and confirm a temperature sensor appears per
  probe, reads in Celsius, and goes unavailable when the thermometer is stale.
- [ ] With a bandit on the network, confirm it appears as its own device linked
  under the server, exposing only its device controls.
- [ ] Stop the install and confirm all entities go unavailable, then recover on
  their own when it returns.
