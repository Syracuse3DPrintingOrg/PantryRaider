# FoodAssistant Bluetooth thermometer reader

Read Bluetooth kitchen thermometers and show their probes live in Pantry
Raider, with target-temperature alerts on every screen. Runs on the host (the
Bluetooth radio is not reachable from inside the app container) as its own
small service, the same pattern as the Stream Deck controller.

Supported thermometers:

* **Inkbird IBT-2X / IBT-4XS / IBT-6XS** (and other iBBQ-protocol devices):
  every probe, plus battery level.
* **ThermoPro TP25-style** Bluetooth BBQ thermometers (TP25 / TP25W and
  similar): every probe, plus battery level.
* **Combustion Inc Predictive Thermometer**: all eight sensors, read straight
  from the probe's advertisements, so no connection or pairing is needed.
* **ThermoWorks BlueDOT**: the probe temperature and the device's own alarm
  state.

## How it works

The reader keeps a passive Bluetooth scan running. Thermometers it recognizes
but that you have not added yet show up on the Timers page under Probes with
an Add button. Added thermometers are connected (Combustion probes are simply
listened to) and their readings are posted to the app every few seconds, so
the temperatures appear on the Timers page and target alerts pop up as
toasts on the kiosk and in the browser.

Which thermometers to read lives in the app's settings, not on this host: the
reader pulls its device list from the app, so adding a thermometer in the web
UI is all it takes.

## Install

On the appliance, `foodassistant-gadgets-setup` does all of this in one step.
By hand, on any Linux machine with a Bluetooth radio near the kitchen:

```bash
python -m venv /opt/foodassistant/venv
/opt/foodassistant/venv/bin/pip install -r gadgets/requirements.txt
cp -r gadgets/foodassistant_gadgets /opt/foodassistant/foodassistant_gadgets
cp gadgets/config.example.toml /etc/foodassistant/gadgets.toml
sudo cp gadgets/systemd/foodassistant-gadgets.service /etc/systemd/system/
sudo systemctl enable --now foodassistant-gadgets
```

If your install requires authentication for LAN clients, put the API key in
an environment file rather than the config:

```bash
echo 'FOODASSISTANT_API_KEY=your-key-here' | sudo tee /etc/foodassistant/gadgets.env
```

Then turn on Bluetooth thermometers in the app (the Probes section appears on
the Timers page once the reader reports in).

## Run in the foreground

For a quick test:

```bash
/opt/foodassistant/venv/bin/python -m foodassistant_gadgets \
  --config /etc/foodassistant/gadgets.toml --verbose
```

## Configuration

See `config.example.toml` for every option. The common ones:

* `base_url` points at the app, normally localhost.
* `push_seconds` sets how often readings are sent.
* `scan` keeps the discovery/advertising scan running (needed for Combustion
  probes and for the "available to add" list).

## Tests

The payload decoders and the app-side state and alert logic are pure and run
without any hardware:

```bash
python -m pytest tests/test_gadgets.py -q
```
