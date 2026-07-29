# Plug-in accessories (STEMMA QT / Qwiic)

Some things belong under your fingers, not behind a menu. Pantry Raider
supports a family of small boards that plug straight into a Pi appliance with
a single cable: no soldering, no breadboard, no firmware to flash. Plug one
in, add it in Settings, and it works.

The first of them is the one you will notice most: a four-key pad that sits
on the counter and decides what your barcode scanner does.

## The four keys and the scanner

The barcode scanner already knows four different jobs. Scan a box of pasta and
Pantry Raider needs to know whether you just bought it, are about to cook it,
want more of it, or are counting what is on the shelf. That choice is the
scanner mode, and until now you changed it on a screen.

With a **NeoKey 1x4** on the counter, each key is one of those jobs:

| Key | Mode | What a scan does |
| --- | --- | --- |
| 1 | Stock | Adds what you scan to the pantry |
| 2 | Use | Takes what you scan back out |
| 3 | Shop | Puts what you scan on the shopping list |
| 4 | Audit | Counts what you scan, changing nothing |

Tap a key and the mode changes instantly, everywhere: the kiosk, the Stream
Deck, and any other screen catch up on their own. The key you picked stays
lit in its own color, and the other three glow faintly so you can read the
layout in a dark kitchen. Change the mode somewhere else and the lit key
follows within a couple of seconds, so the counter never lies about what the
scanner is about to do.

The moment your hands are full of groceries, this is the difference between
putting the bag down to tap a screen and just hitting a key with a knuckle.

## What you need

- A **Pi appliance** (Pantry Raider on a Pi, hosted or as a remote Bandit).
  These boards plug into the device itself, so a Docker server in a closet
  has nothing to plug them into.
- An **Adafruit NeoKey 1x4** and one **STEMMA QT cable**.

STEMMA QT (Adafruit) and Qwiic (SparkFun) are the same connector with two
names, so boards from either brand work on the same cable. The boards also
chain: one cable from the Pi to the first board, another from that board to
the next.

See the [hardware guide](hardware.md) for which port to use and how to wire
it, including the adapter cable option if your board has header pins instead
of a QT socket.

## Setting one up

1. **Plug it in** with the Pi powered off, then boot it. Nothing else is
   wired; the keys are read over the same cable that powers them.
2. Open **Settings, Thermometers & Sensors, Accessories**. Within a minute
   the NeoKey appears under "Found plugged in". Press **Add**.
3. That is the whole setup. The keys arrive mapped in the order above, so the
   pad works the moment you add it.
4. Optionally give it a name ("Counter keys"), remap any key, or turn the
   brightness down for a kitchen you walk through at night.

If the section says this device cannot use accessories, it will say why:
usually the I2C connection is not turned on yet, which one run of
`sudo foodassistant-gadgets-setup` on the device fixes, followed by a reboot.

## Remapping the keys

Each key gets a dropdown. Pick any of the four scanner modes, or **Nothing**
to leave a key dark and inert. Two keys can share a mode if that suits how
you work, and a key set to Nothing is simply ignored when pressed.

Not sure which physical key a row means? Press **Test** next to it and that
key lights up white for a moment.

The key colors are fixed, one per mode, so the same color always means the
same thing on every surface that shows it.

## On a Bandit remote

A NeoKey plugged into a Bandit (a remote Pi that shows your main server's
kitchen) selects the mode for the **whole house**, not just that Bandit. The
key press travels to the main server, which is where your inventory lives, and
every screen everywhere follows. Put a pad on the kitchen counter and another
by the garage freezer if you like; each one drives the same scanner mode, and
both stay lit on whichever mode is current.

## When something is unplugged

Pull the cable and the accessory's card says so within a minute or two, the
same way a silent sensor does. Plug it back in and it recovers on its own;
nothing needs restarting, and its key mapping is remembered by where it sits
on the connector, not by luck.

## More to come

The connector is one cable standard with hundreds of boards behind it, and
the plumbing here is built for all of them. Coming next: distance and motion
sensors that wake the screen when you walk up, temperature and humidity
sensors that join your [fridge and room sensors](hygrometers.md) with no
batteries to change, a rotary knob for adding time to a running timer, and a
status light that goes red when an alarm fires.

Everything here is off until you plug something in and add it, and nothing
touches your existing setup.
