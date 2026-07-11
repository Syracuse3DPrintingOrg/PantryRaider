# ESP devices

A cheap ESP32 or ESP8266 flashed with [ESPHome](https://esphome.io) can talk to
Pantry Raider directly over your network, no Bluetooth radio and no Home
Assistant in between. Two kinds of DIY hardware are supported:

- **Sensors that report in**, like a WiFi temperature probe for a fridge,
  freezer, or room. These show up on the Timers page as regular thermometer
  probes. Setup lives with the rest of the thermometers; see
  [Bluetooth kitchen thermometers](thermometers.md#adding-an-esp-device).
- **Buttons that fire actions**, like a physical button that starts a kitchen
  timer or flips a Home Assistant light. That is what the rest of this page
  covers.

## Buttons and timers

A button on an ESP device works by sending Pantry Raider a short web request
when it is pressed. The request names an action, and Pantry Raider runs it, the
same action a key on the on-screen Start Page or a Stream Deck would run. So a
single button can:

- **Start a kitchen timer.** Press once to start, press again to add a minute,
  press an expired timer to dismiss it, hold to reset. Every screen in the
  house sees the same countdown, because it is a shared timer.
- **Fire a Home Assistant action**, like a light, a scene, or a script, using
  the Home Assistant connection Pantry Raider already has.

### What you need

- The address of your Pantry Raider server on your network, for example
  `http://192.168.1.170:9284`.
- Your Pantry Raider **API key**. It is on the Settings page under Home
  Assistant (the same key headless devices use). Copy it into the firmware
  below.

### The action names

Put one of these names in the button's request. They are the same names the
Stream Deck and Start Page use.

| Name | What the button does |
| --- | --- |
| `timer_1`, `timer_2`, `timer_3` | A general timer that steps through 5, 10, 15, 30, and 60 minutes on repeated presses |
| `timer_eggs` | Start a 6 minute Eggs timer |
| `timer_pasta` | Start a 10 minute Pasta timer |
| `timer_rice` | Start an 18 minute Rice timer |
| `ha_1` through `ha_5` | Fire a legacy Home Assistant slot, if your device still has one saved. New setups: build a custom Home Assistant key in the Start Page editor and use its key id (`c1`, `c2`, ...) here instead |

A long press (see the firmware note below) cancels or resets a timer button.

### ESPHome firmware

Add the `web_server` component (so Pantry Raider can also read any sensors on
the board) and one `http_request` call per button. This example wires two
physical buttons: a short press on the first starts an eggs timer, and the
second toggles a Home Assistant action. Replace the address and the API key with
your own.

```yaml
esphome:
  name: kitchen-buttons

# Lets Pantry Raider read any sensors on this board too.
web_server:
  port: 80

http_request:
  useragent: esphome/pantryraider

binary_sensor:
  - platform: gpio
    pin: GPIO4
    name: "Eggs button"
    on_press:
      then:
        - http_request.post:
            url: "http://192.168.1.170:9284/gadgets/esp-action"
            headers:
              Content-Type: "application/json"
              X-API-Key: "PASTE-YOUR-PANTRY-RAIDER-API-KEY"
            json:
              button: "timer_eggs"

  - platform: gpio
    pin: GPIO5
    name: "Kitchen light button"
    on_press:
      then:
        - http_request.post:
            url: "http://192.168.1.170:9284/gadgets/esp-action"
            headers:
              Content-Type: "application/json"
              X-API-Key: "PASTE-YOUR-PANTRY-RAIDER-API-KEY"
            json:
              button: "ha_1"
```

To make a button a long press (cancel or reset a timer), send `"long": true`
alongside the button name, for example from an `on_click` with a minimum press
length in ESPHome.

Once flashed, press the button. The timer appears on every Timers page a moment
later, or the Home Assistant action fires. If nothing happens, double check the
server address and that the API key matches the one on your Settings page.

## Screens

An ESP with a small OLED or e-ink screen can show a compact status: any running
kitchen timers and the recipe you are cooking. Pantry Raider serves this at
`GET /gadgets/esp-screen`, and it is built only from what the server already
knows locally, so it answers instantly even when a screen polls it every few
seconds.

The reply has a ready-to-print `lines` list (already trimmed to fit a narrow
display) plus the raw `timers` if you would rather lay it out yourself:

```json
{
  "ok": true,
  "lines": ["Eggs DONE", "Pasta 5:00", "Cook: Carbonara"],
  "timers": [
    {"label": "Eggs", "remaining": 0, "expired": true},
    {"label": "Pasta", "remaining": 300, "expired": false}
  ],
  "recipe": "Carbonara"
}
```

Pass `?lines=2` (or up to 8) to match how many rows your screen has. Point your
ESPHome display's HTTP fetch at the address, include the same `X-API-Key`
header as the buttons above, and print the `lines` one per row. A finished timer
is listed first so it is the first thing you see.
