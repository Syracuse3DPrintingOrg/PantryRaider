# FoodAssistant Stream Deck controller

Drive an Elgato Stream Deck, or an embedded Stream Deck Module, as a physical
control surface for a FoodAssistant install. The deck shows live status on its
keys (how many items are expiring soon, how many scans are waiting to commit)
and triggers app actions when you press them. It works alongside a touchscreen
or as the only interface on a headless countertop appliance.

Supported sizes:

* Stream Deck Mini and Stream Deck Module 6 (6 keys)
* Stream Deck, MK.2, and Stream Deck Module 15 (15 keys)
* Stream Deck XL and Stream Deck Module 32 (32 keys)

The controller picks a layout to match whatever is plugged in. On the 6-key
Mini, extra actions move onto further pages reached by a "More" key.

## What the keys do

The common keys:

* **Expiring** shows the number of items expired or expiring within the soon
  window. Press to refresh immediately.
* **Pending** shows the number of scanned items waiting to commit. Press to
  refresh.
* **Commit** commits every pending scan into the inventory.
* **Pantry**, **Stock**, **Cook**, and the other nav keys open the matching
  pages on an attached display.
* **Scan Mode** cycles the barcode scanner between adding stock, consuming
  stock, adding to the shopping list, and a read-only pantry audit, and shows
  the active mode on its face. The mode is shared with the Manage Pantry
  page's tabs, so the deck and every screen agree.
* **Timer keys** run the shared kitchen timers and show the live countdown on
  the key face: a press starts the timer, each press while it runs adds a
  minute, a press on a finished (flashing) timer dismisses it, and holding
  the key resets it. Timers live on the main server, so they also appear on
  the Timers page and every other screen, and vice versa.
* **Cooked** marks the recipe on the line as cooked and takes its ingredients
  out of your inventory. Because it uses up real stock, the first press only
  turns the key red with "Press again" and the kitchen screen names the
  recipe; press it again within five seconds to mark it cooked. Wait longer
  and the next press asks again. A custom macro key never runs Cooked, since
  a macro fires on a single press.
* **Brightness** cycles the deck brightness.
* **Unlock** (the `pin` action) turns the deck into a numeric keypad so you can
  unlock the PIN-locked app without a keyboard or touchscreen. Tap the digits
  (they show masked, never as the real code), press Enter to submit, or Cancel
  to back out. On success the deck returns to the normal layout; a wrong code
  clears the entry and flashes a brief error.

Status keys repaint on a timer (every 30 seconds by default), so the expiring
and pending counts stay current without you touching anything.

The key layout, custom keys (Home Assistant actions, quick-add shopping items,
timers, weather, cameras, media, macros), the key style, and the icon colours
are all edited in the app under Settings, Start Page and Settings, Stream
Deck; the deck picks changes up on its next sync. While the service starts,
the keys show the Pantry Raider raccoon instead of the factory logo, and the
same raccoon covers the keys while the kiosk display is asleep (a switch in
those settings, on by default). Pressing any key or touching the screen wakes
the display and the deck together; the deck's own idle timeout still blanks
the keys fully after its own quiet period.

## Install

On the appliance, or any machine with the deck attached:

```bash
python -m venv /opt/foodassistant/venv
/opt/foodassistant/venv/bin/pip install -r streamdeck/requirements.txt
```

Give the controller permission to talk to the USB device without root:

```bash
sudo cp streamdeck/udev/99-streamdeck.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG plugdev "$USER"      # log out and back in afterward
```

Copy and edit the config:

```bash
cp streamdeck/config.example.toml /etc/foodassistant/streamdeck.toml
```

If your install requires authentication, put the API key in an environment
file rather than the config so it stays out of version control:

```bash
echo 'FOODASSISTANT_API_KEY=your-key-here' | sudo tee /etc/foodassistant/streamdeck.env
```

## Run

Foreground, for a quick test:

```bash
/opt/foodassistant/venv/bin/python -m foodassistant_streamdeck \
  --config /etc/foodassistant/streamdeck.toml --verbose
```

As a service:

```bash
sudo cp streamdeck/systemd/foodassistant-streamdeck.service /etc/systemd/system/
sudo systemctl enable --now foodassistant-streamdeck
```

The unit assumes a `foodassistant` user, a virtualenv at
`/opt/foodassistant/venv`, and the package importable from
`/opt/foodassistant`. Adjust the paths if your layout differs.

If the log says `No Stream Deck found` even though the deck is lit, the USB
cable is usually the culprit: many USB-C and micro-USB cables carry power
only, no data. Random disconnects instead point at an undersized power
supply. See [Power and cabling](../docs/hardware.md#power-and-cabling).

## Configuration

See `config.example.toml` for every option. The common ones:

* `base_url` points at the API, normally the local app.
* `poll_seconds` sets how often status keys refresh.
* `soon_days` sets the window the Expiring key counts against.
* `keys` is the ordered list of actions. Reorder or remove freely.
* The nav keys open their page on the kitchen screen through the kiosk's
  navigation socket (`kiosk_nav_socket`), so the screen stays signed in.
  When the socket is not there they fall back to the browser's debugging
  endpoint (`kiosk_cdp_url`), then to the desktop opener. On a Pantry Raider
  kitchen screen the debugging port is closed by default; to profile the
  kiosk browser, run `sudo foodassistant-kiosk-devtools on` first.
* `api_key` (or `FOODASSISTANT_API_KEY`) goes only to `base_url`, never to a
  camera, Home Assistant or the kiosk browser.
* `ha_policy_version` is set by Pantry Raider. At 1, a Home Assistant key
  that reaches a lock, an alarm, a garage door or similar shows "Needs OK"
  and does nothing until you allow it in Settings, Start Page.

## Tests

The configuration, layout, paging, action registry, and key rendering are
covered by `tests/test_streamdeck.py` and run without any hardware:

```bash
python -m pytest tests/test_streamdeck.py -q
```
