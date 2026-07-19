# Bandit Cubs

A Bandit Cub is a small ESP32 screen that lives in your kitchen, on a counter,
a fridge door, or by the stove, and shows what you need to know right now: items
about to expire, running timers, and thermometer probes. What a Cub shows is
decided on your Pantry Raider server, not on the device, so you change it in
Settings and never reflash.

This page covers buying a Cub, flashing it from your browser, joining it to
Wi-Fi, pairing it, and what the screen shows. If your board is not one of the
three prebuilt ones, there is an ESPHome route at the end.

## What you need

A supported board and a USB cable that carries data (not a charge-only cable).
The three boards with one-click firmware are:

| Board | Screen | Touch |
|---|---|---|
| LilyGo T-Display | 1.14 inch, 135x240 | no |
| LilyGo T-Display S3 | 1.9 inch, 170x320 | no |
| Waveshare ESP32-S3 Touch LCD 7 | 7 inch, 800x480 | yes |

Any of these plugs straight into the computer you use to reach Pantry Raider.

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

## Join it to Wi-Fi

Right after flashing, the page offers to set the Cub's Wi-Fi over the same USB
connection: pick your network, type the password, done. If you flashed the Cub
somewhere else, it raises its own setup Wi-Fi network with a sign-in page you can
join from a phone to do the same thing.

## Approve it (pairing)

Once it is on Wi-Fi, the Cub finds your Pantry Raider server on its own. It
tries three things in order: a server it remembered from a past run, an mDNS
browse of your network, and finally a sweep of your local network looking for
the server directly. The sweep is what finds a server running in Docker with
bridge networking (the common Docker setup), which cannot announce itself over
mDNS, so no address needs to be set by hand there either.

When it finds the server, the Cub shows a short **pairing code** on its screen.
On the kitchen screen a "device asking to join" message appears.

Open **Settings, Bandit Remotes**, check that the code there matches the code on the
Cub, give the Cub a name (like "Stove shelf"), and **Approve**. Within a few
seconds the Cub starts showing your kitchen, and its card appears under Bandit
Cubs with its name, board, firmware version, and an online badge.

One thing to know: a Cub always pairs with your **main server**, never with a
satellite kitchen screen. If your home has both, the Cub finds and joins the
main install on its own; approve it there.

The Cub remembers its server once paired, so later boots connect straight to
it. Re-flashing the Cub with the **Erase device** option checked clears that
memory (along with Wi-Fi and the pairing), and the Cub starts fresh from
Wi-Fi setup.

### What the screen shows along the way

- **Right after flashing**: a setup screen while the Cub waits for Wi-Fi.
- **On Wi-Fi, looking for the server**: a searching message. The network
  sweep can take a minute on a large network; let it work.
- **Server found, waiting for you**: the 4 digit pairing code, which stays
  up until you approve it in Settings, Bandit Remotes.
- **Approved**: your kitchen, within one poll interval (a few seconds).

## What the screen shows

What a Cub displays is set in **Settings, Bandit Remotes**, for every Cub at once, with
a per-Cub override on each Cub's card:

- **Idle view**: expiring items, a rotation through views, or a clock.
- **Timers take over**: a running timer seizes the screen so you see the
  countdown from across the kitchen.
- **A probe target takes over**: when a thermometer probe has a target set and no
  timer is running, the Cub shows the probe instead.
- **A fridge or door alarm takes over**: a live protection alarm (a fridge out of
  range, a door left open) takes the whole screen until it clears.

Change any of these and every Cub follows on its next check-in. No reflash.

On the non-touch boards the two built-in buttons do the essentials: the top
one cycles the view (expiring, timers, probe, clock, back to automatic) and
the bottom one runs a kitchen timer (tap to start or add a minute, hold to
cancel). Both can be remapped to any Start Page action, and a Cub can carry
a couple of extra physical buttons of its own, wired to spare pins; the
`esphome/` folder's README shows both.

## It updates itself

A Cub keeps its own firmware current. It asks the server it paired with for the
firmware that goes with your install, and when there is a newer one, it flashes
it and comes back a minute later. Nothing to plug in, no browser, no cable, and
nothing to do when you have five Cubs instead of one: update Pantry Raider and
the kitchen catches up on its own.

