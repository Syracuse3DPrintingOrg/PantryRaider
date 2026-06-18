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

Defaults, in order:

* **Expiring** shows the number of items expired or expiring within the soon
  window. Press to refresh immediately.
* **Pending** shows the number of scanned items waiting to commit. Press to
  refresh.
* **Commit** commits every pending scan into the inventory.
* **Add**, **Stock**, **Cook** open the matching pages on an attached display.
* **Brightness** cycles the deck brightness.

Status keys repaint on a timer (every 30 seconds by default), so the expiring
and pending counts stay current without you touching anything.

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

## Configuration

See `config.example.toml` for every option. The common ones:

* `base_url` points at the API, normally the local app.
* `poll_seconds` sets how often status keys refresh.
* `soon_days` sets the window the Expiring key counts against.
* `keys` is the ordered list of actions. Reorder or remove freely.
* `kiosk_cdp_url` optionally lets the nav keys steer a local kiosk browser
  through its Chrome DevTools endpoint. Without it, nav keys use the desktop
  opener.

## Tests

The configuration, layout, paging, action registry, and key rendering are
covered by `tests/test_streamdeck.py` and run without any hardware:

```bash
python -m pytest tests/test_streamdeck.py -q
```
