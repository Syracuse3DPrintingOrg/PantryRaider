# Bluetooth kitchen thermometers

Pantry Raider reads Bluetooth meat and probe thermometers and shows their live
temperatures right in the kitchen, all on your own network with no cloud
account. Set a target for a probe and the app pops an on-screen alert the
moment it is reached, sends a Home Assistant event alongside it so your
automations can react too, and can even tell you roughly when the food will
get there, so you can walk away from the roast and let the kitchen screen
watch it for you.

Supported thermometers today: Inkbird (IBT-2X, IBT-4XS, IBT-6XS), ThermoPro
(including the TempSpike), Combustion Inc, ThermoWorks BlueDOT, and Govee
grill thermometers (H5182 and siblings). A thermometer nearby that is not one
of these shows up as "seen nearby, not supported yet" instead of just
disappearing.

Fridge, freezer, and room temperature + humidity sensors (Govee H5075,
SwitchBot Meter, and kin) are their own device class with their own page:
see [Fridge and room sensors](hygrometers.md). They use the same reader as
the thermometers here, so setting one feature up sets up both.

## Where the readings come from

Readings can come from three sources, together or on their own. All feed the
same live numbers, targets, and alerts.

- **A Bluetooth reader on the device.** The Bluetooth radio belongs to the
  machine, not the app, so a small helper on the device reads the thermometers
  and hands their temperatures to the app. This is the natural path on a Pi
  appliance sitting in the kitchen.
- **Home Assistant.** If Home Assistant already sees your thermometers,
  including through ESPHome Bluetooth proxies placed around the house, Pantry
  Raider can read them from there. Pick the temperature entities in Settings and
  they behave exactly like a directly connected probe. No Bluetooth radio is
  needed on the Pantry Raider machine, which makes this the natural path for a
  server install.
- **An ESP device on your network.** A DIY ESP32 or ESP8266 flashed with
  ESPHome, a temperature sensor, and the web_server component reports its
  reading over WiFi, straight to Pantry Raider with nothing in between. Point
  Settings at the device address and it shows up like any other probe. This is
  a nice fit for a fixed sensor you want watched, a fridge, a freezer, or a
  room, and it needs neither a Bluetooth radio nor Home Assistant.

## Setting it up

Settings, Thermometers & Sensors is the home for all of this on every kind of install.
Turn the feature on, see at a glance whether the reader is connected, add,
rename, or remove thermometers, and set your targets. Removing a thermometer
lives here in Settings only, so a stray tap at the counter cannot drop one
mid-cook.

### On a Raspberry Pi appliance

Open Settings, Thermometers & Sensors and press Set up for me. That installs the
Bluetooth reader through the host bridge in one step. A thermometer that is
awake and in range then shows up as ready to add. You can also turn the reader
on at install time, or install it by hand on the device if you prefer.

### On a server

A server usually has no Bluetooth radio of its own, so the easiest path is
Home Assistant: if HA already sees your thermometers, turn on the Home
Assistant source in Settings, Thermometers & Sensors and pick the temperature entities.
They appear on the Timers page with targets and alerts like any other probe.

If your server does have a Bluetooth radio, the reader can be installed on the
host instead. The Thermometers & Sensors pane shows the exact steps.

### On a satellite

A satellite (a thin display pointed at your main server) with its own
Bluetooth radio reads nearby thermometers the same way and sends what it
finds up to the server, so those probes show up on the server's Timers page
as well as the satellite's own kiosk. Add and manage a satellite's probes
from either screen; the server holds the list.

### Discovering a Home Assistant grill in one step

If your grill or smoker exposes several probes to Home Assistant, Settings,
Thermometers has a **Discover grills** list that groups those probes into one
device and adds them all at once, instead of adding each probe by hand. The
Home Assistant entity picker also has an "only show sensors with a current
reading" option, on by default, so a large HA install does not bury your live
probes under a long list of unavailable ones.

### Adding an ESP device

If you have built (or want to build) a WiFi temperature sensor, flash an ESP32
or ESP8266 with ESPHome using any temperature sensor (a DS18B20 waterproof
probe, a DHT22, a BME280) and the `web_server` component turned on. That last
part is what lets Pantry Raider read it: ESPHome then serves each reading at a
small web address on your network.

In Settings, Thermometers & Sensors, open **From an ESP device**, type the device's
address (an IP like `192.168.1.50`, or an mDNS name like `fridge.local`), and
tap **Find sensors**. Pantry Raider lists the temperature sensors the device
offers so you can pick one, give it a name like Fridge or Freezer, and add it.
The probe then behaves like any other: live on the Timers page, with targets,
alerts, and the ready-in estimate. If the device is on a part of your network
the app cannot reach for discovery, you can still type the sensor name yourself
and add it directly.

An ESP device can also carry buttons that start a timer or fire an action; see
[ESP devices](esp-devices.md#buttons-and-timers).

## Naming your thermometers and probes

Any thermometer you add can be renamed to something you recognize, like Grill
or Smoker, so a device that only broadcasts a code no longer shows a bare
address; the pencil next to it prompts for a new name.

A two-lead probe like the ThermoPro TempSpike reads two things at once, and
Pantry Raider labels each lead: **Internal** (the tip that goes in the food)
and **Ambient** (the pit or oven air around it). Both readings share one
compact card, with the internal food temperature big and bold and the ambient
reading smaller and dimmed, so the number that matters stands out. If the
guess is wrong, or you have moved a lead, you can override any probe to
Internal, Ambient, or Food.

## Targets, presets, and alerts on the Time & Temp page

Day-to-day, your probes live under Time & Temp, right alongside your kitchen
timers; the tabs in the header switch between Timers, Thermometers, and the
combined view. In the combined view the timers area folds down when nothing is
running, so the temperatures get the screen. Each probe shows its current
temperature in big, kitchen-readable numbers with its battery state, and a
low-battery badge once it starts running down. A thermometer that has been out
of signal for more than five minutes folds its card down to just the name and
a No signal badge (tap to expand and see its last reading), so a dropped probe
does not take up the screen with stale numbers.

Setting a target offers a doneness preset, Beef medium-rare, Chicken, Pork,
and the like, instead of making you remember a number, with your own custom
temperature still available. If a recipe is on the line and it names a
target, one tap fills it in. When it is met, the screen raises a single
alert, once, rather than nagging every few seconds, and Pantry Raider sends a
matching Home Assistant event so your automations can flash a light,
announce it, or push a notification.

While a probe is genuinely climbing toward its target, the card also shows a
live "Ready in ~20 min" estimate that updates as the temperature moves, so
you know roughly how much time is left without hovering over the grill. It
quietly disappears when the food is cooling, holding steady, or already
there.

A **Govee grill** thermometer that has its own alarm temperature set on the
device shows that as the probe's target automatically, so you do not have to
type it in twice; a target you set yourself in the app still wins and is what
drives the alert.

A thermometer that comes into range simply shows up as ready to add.

Everything here stays local. The thermometers talk over Bluetooth to the
device (or to Home Assistant on your own network), and nothing about your cook
leaves the house.
