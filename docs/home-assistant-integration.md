# Home Assistant integration

Pantry Raider has a native Home Assistant integration. Add your install once and
it shows up as a device with your food counts, kitchen timers, thermometer
readings, printer status, and screen settings as Home Assistant entities. No
templates or REST sensors to hand-edit.

If you run a server or a Pi appliance, any bandits (satellite screens) it knows
about appear as their own devices underneath it, so a wall screen in the garage
and the main pantry box each get their own controls in Home Assistant.

## Add it

### With HACS (recommended)

1. In HACS, open the three-dot menu and choose **Custom repositories**.
2. Add `https://github.com/Syracuse3DPrintingOrg/PantryRaider` as an
   **Integration**.
3. Search HACS for **Pantry Raider**, install it, and restart Home Assistant.
4. Open **Settings, Devices & Services, Add Integration**, search for
   **Pantry Raider**, and fill in your address.

### By hand

1. Copy the `custom_components/pantry_raider` folder from the project's
   `homeassistant` directory into your Home Assistant `config/custom_components`
   folder.
2. Restart Home Assistant.
3. Add the integration from **Settings, Devices & Services**.

## What to enter

- **Host**: the local address of your install, such as `192.168.1.170`. Use the
  local address, not a public web address. Home Assistant talks to the app
  directly, and a public login page would get in the way.
- **Port**: `9284` unless you changed it.
- **API key**: leave it blank if your install has no password. If it does, copy
  the key from **Settings, Security** in Pantry Raider.

You can change how often Home Assistant checks in (every 30 seconds by default)
from the integration's **Configure** button later.

## What appears, by install

The integration only offers what a given install can actually do.

A **server** or **Pi appliance** gives you:

- Counts for expired, expiring today, expiring within 3 days, and expiring
  within 7 days.
- Pending scans waiting for review, and open action items.
- Timers running, and the next timer with its label and countdown.
- A temperature sensor for each Bluetooth thermometer probe, which goes
  unavailable when the thermometer drops out or its battery dies.
- The label printer queue and the app version.
- Presence, when the screen reports it, and an "expiring attention" alert that
  turns on when anything is already expired or expires today.
- Display sleep and screensaver delay in minutes, the screensaver style, and how
  presence wakes the screen.

A **bandit** (a satellite screen, whether you add it directly or it is
discovered under a server) gives you just its own device controls: presence,
display sleep, screensaver delay and style, wake on presence, its printer queue,
and version.

Bandits under a server are added on their own as they check in. If a bandit
drops off the network for a moment its entities stay put and simply go
unavailable, so your automations are not disturbed; they come back when it
returns. To drop a bandit that is truly gone for good, reload the integration.

## Moving from the old YAML sensors

Earlier setups pasted REST sensors from `configuration.yaml` and, for wall
panels, `espcontrol.yaml`. Those still work and remain the manual alternative if
you prefer to hand-manage everything. The integration replaces them for exposing
entities: it discovers your install, keeps unique IDs stable, groups everything
under proper devices, and adds bandits for you. You can run the integration and
retire the `sensor.food_*` REST sensors at your own pace.

Two other Home Assistant connections are separate from all of this and do not
change: Pantry Raider's own link to Home Assistant for camera feeds and Stream
Deck keys, and the on-screen event toasts. Setting up the integration does not
touch either one.
