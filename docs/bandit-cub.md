# Bandit Cubs

A Bandit Cub is a small ESP32 screen that lives in your kitchen, on a counter,
a fridge door, or by the stove, and shows what you need to know right now: items
about to expire, running timers, and thermometer probes. What a Cub shows is
decided on your Pantry Raider server, not on the device, so you change it in
Settings and never reflash.

This page covers buying a Cub, flashing it from your browser, turning on the
broadcast it listens to, and what the screen shows. If your board is not one of
the three prebuilt ones, there is an ESPHome route at the end.

## What you need

A supported board and a USB cable that carries data (not a charge-only cable).
The three boards with one-click firmware are:

| Board | Screen | Touch |
|---|---|---|
| LilyGo T-Display | 1.14 inch, 135x240 | no |
| LilyGo T-Display S3 | 1.9 inch, 170x320 | no |
| Waveshare ESP32-S3 Touch LCD 7 | 7 inch, 800x480 | yes |

Any of these plugs straight into the computer you use to reach Pantry Raider.

You also need a device running the thermometer reader with a Bluetooth radio,
because that is what broadcasts the status a Cub shows. A Pi appliance has one;
a server in a cupboard often does not.

## Flash it from your browser

1. Open **Settings, Bandit Remotes** and click **Flash a new Cub** (or go to the
   Bandit Cubs page directly).
2. Plug the Cub into this computer over USB.
3. Find your board in the list and click **Install**. Your browser asks which
   USB serial port to use; pick the one that appeared when you plugged the Cub
   in, and the firmware writes itself. No drivers, no command line.
4. When it finishes, the page walks straight into Wi-Fi (see below).

### The Chrome and HTTPS requirement

Flashing over USB from a web page uses Web Serial, which browsers only allow in
two situations: **Chrome or Edge**, and only when the page is on a **secure
(HTTPS) address** (or `localhost`). Firefox and Safari do not support it at all.

If you reach Pantry Raider at a plain `http://` address on your home network,
the browser blocks USB flashing there, and the page tells you so. To flash from
the browser, open the **same page over your secure address**, the one you use to
reach Pantry Raider from away from home, then flash from there.

If Chrome or a secure address is not an option, you do not need the browser
flasher at all. Every board on the page offers:

- a **firmware download** for that board, and
- a **one-line `esptool` command** to flash the downloaded file yourself.

