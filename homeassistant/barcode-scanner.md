# Headless Wireless Barcode Scanner

Scan groceries with a dedicated wireless barcode scanner - no phone, no
browser. Each scan is forwarded to Pantry Raider's `/pending/scan` endpoint
via an HA automation and REST command, then appears in `/ui/pending` with
name, category, storage, and best-by date pre-filled from Open Food Facts
and your defaults rules. Review and commit when ready.

## How it works

Almost every wireless barcode scanner (Eyoyo, Tera, Inateck, NETUM...)
presents itself as a **HID keyboard**: it "types" the barcode digits
followed by Enter. Home Assistant's
[`keyboard_remote`](https://www.home-assistant.io/integrations/keyboard_remote/)
integration listens to that input device and fires an event per keypress.
An automation buffers digits and submits the completed barcode to
Pantry Raider when Enter is received.

How the scanner reaches the host depends on the model - most support one
or both of:

- **2.4 GHz / 433 MHz USB dongle** - a receiver that plugs into a USB port
  and shows up as a keyboard. Easiest and most reliable; use this if your
  scanner came with one. See Option A below.
- **Classic Bluetooth HID** - pairs directly with the host's Bluetooth
  adapter. See Option B below. *Note: Bluetooth pairing support is pending
  testing - the USB dongle path is recommended.*

> **Requirement:** `keyboard_remote` only works on Home Assistant OS /
> Supervised / Core on Linux with access to `/dev/input`. It does not work
> on HA running in Docker without `--device` mappings for the input device.

## Setup

### 1. Connect the scanner to the HA host

#### Option A: USB receiver dongle (recommended)

If your scanner came with a wireless USB dongle, just plug it into a USB
port on the HA host - the scanner and dongle are factory-paired, so there
is nothing to configure. No re-pairing after reboots, no Bluetooth
disconnect issues, and usually better range than BT.

The dongle registers as a USB keyboard. Note that its device name often
won't mention "barcode" - expect something generic like
`Telink Wireless Receiver` or `SM SM-2D PRODUCT HID KBW`. To find it:

- **HA UI:** Settings - System - Hardware - three-dot menu - All Hardware
  and look for the new input device that appeared after plugging in the
  dongle, or
- **Shell:** `cat /proc/bus/input/devices` and check which entry is new.

Note the exact name and skip to Step 2.

> If HA runs in Docker (not HA OS/Supervised), pass the device through
> with `--device /dev/input/eventX` as noted above.

#### Option B: Bluetooth pairing

> **Status: pending testing.** Classic Bluetooth HID pairing works at the
> OS level but has not been fully verified with the add-on path. The steps
> below should work on HA OS/Supervised; file an issue if you run into
> problems.

HA's Bluetooth integration is for BLE sensors - it won't pair a classic
Bluetooth HID device like a barcode scanner. Pairing happens at the OS
level with `bluetoothctl`.

**Get a shell on the host.** On HA OS, install the
[Advanced SSH & Web Terminal](https://github.com/hassio-addons/addon-ssh)
add-on and turn **Protection mode off** (required for host-level access),
then open its terminal. On Supervised/Core installs, just SSH into the
machine as usual.

**Put the scanner in pairing mode.** Most scanners enter Bluetooth HID
pairing by scanning a setup barcode printed in their manual (often labeled
"Bluetooth HID mode" or "pairing"). The LED usually blinks blue while
discoverable.

**Pair, trust, and connect:**

```
$ bluetoothctl
[bluetooth]# power on
[bluetooth]# agent on
[bluetooth]# default-agent
[bluetooth]# scan on
# wait for the scanner to show up, e.g.:
#   [NEW] Device A1:B2:C3:D4:E5:F6 Barcode Scanner HID
[bluetooth]# scan off
[bluetooth]# pair A1:B2:C3:D4:E5:F6
[bluetooth]# trust A1:B2:C3:D4:E5:F6     # auto-reconnect after reboots/sleep
[bluetooth]# connect A1:B2:C3:D4:E5:F6
[bluetooth]# exit
```

If `pair` asks for a PIN, try `0000` or `1234` (check the scanner manual).

**Verify it registered as an input device:**

```
cat /proc/bus/input/devices | grep -A4 -i barcode
```

You can also check in the HA UI: **Settings - System - Hardware - three-dot
menu - All Hardware**. Note the exact device name (e.g. `Barcode Scanner HID`)
for the next step.

### 2. Configure keyboard_remote

In `configuration.yaml` (requires a full HA restart, not just a YAML reload):

```yaml
keyboard_remote:
  - device_name: "BarCode WPM USB"   # exact name from the hardware list
    type:
      - key_down
```

If the name is ambiguous, the stable by-id path is more reliable:

```yaml
keyboard_remote:
  - device_descriptor: /dev/input/by-id/usb-BarCode_WPM_USB-event-kbd
    type:
      - key_down
```

### 3. Add the rest_command

Already included in this repo's `configuration.yaml`:

```yaml
rest_command:
  foodassistant_scan:
    url: http://YOUR_HOST:9284/pending/scan
    method: POST
    content_type: "application/json"
    # Required if Pantry Raider has API_KEY set - without it the POST is
    # rejected with 401 and HA only logs a warning (the automation trace
    # still looks successful).
    headers:
      X-API-Key: !secret foodassistant_api_key
    payload: '{"barcode": "{{ barcode }}", "source": "ha"}'
```

Replace `YOUR_HOST` with your Pantry Raider host:
- **Docker standalone:** your server's IP or hostname, e.g. `192.168.1.50`
- **HA add-on with mapped port:** `<HA-IP>:<mapped-port>`, e.g. `192.168.1.10:9284`

### 4. Add the barcode-assembly automation

Scanners type one key per digit, so the automation buffers digits in a
helper and submits on Enter. First create the helper: **Settings -
Devices & Services - Helpers - Create Helper - Text**, name it
`barcode_buffer`. Then create the automation (**Settings - Automations -
+ Create Automation - Skip - three-dot menu - Edit in YAML**) and paste:

```yaml
alias: Barcode scanner - Pantry Raider
mode: queued
triggers:
  - trigger: event
    event_type: keyboard_remote_command_received
    event_data:
      device_name: BarCode WPM USB   # match your scanner's device name
actions:
  - variables:
      # key_code 28 = Enter; 2-11 are digits 1234567890 on the number row.
      # Compare as integers: HA coerces template variable results back to
      # native types, so a |string cast gets undone and string comparisons
      # silently never match.
      key: "{{ trigger.event.data.key_code | int }}"
      # Helper state is 'unknown' until first written - treat that as empty
      buffer: >-
        {{ states('input_text.barcode_buffer')
           if states('input_text.barcode_buffer') not in ['unknown', 'unavailable']
           else '' }}
  - choose:
      # Enter pressed - submit the buffered barcode
      - conditions:
          - condition: template
            value_template: "{{ key == 28 }}"
        sequence:
          - action: rest_command.foodassistant_scan
            data:
              barcode: "{{ buffer }}"
          - action: input_text.set_value
            target:
              entity_id: input_text.barcode_buffer
            data:
              value: ""
      # Digit pressed - append to the buffer. Codes 2-11 are 1..9,0 in
      # order, so the digit is computed arithmetically. If the buffer has
      # already grown past a real barcode's length (24), Enter must have been
      # missed on the previous scan, so start fresh with this digit instead of
      # concatenating two scans into one nonsense code. The server also refuses
      # anything longer than 24, so the pending list never fills with garbage.
      - conditions:
          - condition: template
            value_template: "{{ 2 <= key <= 11 }}"
        sequence:
          - action: input_text.set_value
            target:
              entity_id: input_text.barcode_buffer
            data:
              value: >-
                {{ ((key - 1) % 10) if (buffer | length) >= 24
                   else (buffer ~ ((key - 1) % 10)) }}
```

> If scans keep arriving concatenated (one long code spanning several
> products), the buffer is not clearing on Enter: your scanner may send a
> terminator other than Enter (key_code 28), or send no terminator at all.
> Check **Developer Tools - Events** for the terminator's `key_code` and add
> it to the submit condition (e.g. `{{ key in [28, 96] }}` to also accept the
> keypad Enter), or configure the scanner to append a carriage return suffix.

**Debugging tips:**

- **Developer Tools - Events**, listen to `keyboard_remote_command_received`,
  pull the trigger: you should see one event per digit plus Enter (28). No
  events means `keyboard_remote` isn't attached; check the device name/descriptor
  and do a full restart (YAML reload is not enough).
- Watch `input_text.barcode_buffer` in **Developer Tools - States** while
  scanning - digits should accumulate, then clear on Enter.
- Automation triggers but "Choose: No action executed" in the trace means a
  key-code/type mismatch; check the `key` value in the trace's Changed
  variables tab.
- Buffer fills but nothing reaches `/ui/pending` means the `rest_command` is
  failing (check **Settings - System - Logs**). If Pantry Raider has
  `API_KEY` set, the `X-API-Key` header is required - see step 3.

### 5. Optional: scan-received notification

```yaml
  - alias: "Notify on new pending scan"
    trigger:
      - platform: state
        entity_id: sensor.food_pending_scans
    condition:
      - "{{ trigger.to_state.state | int(0) > trigger.from_state.state | int(0) }}"
    action:
      - service: notify.mobile_app_your_phone
        data:
          message: >-
            Scanned item queued - {{ states('sensor.food_pending_scans') }} pending review.
          data:
            url: http://YOUR_HOST:9284/ui/pending
```

## ESP32 alternative

If your scanner can't reach the HA host (e.g. scanning in a pantry away
from the hub), an ESP32 can host the scanner instead. Two options:

- **USB scanner + ESP32-S3**: use ESPHome's `usb_host` support or a
  USB-HID-to-serial firmware, then an `http_request` to
  `POST /pending/scan` per read. Wired, reliable.
- **BLE scanner + ESP32**: requires the scanner to support BLE mode (not
  just classic Bluetooth HID) and custom NimBLE HID-host firmware -
  significantly more work; recommended only if the HA path is impossible.

Either way the ESP32 just needs to send:

```
POST http://YOUR_HOST:9284/pending/scan
Content-Type: application/json
X-API-Key: <your key, if auth enabled>

{"barcode": "049000028935", "source": "esp32"}
```

## Testing without a scanner

```bash
curl -s -X POST http://YOUR_HOST:9284/pending/scan \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_KEY" \
  -d '{"barcode": "049000028935", "source": "manual"}'
```

Then check `/ui/pending` - a Coca-Cola entry should appear with defaults
filled in. Unknown barcodes are queued too (flagged with a warning icon), so
a scan is never lost.
