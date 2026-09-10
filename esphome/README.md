# Bandit Cubs: ESP32 displays for Pantry Raider

A Bandit Cub is a small ESP32 screen that lives on the counter, the fridge
door, or next to the stove and shows what your kitchen needs to know right
now: items about to expire, running timers, and thermometer probes. Timers
count down smoothly on the device, and what a Cub shows is decided by
settings on your Pantry Raider server, so changing its behavior never means
reflashing it.

This directory holds everything a Cub is made of:

- `components/pantry_raider/`: the ESPHome external component. It listens for
  the kitchen's status broadcast and provides the sensors the rest of the
  firmware draws from. It can also pair with a server and poll it over
  Wi-Fi, which is how it will work again once firmware images are signed;
  that path is not what ships today.
- `packages/`: one YAML package per supported board, plus `cub-base.yaml`
  with everything the boards share.
- `cub-<profile>.yaml`: the build files, one per board.
- `cub-custom.example.yaml`: a starting point for boards we do not prebuild.

## Supported hardware

| Build file | Board | Screen | Touch |
|---|---|---|---|
| `cub-tdisplay.yaml` | LilyGo T-Display (ESP32) | 135x240 color LCD | no |
| `cub-tdisplay-ble.yaml` | LilyGo T-Display (same as above for now; see below) | 135x240 color LCD | no |
| `cub-tdisplay-s3.yaml` | LilyGo T-Display S3 | 170x320 color LCD | no |
| `cub-touch7.yaml` | Waveshare ESP32-S3 Touch LCD 7 | 7 inch, 800x480 | yes |

Every build listens for the Bluetooth broadcast now, so `cub-tdisplay-ble.yaml`
and `cub-tdisplay.yaml` produce the same firmware. The separate file stays
because the flasher, the docs and people's bookmarks name it, and it earns its
name back when the read-write transports return.

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
  # "view_cycle" walks the on-screen views forwards, "view_back" walks them
  # backwards. Those two happen on the device, so they work on a Cub as it
  # ships. Any other value is a Start Page action token posted to the server,
  # which a receive-only Cub does not do (see "Read-only for the beta").
  cub_button1_action: view_cycle
  cub_button2_action: view_back
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

## First run

1. **Turn the broadcast on.** A Cub takes its state off the air, so the
   kitchen has to be broadcasting. That is the **Bandit Cub broadcast**
   setting on your Pantry Raider install, and it needs a Bluetooth radio on
   whichever machine runs the gadgets agent. Nothing is paired and no address
   is configured, on either side.
2. **Power the Cub.** It listens, and shows a waiting line until the first
   broadcast lands. The first install it hears is remembered in flash, so a
   second kitchen appearing later cannot take the screen over.
3. **Wi-Fi is optional.** There is nothing on the network a Cub needs. Join it
   to Wi-Fi if you want the Home Assistant sensors or the logs (Improv over
   the USB cable, Improv over Bluetooth from a phone, or the captive-portal
   hotspot); skip it and the Cub works exactly the same.

If two installs are broadcasting in range of each other, pin which one a Cub
listens to with `pr_install_tag` rather than letting the first one heard win.

## Read-only for the beta

A Cub listens and displays. It does not pair, hold a key, poll, or send
anything back, and nothing on the network can put firmware on it.

That is narrower than the firmware can do, and it is deliberate. The
read-write design had a hole worth more than the features it bought: a Cub
found its server over unauthenticated mDNS, believed that server when it said
pairing had been approved, and then installed whatever firmware that server
offered, on a timer, with nobody touching the device. Anything on the network
could hand a Cub code and the Cub would run it. Closing half of that (push
updates were removed earlier) read like the question had been settled, which
was worse than closing neither.

So for the beta:

- **Firmware changes one way only**: plug the Cub into a computer and flash it
  from the Bandit Cubs page. There is no network path onto the device at all.
- **Buttons do local things.** `view_cycle` and `view_back` walk the on-screen
  views. Action tokens (starting a timer, adding to the shopping list) post to
  the server, so they do nothing until read-write returns.
- **The sensor relay is off**, for the same reason: forwarding what a Cub
  hears means posting it.

Signed firmware images are the road back to automatic updates, and to the
write features with them. Until then a Cub is a display, and a display cannot
be talked into running someone else's code.

## How the broadcast works

Whichever machine runs the gadgets agent polls `GET /cub/summary` locally and
re-broadcasts the glanceable numbers as a 23-byte Bluetooth advertisement (the
**Bandit Cub broadcast** setting, off by default). A Cub listens for it. That
is the whole path: no wifi, no pairing, no key, and no per-Cub state on the
server at all.

What travels: the expired, expiring and pending counts, the timer count and
the soonest one, one probe temperature and how far it is from target, plus
flags for a ringing timer, a probe at target, and an attention alert. What
does not: item names and anything that would need a request back. The packet
parser is tested against the same byte vectors as the sending side
(`components/pantry_raider/check_vectors.py`), so both ends move together.

In a two-kitchen household, `pr_install_tag` pins which install a Cub listens
to. Left empty, the first sender heard wins and is remembered in flash until
the device is erased. Worth pinning if a hostile broadcaster could plausibly
be in radio range when the Cub first boots: the broadcast is not
authenticated, so anything in range can put numbers on the screen.

The `transport` option on the hub still accepts `lan` and `auto`, and the
component still implements them. They pair, hold a key, and let the firmware
check follow whichever server they found, which is why they are not what
ships. See "Read-only for the beta" above.

## Relaying kitchen sensors to the server

Off for the beta. A Cub has a Bluetooth radio and your server probably does
not, so forwarding the sensors near a Cub to the server is genuinely useful,
and it is the feature most worth having back. It needs the Cub to post what it
hears, which a receive-only Cub does not do.

The code is still there behind the hub's `relay: true` option and the server's
`cub_ble_relay` setting, and a build of your own can turn it on, but doing so
means choosing `transport: lan` or `auto` and taking the pairing path with it.

## The same device in Home Assistant

Every Cub is a normal ESPHome device, so Home Assistant discovers it
natively, no Pantry Raider integration required. Adopting it adds entities
for the expiring counts, pending scans, active timers, the next timer,
probe temperature, the current view, and the pairing code, all fed from the
same kitchen summary the screen renders.

## Your own board

`cub-custom.example.yaml` is the escape hatch: copy it, wire in your board
and display, and build it in your own ESPHome dashboard. The
`pantry_raider:` component gives you the hub and the `sensor`/`text_sensor`
platforms. It also carries three actions, all of which post to the server, so
they need `transport: lan` or `auto` and the pairing that comes with them.
They do nothing on a Cub as it ships:

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