You can also upload that same downloaded firmware file to
[web.esphome.io](https://web.esphome.io), a browser flasher that works from any
secure page.

If a board says **firmware for this board has not been published yet**, there is
no image to flash for it in this release; check back after the next update, or
take the ESPHome route below.

## Turn the broadcast on

A Cub listens rather than asks. Your kitchen broadcasts a small status packet
over Bluetooth and the Cub shows what it hears, so before a Cub has anything to
show, turn that on: **Settings, Devices, Bandit Cubs, Broadcast kitchen status
over Bluetooth**.

The broadcast comes from whichever of your devices runs the thermometer reader,
so that device needs a Bluetooth radio. A Pi appliance has one built in. A
server in a cupboard may not, and if none of your devices has one, nothing is
broadcast and a Cub waits.

There is nothing to pair, no address to type, and no key anywhere. A Cub that
can hear the broadcast works; one that cannot, waits.

## Wi-Fi is optional

A Cub does not need your network. Everything it shows arrives over Bluetooth.

Join it to Wi-Fi anyway if you want it to appear in Home Assistant as a set of
sensors, or if you want to read its logs. Right after flashing, the page offers
to set the Cub's Wi-Fi over the same USB connection: pick your network, type
the password, done. If you flashed the Cub somewhere else, it raises its own
setup Wi-Fi network with a sign-in page you can join from a phone.

### What the screen shows along the way

Until the first broadcast lands, the Cub says it is listening. Once one
arrives, it switches to the kitchen view and stays there. If the broadcast
stops (the reader went away, the radio was switched off), the Cub keeps showing
the last thing it heard and marks it as stale rather than blanking.

If two kitchens are broadcasting near each other, the first one a Cub hears is
the one it keeps, remembered until the device is erased.

## What the screen shows

What a Cub displays is set in **Settings, Bandit Remotes**, for every Cub at
once, with a per-Cub override on each Cub's card:

- **Idle view**: expiring items, a rotation through views, or a clock.
- **Timers take over**: a running timer seizes the screen so you see the
  countdown from across the kitchen.
- **A probe target takes over**: when a thermometer probe has a target set and
  no timer is running, the Cub shows the probe instead.
- **A fridge or door alarm takes over**: a live protection alarm (a fridge out
  of range, a door left open) takes the whole screen until it clears.

Change any of these and every Cub follows within a few seconds. Those choices
ride along in the broadcast itself, so they reach a Cub without it having to
ask for anything, and without a reflash.

Counts, the soonest timer and one probe temperature all travel. Item names do
not: the broadcast is a small packet that anything in radio range can hear, so
it carries numbers rather than what is in your fridge.

On the non-touch boards the two built-in buttons walk the views: the top one
forwards (expiring, timers, probe, clock, back to automatic) and the bottom one
backwards. Buttons that start a timer or add to the shopping list have to talk
back to your kitchen, which a Cub does not do for now, so they are not mapped;
the `esphome/` folder's README covers remapping and wiring extra buttons.

## You update it by flashing it

A Cub does not update itself. When there is new firmware, plug it into a
computer, open the Bandit Cubs page, and flash it the same way you did the
first time. It takes about a minute.

That is a step back from how it used to work, and it is deliberate. Cubs used
to find a server on the network by themselves and install whatever firmware
that server offered. It was convenient, and it meant anything on your network
could have handed a Cub its own firmware and had it run, without you touching
the device. Rather than leave that in place for the beta, updates now go
through you and a cable.

The way back is signing: once firmware images carry a signature a Cub can
check, it can go back to updating itself safely, because it will refuse
anything that is not genuinely ours. That is the plan, not a maybe.

## What a Cub allows on your network

Nothing on your network can put firmware on a Cub. Not by pushing it, which
the usual ESPHome setup allows and this one does not, and not by offering it
either: a Cub flashed from this page does not go looking for firmware. The only
way its software changes is a cable and this page. That is worth saying plainly,
because code running on the device is the whole game, and it is why updating a
Cub costs you a minute with a USB lead.

The rest of what a Cub shares is read only. It publishes its sensors so Home
Assistant can see them, and that connection is not encrypted on firmware
flashed from this page, because every Cub flashed from the same published image
would have to share one key, and a key printed in a public repository is not a
key. What someone else on your network could read is kitchen status: timers,
alerts, the same things on the Cub's own screen. They cannot change anything,
and they cannot reach your Pantry Raider server through it.

If you want the connection encrypted anyway, build the firmware yourself:
`esphome/cub-custom.example.yaml` in the project has the two lines to add, one
for an encryption key and one for an update password, and a build of your own
can hold real secrets that a shared image cannot.

## The firmware's license

Cub firmware is built with ESPHome, whose runtime is GPL-3.0, so the firmware
and the Pantry Raider component it is built from are GPL-3.0-or-later, not the
noncommercial license the rest of Pantry Raider uses. The source is in the
`esphome/` folder of the project repository, at the tag matching the version
your Cub reports, and `esphome/NOTICE.md` there explains the split.

## More about the broadcast

The broadcast is how every Cub works now, so the short version is above under
"Turn the broadcast on". A few details worth knowing:

- **It is off by default on the sending side.** Turn on **Broadcast kitchen
  status over Bluetooth** in Settings, Devices, under Bandit Cubs. Until you
  do, a Cub has nothing to hear.
- **It needs a sender in Bluetooth range**, meaning a device with a radio
  running the thermometer reader. A Pi appliance has one. A Docker server on a
  machine with no Bluetooth cannot broadcast at all.
- **It is counts only.** The packet carries the expiring counts, pending scans,
  the soonest timer and one probe temperature, and never item names. Anything
  in radio range can hear it, which is exactly why it carries no names.
- **Anything in range can also send one.** The packet is not signed, so a
  determined neighbour could put wrong numbers on your screen. They cannot read
  anything back, reach your server, or change your kitchen; the worst case is a
  Cub showing something untrue. If that matters where you live, pin the install
  your Cub listens to rather than letting the first one heard win.

### Lending your server the Cub's radio (fridge sensors)

**Not available for the beta.** This one is genuinely useful and it is the
feature most worth having back, so here is what it does and why it is off.

The idea is the other way round from the broadcast: your Cub sits in the
kitchen with a live Bluetooth radio, and your server may have none at all. A
Docker server on a NAS cannot hear the sensor in the fridge two rooms away; the
Cub standing next to it can. With the relay on, the Cub forwards what it hears
to the server, which reads it and shows it like any other sensor.

It is off because forwarding means the Cub sending things to your server, and a
Cub does not talk to your server at all right now. It comes back with the rest
of that work; the section below is kept so you know what is coming.

What that gets you: fridge and freezer sensors, door sensors, and shelf
buttons near a Cub appear in Settings, Thermometers & Sensors on the server, in the
Found nearby lists, ready to add. Once added they report their readings
there, each one saying which Cub hears it ("via Cub Kitchen"), and the fridge,
freezer, and door alarms fire from the server exactly as they do for a sensor
its own radio hears.

To turn it on, both ends have to agree:

1. On the server, turn on the Bandit Cub Bluetooth relay setting (a Settings
   toggle is coming; until then set `cub_ble_relay` in `settings.json` or the
   environment). It is off by default.
2. Flash the Cub from a build with the relay on: in `esphome/`, set
   `pr_ble_relay` to `true` in `cub-tdisplay-ble.yaml` and rebuild. The
   standard builds do not relay.

Worth knowing:

- The server decides what is listened for. It sends the Cub the list of
  sensors it can actually read, so support for a new sensor reaches your
  Cubs on their next poll, with no reflash.
- **It relays sensors that broadcast, not ones you connect to.** That covers
  the whole fridge and freezer class (Govee, Xiaomi/ATC, SwitchBot, Inkbird
  IBS-TH), Combustion probes, TempSpikes, and Govee grills. Brands that need
  a Bluetooth connection (Inkbird BBQ probes, ThermoPro) cannot be relayed at
  all; those need a reader in range, or lend the radio to Home Assistant
  instead (below).
- Nothing else changes. A relaying Cub still shows its normal display, and a
  Cub that loses the server simply stops relaying until it is back.
- Several Cubs can relay the same sensor without any double counting: an
  alarm still fires once, and one shelf-button press is still one press.

### Lending Home Assistant your Cub's radio

Because a Cub is a normal ESPHome device with a Bluetooth radio, it can also
serve as a **Bluetooth proxy for Home Assistant**, relaying nearby Bluetooth
devices (thermometers, plant sensors, buttons) to HA from the kitchen. Each
profile package in the `esphome/` folder carries a commented-out
`bluetooth_proxy` block; uncomment it and rebuild. It stays off by default
because continuous scanning and relayed connections cost working memory:
noticeable on the small T-Display, and on the 7 inch touch panel they
compete with the display buffers, so watch the logs after enabling it there.

## Troubleshooting

**The Install button reads "undefined" or does nothing.** The browser is
holding an old cached copy of the flasher page from before an update. Hard
refresh the page (Ctrl+Shift+R, or Cmd+Shift+R on a Mac) and it comes back.

**No serial port shows up when the browser asks.** Usually the cable: it must
carry data, and many bundled USB cables are charge-only. Try another cable and
another USB port. Remember the page itself must be Chrome or Edge on a secure
(HTTPS) address, or the browser hides the flasher entirely.

**The Cub sits on the listening screen and never shows anything.** It has not
heard a broadcast yet. Check that **Broadcast kitchen status over Bluetooth**
is on in Settings, Devices, that the device running your thermometer reader has
a Bluetooth radio, and that it is close enough: Bluetooth is good for a room or
two, not a whole house. A Cub with no broadcast in range waits rather than
showing anything wrong.

**The numbers on the Cub look stale.** The Cub shows the last broadcast it
heard and marks it old rather than blanking, so a stale screen means the
broadcast stopped. Check the reader is still running on the machine with the
radio.

**Firmware for this board has not been published yet.** The release you are on
has no image for that board. Check back after the next update, or take the
ESPHome route below.

**Starting completely over, or moving a Cub to another kitchen.** Flash again
with the **Erase device** box checked on the install dialog. That wipes the
stored Wi-Fi and, importantly, which kitchen's broadcast the Cub had latched
onto, so it will listen for a new one. If the Cub was flashed before this
release and had paired with a server, erasing also clears that key; revoke its
old pairing from the Security pane if you are retiring it.

## A different board

A Cub is a standard ESPHome project, so any ESP32 board can be one. The firmware
for the three boards above is built from the `esphome/` folder in the project
repository. To use another board, copy the profile YAML closest to yours, set
the two substitutions it documents (your server's address, and an API key once
the Cub is paired), and build it in your own ESPHome dashboard. The Bandit Cubs
page links straight to that folder.

Because it is ordinary ESPHome, the same Cub can also join Home Assistant
natively: add the standard ESPHome API block to the YAML and every count, timer,
and probe reading shows up in Home Assistant as entities, with no extra work.