Because it asks the server it actually found, this works on any network. A Cub
that swept the network to find a Docker install is checking that install, at
that address, not a name that may not resolve.

A Cub waits for a quiet moment before it installs. It never interrupts a timer
that is ringing, an alarm on the screen, or a pairing code you are in the
middle of typing in; it simply tries again a few minutes later. Updates only
ever move forward, so a Cub running newer firmware than the server sits still
rather than going backwards.

To turn it off, set **Automatic Cub updates** off on the server. You can also
switch it off for a single Cub from that Cub's card, which is the one to reach
for when one device is somewhere awkward to recover.

One thing worth knowing: a Cub can only start updating itself once it is
running firmware that knows how. Cubs flashed before this shipped need one
last trip through the browser flasher on the Bandit Cubs page. After that, they
are on their own.

## Cubs without Wi-Fi (Bluetooth broadcast)

A Pi-based Pantry Raider (or any install with a Bluetooth radio running the
gadgets agent) can broadcast a small status summary over Bluetooth: the
expiring counts, pending scans, the soonest timer, and one probe temperature.
A non-touch Cub built with the Bluetooth receiver listens for that broadcast,
which gives you two things:

- **A Cub with no Wi-Fi at all.** Skip Wi-Fi setup entirely; the Cub shows
  the broadcast as soon as it hears one. It never pairs and never appears in
  Settings, Bandit Remotes.
- **A backup feed.** A Cub built in `auto` mode works like a normal paired
  Cub, and if Wi-Fi or the server drops, the broadcast keeps the counts,
  the timer countdown, and the probe on screen.

Be aware of what this mode is and is not:

- It is **off by default** on both ends. Turn on **Broadcast kitchen
  status over Bluetooth for battery Cubs** in Settings, Bandit Remotes
  (under Bandit Cubs), and flash the Cub from the Bluetooth-receiver build
  (`cub-tdisplay-ble.yaml` in the `esphome/` folder); the standard builds
  do not listen.
- It needs a **sender in Bluetooth range**: a Pi appliance or another
  install with a radio. A Docker server on a machine with no Bluetooth
  cannot broadcast.
- It is **counts only**. The broadcast never carries item names or anything
  private, so the expiring view shows numbers, not the item list, and the
  screen cannot start or dismiss timers. For the full display and touch
  controls, use the normal Wi-Fi path.
- Timers still count down smoothly on the Cub between broadcasts.
- With two Pantry Raider installs in range, the Cub locks onto the first
  sender it hears and stays with it (re-flash with **Erase device** to
  reset that choice, or pin the install in the build file).

Touch Cubs are not offered without Wi-Fi: a touch screen that cannot send
anything back is a worse non-touch Cub.

### Lending your server the Cub's radio (fridge sensors)

The other way round from the broadcast above: your Cub sits in the kitchen
with a live Bluetooth radio, and your server may have none at all. A Docker
server on a NAS cannot hear the sensor in the fridge two rooms away; the Cub
standing next to it can. Turn the relay on and the Cub forwards what it hears
to the server, which reads it and shows it like any other sensor.

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

**The pairing request keeps appearing and disappearing, and approving never
sticks.** The Cub has latched onto a satellite kitchen screen instead of the
main server. Pantry Raider 0.18.40 fixed this so Cubs move on to the main
server; update your install, then power-cycle the Cub and it pairs with the
right machine.

**The Cub sits on the searching screen.** The network sweep needs the Cub and
the server on the same network; separate Wi-Fi VLANs or guest networks keep
them apart. Give the sweep a minute or two on a large network before assuming
it failed.

**Firmware for this board has not been published yet.** The release you are on
has no image for that board. Check back after the next update, or take the
ESPHome route below.

**Starting completely over.** Flash again with the **Erase device** box checked
on the install dialog. That wipes the stored Wi-Fi, the remembered server, and
the pairing key, and the Cub behaves like it just came out of the box. Revoke
its old pairing from the Security pane if you are retiring it.

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
