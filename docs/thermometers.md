# Bluetooth kitchen thermometers

Pantry Raider reads Bluetooth meat and probe thermometers and shows their live
temperatures right in the kitchen, all on your own network with no cloud
account. Set a target for a probe and the app pops an on-screen alert the
moment it is reached, so you can walk away from the roast and let the kitchen
screen watch it for you.

Supported thermometers today: Inkbird (IBT-2X, IBT-4XS, IBT-6XS), ThermoPro,
Combustion Inc, and ThermoWorks BlueDOT.

## Where the readings come from

Readings can come from two sources, together or on their own. Both feed the
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

## Setting it up

Settings, Thermometers is the home for all of this on every kind of install.
Turn the feature on, see at a glance whether the reader is connected, add or
remove thermometers, and set your targets.

### On a Raspberry Pi appliance

Open Settings, Thermometers and press Set up for me. That installs the
Bluetooth reader through the host bridge in one step. A thermometer that is
awake and in range then shows up as ready to add. You can also turn the reader
on at install time, or install it by hand on the device if you prefer.

### On a server

A server usually has no Bluetooth radio of its own, so the easiest path is
Home Assistant: if HA already sees your thermometers, turn on the Home
Assistant source in Settings, Thermometers and pick the temperature entities.
They appear on the Timers page with targets and alerts like any other probe.

If your server does have a Bluetooth radio, the reader can be installed on the
host instead. The Thermometers pane shows the exact steps.

## Targets and alerts on the Timers page

Day-to-day, your probes live on the Timers page, right alongside your kitchen
timers. Each probe shows its current temperature in big, kitchen-readable
numbers with its battery state. Set a target for a probe (reach a temperature
from below, or drop below one) and when it is met the screen raises a single
alert, once, rather than nagging every few seconds. A thermometer that comes
into range simply shows up as ready to add.

Everything here stays local. The thermometers talk over Bluetooth to the
device (or to Home Assistant on your own network), and nothing about your cook
leaves the house.
