# Bandit Cubs: ESP32 displays for Pantry Raider

A Bandit Cub is a small ESP32 screen that lives on the counter, the fridge
door, or next to the stove and shows what your kitchen needs to know right
now: items about to expire, running timers, and thermometer probes. Timers
count down smoothly on the device, and what a Cub shows is decided by
settings on your Pantry Raider server, so changing its behavior never means
reflashing it.

This directory holds everything a Cub is made of:

- `components/pantry_raider/`: the ESPHome external component. It finds your
  server, pairs with it, polls the kitchen summary, and provides the
  sensors and actions the rest of the firmware uses.
- `packages/`: one YAML package per supported board, plus `cub-base.yaml`
  with everything the boards share.
- `cub-<profile>.yaml`: the build files, one per board.
- `cub-custom.example.yaml`: a starting point for boards we do not prebuild.

## Supported hardware

| Build file | Board | Screen | Touch |
|---|---|---|---|
| `cub-tdisplay.yaml` | LilyGo T-Display (ESP32) | 135x240 color LCD | no |
| `cub-tdisplay-s3.yaml` | LilyGo T-Display S3 | 170x320 color LCD | no |
| `cub-touch7.yaml` | Waveshare ESP32-S3 Touch LCD 7 | 7 inch, 800x480 | yes |

The non-touch boards show the kitchen state and have two buttons: one cycles
the view (expiring, timers, probe, clock, or back to automatic), the other
starts a shared kitchen timer (press again to add a minute, hold to cancel).

The touch board adds control: each running timer gets its own "+1 min" and
"Dismiss" buttons, a Presets view starts the Eggs, Pasta, Rice, or general
timer with one tap, and a row along the top switches views by hand. "Auto"
hands the screen back to the server's decision.

## Building and flashing

Install the [ESPHome](https://esphome.io) CLI (`pip install esphome`), then
from the repository root:

```bash
esphome run esphome/cub-tdisplay.yaml
```

`run` compiles the firmware and flashes it over the USB cable (or over the
air once the Cub is on your network). Use `cub-tdisplay-s3.yaml` or
`cub-touch7.yaml` for the other boards.

To pin the server address instead of relying on discovery, override the
substitution in the build file (or on the command line):

```yaml
substitutions:
  pr_server: "192.168.1.170"
```

## First run: Wi-Fi, then pairing

1. **Wi-Fi.** The Cub supports Improv over the same USB cable used for
   flashing and Improv over Bluetooth from a phone. If neither reaches it,
   the Cub raises its own setup hotspot with a captive portal.
2. **Finding the server.** The Cub browses the network for your Pantry
   Raider install automatically. If your server runs in Docker with bridge
   networking it cannot announce itself; set `pr_server` as shown above in
   that case.
3. **Pairing.** The Cub asks the server to join and shows a 4 digit code on
   its screen. Your kitchen screen pops a matching request; open Settings,
   Devices, check that the codes match, and approve it. The Cub is named
   after itself automatically, and you can rename it on its device card.
   The key it receives is stored in flash, survives reboots and updates,
   and can be revoked any time from the Security pane.

Within one poll interval of approval the Cub is showing content.

## The same device in Home Assistant

Every Cub is a normal ESPHome device, so Home Assistant discovers it
natively, no Pantry Raider integration required. Adopting it adds entities
for the expiring counts, pending scans, active timers, the next timer,
probe temperature, the current view, and the pairing code, all fed from the
same kitchen summary the screen renders.

## Your own board

`cub-custom.example.yaml` is the escape hatch: copy it, wire in your board
and display, and build it in your own ESPHome dashboard. The
`pantry_raider:` component gives you the hub (discovery, pairing, polling),
the `sensor`/`text_sensor` platforms, and three actions:

```yaml
# Fire a Start Page action token (see docs/esp-devices.md for the names):
- pantry_raider.press:
    button: timer_eggs
    long: false

# Add a minute to a specific timer, or dismiss it:
- pantry_raider.timer_extend:
    timer_id: !lambda return id(pantry)->state().timers[0].id;
- pantry_raider.timer_dismiss:
    timer_id: !lambda return id(pantry)->state().timers[0].id;
```

Display lambdas read the parsed state directly: `id(pantry)->state()` holds
the expiring list, timers, and probes; `id(pantry)->effective_view()` says
what to show; `id(pantry)->format_remaining(t)` renders a live countdown.
`packages/tdisplay.yaml` is a complete worked example.
