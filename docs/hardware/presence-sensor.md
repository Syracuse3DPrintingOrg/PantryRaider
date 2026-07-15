# mmWave presence sensor

An LD2410C presence sensor watches the kitchen and wakes the display the
moment someone walks up, instead of waiting for a touch. Add one to a Pi
appliance and the screen (and the Stream Deck, which wakes with it) is
already lit by the time you get to it.

## What you get

- The kiosk screen wakes as you approach, before you touch anything.
- The Stream Deck wakes with it, since it shares the same wake signal as a
  screen touch.
- Detection is automatic once you turn the setting on: the app confirms the
  sensor is fitted the first time it triggers, with nothing to configure on
  the module itself.

## Parts

- An HLK-LD2410C mmWave presence sensor module (about $5, widely sold as
  "LD2410C" or "HLK-LD2410C").
- 4 female-to-female Dupont jumper wires.

## Wiring

Connect the sensor straight to the Pi's GPIO header. Only three of its five
pins are used; leave TX and RX unconnected; the OUT pin alone carries
everything the display wake needs.

| LD2410C pin | Connects to | Raspberry Pi physical pin |
| --- | --- | --- |
| VCC | 5V | Pin 2 |
| GND | Ground | Pin 6 |
| OUT | GPIO17 | Pin 11 |
| TX | Not connected | |
| RX | Not connected | |

The sensor's own UART pins (TX/RX) carry a richer protocol with distance and
target details, but Pantry Raider does not use them. OUT is a plain 3.3V
line that goes high whenever the sensor sees someone in range, which is all
a wake trigger needs, so leaving TX and RX unconnected is intentional, not a
step you skipped.

## Mounting

Mount the sensor behind or above the display, with a clear, unobstructed
view of the space in front of it (glass, plastic panels, and thin plywood
are fine; metal enclosures are not). The sensor covers roughly a 60 degree
cone out to about 5 meters, so it does not need to point exactly at the spot
where someone stands, just generally at the room. If it feels too sensitive
or not sensitive enough for your kitchen, the module has its own free phone
app over Bluetooth for tuning detection range and sensitivity; Pantry Raider
does not need any of those settings changed to work, so this step is
optional.

## Turn it on

1. Wire the sensor as shown above and power the Pi back up.
2. Go to Settings, Screen, and set **Wake on presence** to **Auto**.
3. Walk up to the display. The first time the sensor sees you, the setting
   confirms the sensor is fitted and the wake starts working from then on.

If your Pantry Raider install is already up to date, no reflash or update is
needed to use the sensor; if the setting does not confirm detection after a
few walk-ups, run an update from Settings, Maintenance to pick up the latest
host software, then try again.

## How it works with other display settings

**Wake on presence** stacks with **Display sleep** and **Wake on motion**:
the screen still sleeps after your chosen idle time, and a touch, a Stream
Deck button, a physical bump (with the accelerometer wake sensor), or now a
person walking up, all wake it the same way. Turning **Wake on presence**
off just removes that last trigger; the other wake paths keep working.

## Home Assistant

If your kitchen screen is linked to Home Assistant, the presence sensor
shows up automatically as an occupancy sensor through the Pantry Raider
integration, no extra setup required.
