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

1. Open **Settings, Devices** and click **Flash a new Cub** (or go to the
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

Once it is on Wi-Fi, the Cub finds your Pantry Raider server on its own and shows
a short **pairing code** on its screen. On the kitchen screen a "device asking to
join" message appears.

Open **Settings, Devices**, check that the code there matches the code on the
Cub, give the Cub a name (like "Stove shelf"), and **Approve**. Within a few
seconds the Cub starts showing your kitchen, and its card appears under Bandit
Cubs with its name, board, firmware version, and an online badge.

If your Pantry Raider server runs in Docker on a bridge network, the Cub may not
find it automatically. In that case the Cub shows a prompt to set the server
address, and you enter your server's address on the flasher page while the Cub is
still connected over USB.

## What the screen shows

What a Cub displays is set in **Settings, Devices**, for every Cub at once, with
a per-Cub override on each Cub's card:

- **Idle view**: expiring items, a rotation through views, or a clock.
- **Timers take over**: a running timer seizes the screen so you see the
  countdown from across the kitchen.
- **A probe target takes over**: when a thermometer probe has a target set and no
  timer is running, the Cub shows the probe instead.
- **A fridge or door alarm takes over**: a live protection alarm (a fridge out of
  range, a door left open) takes the whole screen until it clears.

Change any of these and every Cub follows on its next check-in. No reflash.

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
