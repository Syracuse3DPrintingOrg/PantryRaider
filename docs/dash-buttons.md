# Shelf buttons (dash buttons, reborn)

Remember Amazon Dash buttons? Pantry Raider brings the idea home, with no
cloud and no vendor account. Stick a cheap Bluetooth button on a pantry
shelf, next to the paper towels or on the coffee bin, and a press adds that
product straight to your shopping list. A double or long press can do
something else: add a different product, or start a kitchen timer, or fire
any action a Stream Deck key could.

Everything happens on your own network. The button broadcasts its press over
Bluetooth, the same reader that listens for your
[thermometers](thermometers.md) and [fridge sensors](hygrometers.md) hears
it, and the app runs whatever you mapped. Nothing is paired or connected,
and battery life is measured in years because the button sleeps until
pressed.

## Supported buttons

The button has to broadcast its presses in a format Pantry Raider can read.
Two formats are supported:

- **BTHome buttons (recommended).** BTHome is a published, open Bluetooth
  broadcast standard, and the **Shelly BLU Button1** is the flagship: a
  coin-cell button around $10-15 that broadcasts single, double, triple, and
  long presses plus its battery level, unencrypted out of the box. The
  four-button **Shelly BLU RC Button 4** works too (each of its buttons maps
  separately), and so does any DIY ESPHome device that broadcasts BTHome
  button events. If you are buying a button just for this, buy a BTHome one.
- **Xiaomi Bluetooth switches, unencrypted broadcasts only.** Buttons that
  broadcast plain (unencrypted) MiBeacon press events work. Be aware that
  many Xiaomi devices start encrypting their broadcasts once bound to the
  Mi Home app, and encrypted broadcasts cannot be read; the BTHome buttons
  above are the safer purchase.

Not supported, honestly: encrypted BTHome devices (they need a per-device
key exchange that has no place in a press-to-add flow; Shelly buttons ship
with encryption off), encrypted Xiaomi MiBeacon, and SwitchBot's remote
fobs (their press broadcasts are not reliably decodable without a
connection). A button that only speaks Zigbee or Z-Wave is a different radio
entirely; bring those in through Home Assistant automations calling the
Pantry Raider API instead.

## Adding a button

Buttons ride the same Bluetooth reader as the thermometers, so if probes or
fridge sensors already work on your device there is nothing more to install;
see [Bluetooth kitchen thermometers](thermometers.md) for the one-time
reader setup.

1. Open Settings, Thermometers, and find the Shelf buttons section.
2. Click **Listen for a press**, then press any button on the device. A
   button only broadcasts when pressed, so the press is what makes it
   appear; it shows up marked "just pressed" within a few seconds.
3. Click **Add**, give it a name that says where it lives ("Paper towels
   shelf"), and map its presses.

Each button maps three press types independently: **single**, **double**,
and **long**. Each one can:

- **Add a product to the shopping list.** Start typing and pick the product
  from your Grocy inventory; the exact product is linked, so it lands on the
  list properly, not as loose text.
- **Run an action.** Any action token the Start Page and Stream Deck
  understand: `timer_eggs`, `timer_pasta`, an `ha_1`..`ha_5` Home Assistant
  slot, or a custom key id. One press can start the egg timer from across
  the kitchen.
- **Do nothing** (the default).

Use **Try it** next to a mapping to run it once without pressing the
button, and watch the kitchen screen: every successful press shows a toast
("Paper Towels added to Shopping list") so whoever pressed it knows it
worked.

## Good to know

- **One press is one add.** Bluetooth buttons shout their press several
  times to make sure it is heard; the reader collapses the repeats, and the
  app additionally ignores a second identical press within five seconds, so
  an enthusiastic double tap cannot put the same product on the list twice.
- **Battery** shows on the button's card when the button broadcasts it
  (BTHome buttons do), with the same low-battery badge the other sensors
  use.
- **Triple presses** and other exotic press types show as the button's last
  press but cannot carry a mapping; single, double, and long cover the
  presses every supported button sends reliably.
- Presses only act while the Shelf buttons switch is on (adding your first
  button turns it on for you).
