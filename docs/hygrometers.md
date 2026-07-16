# Fridge and room sensors (hygrometers)

Pantry Raider reads small Bluetooth temperature and humidity sensors, the
kind you drop in a refrigerator, freezer, pantry, or room, and shows their
readings on the Time & Temp page alongside your kitchen timers and cooking
probes. For a food tracker this is the natural companion feature: the same
screen that watches your roast can also tell you your fridge is sitting at a
safe 3 degrees.

These sensors are a separate class from the [cooking
thermometers](thermometers.md). A hygrometer has no probes, targets, or
doneness presets; it has a location (Fridge, Freezer, Pantry, Room), a
temperature, a humidity, and a battery. Set a normal range on each sensor
and Pantry Raider stands guard: a fridge that drifts warm, a freezer left
thawing, or a door left open raises an alarm on every screen in the kitchen
until it is fixed.

## Supported hardware

All of these broadcast their readings over Bluetooth continuously, so Pantry
Raider only listens; nothing is paired or connected, and the sensor's phone
app keeps working alongside.

- **Govee H5075** (and the H5072/H5074 and similar Govee ambient sensors).
  Cheap, ubiquitous, and long-lived on a pair of AAA batteries.
- **Xiaomi LYWSD03MMC**, the little square Mijia sensor, **only when flashed
  with the community ATC firmware** (either the atc1441 or the pvvx build,
  both work). The stock Xiaomi firmware encrypts its broadcasts and is not
  supported; flashing takes a few minutes in a web browser and makes the
  sensor both readable and better on battery.
- **SwitchBot Meter and Meter Plus** (and the outdoor meter).
- **Inkbird IBS-TH1 and IBS-TH2**. A temperature-only IBS-TH2 shows just its
  temperature; the humidity spot stays blank.

## Setting it up

Hygrometers ride the same Bluetooth reader as the cooking thermometers, so if
probes already work on your device there is nothing more to install; see
[Bluetooth kitchen thermometers](thermometers.md) for the one-time reader
setup on a Pi appliance or a server.

Open Settings, Thermometers and find the **Hygrometers** section. A sensor
that is switched on nearby appears under Found nearby; add it, give it a name
and a location like Fridge or Freezer, and its card shows up on the Time &
Temp page with live temperature, humidity, and battery. A sensor that is out
of range right now can be added by its Bluetooth address instead.

Each sensor's row in Settings also has an alert range: the lowest and highest
temperature, and humidity, you consider normal for that spot. Leave a field
blank to skip that check. The same row sets the alarm timing described
below.

### From Home Assistant

No Bluetooth radio on the Pantry Raider machine? If Home Assistant already
sees your sensor (directly or through Bluetooth proxies), pick its
temperature entity, and its humidity entity if it has one, in the
Hygrometers section's From Home Assistant row. The pair shows up as one
sensor, readings and all, with nothing else to install.

### From an ESP device

A DIY WiFi sensor works too: an ESP32 or ESP8266 flashed with ESPHome (a
DHT22 or BME280 gives you temperature and humidity together) and the
`web_server` component can report to Pantry Raider directly, the same way
[ESP thermometers](thermometers.md#adding-an-esp-device) do, with the
humidity sensor read alongside the temperature one.

## Alarms

This is the point of the whole feature: a fridge full of groceries is worth
protecting, and a sensor that only shows a number cannot protect anything.

- **Out of range.** When a sensor's temperature or humidity leaves the range
  you set and stays out for longer than the grace period (5 minutes unless
  you change it), an alarm appears on the kiosk, in the browser, and on any
  Bandit Cub display. The grace period is there so a door opening or a
  defrost cycle does not page the whole house; a real failure does.
- **Stopped reporting.** Each sensor can also alarm when it goes silent for
  too long, a dead battery or a sensor knocked off its shelf. This one is
  off unless you set a window (the "min silent" field on the sensor's row).
- **Door left open.** A [door sensor](#door-sensors) open past its limit
  (3 minutes unless you change it) alarms the same way and clears the moment
  the door closes.

An alarm clears itself when the condition ends: the reading comes back into
range, the sensor reports again, the door closes. On a Bandit Cub an active
alarm takes over the display outright until it clears; turn that off with
the "A fridge or door alarm takes over" switch in Settings, Devices if you
would rather the Cub kept its usual view.

## Door sensors

A door sensor is a small magnet contact you stick on the fridge or freezer
door, the cheapest insurance there is against a door left ajar overnight.
They have their own section in Settings, Thermometers, right under the
hygrometers, and work the same way: add one from Found nearby (open or close
the door once if it does not show; many only broadcast on a change), give it
a name and a location, and set how long the door may stay open before the
alarm.

Supported hardware, honestly stated:

- **Shelly BLU Door/Window**, and anything else that broadcasts
  [BTHome](https://bthome.io/) version 2 unencrypted. BTHome is an open,
  published standard, which makes these the recommended pick; a DIY ESPHome
  sensor broadcasting BTHome works too. A BTHome device set to encrypted
  mode cannot be read.
- **SwitchBot Contact Sensor.**
- **Xiaomi door sensors, only the unencrypted ones.** Most current Xiaomi
  door sensors encrypt their broadcasts once paired to the Mi Home app (the
  "bindkey" models) and are NOT supported; if a Xiaomi sensor shows nothing,
  that is why. Prefer the Shelly or SwitchBot options.

## Sensors near a Bandit

Your sensors do not have to sit near the main server. A Bandit (a Pi Remote
satellite) with the Bluetooth reader installed relays everything it hears to
your main server: fridge and room sensors, door sensors, thermometers, and
shelf buttons near any Bandit appear in the server's Found nearby lists and
report their readings there, even when the server itself has no Bluetooth
radio at all.

Add and manage the relayed sensors on the main server like any other: the
server's settings are where names, locations, thresholds, and alarms live,
and each satellite picks the device list up on its next sync (within a
minute or two), so its radio knows what to watch. Alarms are raised by the
server, once, no matter how many devices can hear the sensor; a sensor in
range of both a server radio and a Bandit shows a single card with the
freshest reading. If the server is briefly unreachable, the Bandit holds its
readings and delivers them when the connection returns.

The relay is on for every satellite out of the box and rides the same secure
link the satellite already uses for its settings sync. Nothing leaves your
network.

## On the Time & Temp page

Your sensors get their own Fridge & room sensors block under the cooking
probes: one card per sensor with its location, the temperature big and
readable across the kitchen, the humidity beside it, and the battery level.
A sensor that has not been heard from in a few minutes dims and shows a No
signal badge; fridge walls and metal doors eat Bluetooth, so a sensor deep in
a freezer may report less often than one on a shelf, and the card takes that
in stride. A sensor with a live alarm turns its card red and says what is
wrong, so the problem is visible from across the kitchen.

Everything stays local: the sensors broadcast to your device (or to your own
Home Assistant), and nothing about your kitchen leaves the house.
