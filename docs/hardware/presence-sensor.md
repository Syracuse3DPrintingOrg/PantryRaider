# mmWave presence sensor

A mmWave presence sensor watches the kitchen and wakes the display the moment
someone walks up, instead of waiting for a touch. Add one to a Pi appliance and
the screen (and the Stream Deck, which wakes with it) is already lit by the time
you get to it.

## What you get

- The kiosk screen wakes as you approach, before you touch anything.
- The Stream Deck wakes with it, since it shares the same wake signal as a
  screen touch.
- Detection is automatic once you turn the setting on: the app confirms the
  sensor is fitted the first time it triggers, with nothing to configure on
  the module itself.

## Which sensors work

Pantry Raider reads **one digital pin**. It does not speak any sensor's serial
protocol, so a module works here if it has a plain presence output pin that goes
high when someone is there, low when nobody is, at 3.3V logic. The model name
does not decide it; that pin does.

| Sensor | Works today? | Supply | Presence output | Notes |
| --- | --- | --- | --- | --- |
| HLK-LD2410C | Yes, recommended | 5V to 12V | `OUT` pin, 3.3V logic, high on presence | The worked example below. 2.54mm pins, so jumper wires push straight on. About $5. |
| HLK-LD2410D | Check the board you receive | 3.3V or 5V | Expected to be the same `OUT` pin, 3.3V logic | Same radar family and the same output idea, but Hi-Link does not publish a pin table for it, and its pins are on a 1.27mm pitch that jumper wires do not fit. Buy it only if you are soldering a custom build and can confirm the pin. |
| HLK-LD2420 | Not recommended | 3.3V only | Present, but which pin it is depends on the module's firmware | Its presence pin and its serial pin swap places between firmware versions. Details below. |

None of these three put 5V on their output, so none of them needs a voltage
divider between the sensor and the Pi. The output goes straight to GPIO17.

**If you are buying a sensor, buy the LD2410C.** It is the cheapest of the
three, it is the one this project tests against, and it is the only one whose
pins fit jumper wires without a soldering iron.

## Parts

- An HLK-LD2410C mmWave presence sensor module (about $5, widely sold as
  "LD2410C" or "HLK-LD2410C").
- 4 female-to-female Dupont jumper wires.

## Wiring the LD2410C

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

Fitting the sensor alongside other accessories (a PoE HAT, a DSI display, or a
STEMMA QT chain) is covered in
[Pin map and accessory compatibility](pinout-and-compatibility.md), which lists
what else wants these pins.

The sensor's own UART pins (TX/RX) carry a richer protocol with distance and
target details, but Pantry Raider does not use them. OUT is a plain 3.3V
line that goes high whenever the sensor sees someone in range, which is all
a wake trigger needs, so leaving TX and RX unconnected is intentional, not a
step you skipped.

## Wiring the LD2410D

The LD2410D is the same 24GHz radar family as the LD2410C, reaching further (Hi-Link
rates it to 10m of motion detection against the C's 5m) in a longer 7mm by 35mm
body. It runs from either 3.3V or 5V. If your module has an `OUT` pin, it behaves
exactly like the C's and the wiring is the same three wires:

| LD2410D pin | Connects to | Raspberry Pi physical pin |
| --- | --- | --- |
| VCC | 5V (pin 2) or 3.3V (pin 1), whichever your module is marked for | Pin 2 or pin 1 |
| GND | Ground | Pin 6 |
| OUT | GPIO17 | Pin 11 |

Two honest cautions before you order one. Hi-Link publishes range and power
figures for the D but not a pin table, so we cannot promise from their
documentation which pin on the module you receive is the presence output; check
the silkscreen and the sheet in the box. And its pins sit on a 1.27mm pitch,
half the spacing of the C's, so standard jumper wires will not fit and you are
soldering. The extra range is real, but for a kitchen screen you walk up to, 5m
is already more than the room needs.

## Why the LD2420 is not recommended

The LD2420 is a newer chip, and it is the one part here we would steer you away
from, for a specific reason rather than a vague one: **you cannot tell from the
label which of its pins is the presence output.** Its two signal pins are named
OT1 and OT2, and they swap jobs depending on the firmware the module happens to
ship with. On firmware 1.5.2 and older, OT1 is the presence output and OT2 is
the serial transmit pin. On 1.5.3 and newer, they trade places. Hi-Link's own
pin table describes both as serial pins. Wiring one up is a coin flip you have
to resolve by reading the firmware version off the module first.

It is also 3.3V-only, with a supply range of 3.0V to 3.6V. If you do fit one,
its power wire goes to **pin 1 (3.3V), never pin 2 (5V)**, which will damage it.

If you already own an LD2420 and want to use it, it can work: identify your
firmware version, wire whichever of OT1/OT2 is the presence output on that
version to GPIO17 (pin 11), power from pin 1, and ground to pin 6. Pantry Raider
will read it like any other presence pin. We just cannot give you a wiring table
that is right for every board sold under that name, so we do not pretend to.

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

The sensor holds its output high for its own built-in delay after it stops
seeing you, so brief stillness does not drop the wake. That hold time is a
setting on the module itself, not in Pantry Raider, and the factory value is
fine for a kitchen screen.

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
