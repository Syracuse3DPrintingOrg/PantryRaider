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
| `cub-tdisplay-ble.yaml` | LilyGo T-Display, with the Bluetooth broadcast receiver | 135x240 color LCD | no |
| `cub-tdisplay-s3.yaml` | LilyGo T-Display S3 | 170x320 color LCD | no |
| `cub-touch7.yaml` | Waveshare ESP32-S3 Touch LCD 7 | 7 inch, 800x480 | yes |

The non-touch boards show the kitchen state and have two buttons: one cycles
the view (expiring, timers, probe, clock, or back to automatic), the other
starts a shared kitchen timer (press again to add a minute, hold to cancel).

The touch board adds control: each running timer gets its own "+1 min" and
"Dismiss" buttons, a Presets view starts the Eggs, Pasta, Rice, or general
timer with one tap, and a row along the top switches views by hand. "Auto"
hands the screen back to the server's decision.

## Physical buttons

On the non-touch boards, the two built-in buttons are remappable without
editing the package files. Each profile exposes two substitutions,
overridable from the build file or the command line:

```yaml
substitutions:
  # Button 1: "view_cycle" (the default) cycles the on-screen view.
  # Any other value is sent to the server as a Start Page action token.
  cub_button1_action: view_cycle
  # Button 2: the action token sent on a tap, and long-pressed on a hold.
  # The default, timer_1, taps to start/add a minute and holds to cancel.
  cub_button2_action: timer_1
```

The tokens are the same names the Stream Deck and Start Page use
(`timer_1`, `timer_eggs`, `timer_pasta`, a custom key id like `c1`, and so
on); `docs/esp-devices.md` lists them. A custom key configured as a
shopping add turns a button into a "we're out of this" key.

A Cub can also carry a couple of extra buttons of its own, shelf-button
style. Both `packages/tdisplay.yaml` and `packages/tdisplay-s3.yaml` end
their `binary_sensor` section with a commented block showing the pattern:
a momentary switch wired between a free GPIO and GND, mapped to a
`pantry_raider.press` action. Uncomment it, pick the pins and tokens, and
rebuild.

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
substitution in the build file (or on the command line). Pinning is optional:
the Cub finds the server on its own (see below), including on a
bridge-networked Docker server that cannot announce itself.

```yaml
substitutions:
  pr_server: "192.168.1.170"
```

## First run: Wi-Fi, then pairing

1. **Wi-Fi.** The Cub supports Improv over the same USB cable used for
   flashing and Improv over Bluetooth from a phone. If neither reaches it,
   the Cub raises its own setup hotspot with a captive portal.
2. **Finding the server.** The Cub finds your Pantry Raider install
   automatically, in this order: a server it remembered from a past run,
   then an mDNS browse of the network, then a sweep of your local network
   looking for the server directly. The sweep is what finds a server running
   in Docker with bridge networking, which cannot announce itself over mDNS,
   so you do not have to set an address by hand for that setup. Setting
   `pr_server` as shown above still works if you would rather pin the
   address. Once the Cub finds the server it remembers it, so later boots
   skip straight to it; if that server ever moves to a new address, the Cub
   notices the missed polls and searches again.
3. **Pairing.** The Cub asks the server to join and shows a 4 digit code on
   its screen. Your kitchen screen pops a matching request; open Settings,
   Devices, check that the codes match, and approve it. The Cub is named
   after itself automatically, and you can rename it on its device card.
   The key it receives is stored in flash, survives reboots and updates,
   and can be revoked any time from the Security pane.

Within one poll interval of approval the Cub is showing content.

## Bluetooth broadcast receive (no Wi-Fi needed)

A Pi appliance can broadcast a small status packet over Bluetooth (the
`cub_ble_advertise` setting, off by default), and a non-touch Cub can
listen for it instead of, or as a fallback to, the LAN feed. Build
`cub-tdisplay-ble.yaml` and pick the mode with the `transport` option on the
hub (exposed there as the `pr_transport` substitution):

- `auto`: a normal paired Cub while Wi-Fi and the server work; when the LAN
  feed drops, the freshest broadcast (up to 90 seconds old) fills the screen.
- `ble`: receive-only. The Cub never pairs and never needs a Wi-Fi network;
  it shows the counts, the soonest timer, and one probe temperature from the
  broadcast. Item names and buttons that talk back need the LAN.
- `lan` (the default everywhere else): exactly the behavior described above.

In a two-server household, `install_tag` pins which install the Cub listens
to; left empty, the first sender heard wins and is remembered in flash until
the device is erased. The packet parser is tested against the same byte
vectors as the Pi-side packer (`components/pantry_raider/check_vectors.py`).

## Relaying kitchen sensors to the server

A Cub has a Bluetooth radio and your server probably does not (a Docker box
on a NAS has none at all). Set `pr_ble_relay: "true"` in
`cub-tdisplay-ble.yaml` and the Cub forwards the advertisements of the
kitchen sensors near it to `POST /cub/ble-adv`, where the server decodes them
with the same decoders its own reader uses. Fridge and freezer sensors, door
sensors, and shelf buttons near the Cub then appear on the server and report
there, tagged "via Cub <name>".

```yaml
substitutions:
  pr_ble_relay: "true"
```

The server's `cub_ble_relay` setting has to be on too; with it off the Cub is
told nothing to listen for and the relay costs nothing at all.

The relay does not need the broadcast listener, so a plain LAN Cub can relay:
add an `esp32_ble_tracker:` block to your own build on top of
`cub-tdisplay.yaml` and give the hub `relay: true` (leave `transport` alone,
and leave `install_tag` out; it belongs to the broadcast listener).

How it behaves on the device: the server sends the allowlist (company ids,
service UUIDs, and name prefixes) in every `/cub/summary` reply, so a new
sensor never needs a reflash. Matching advertisements are deduped per device
over a 3 second window, batched up to ten packets or two seconds (whichever
comes first), and POSTed with the Cub's API key. An unpaired or offline Cub
drops the batch rather than queueing it. Every buffer is fixed-capacity: the
whole relay costs about 1.3 KB of RAM whether the kitchen is busy or empty.

Only sensors that broadcast their readings can be relayed. The
connect-and-notify brands (Inkbird iBBQ, ThermoPro TP25) cannot: their
advertisement carries no reading, so they need a reader in range or the Home
Assistant proxy below.

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
