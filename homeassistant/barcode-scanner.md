# Headless Bluetooth Barcode Scanner → Pending Queue

Scan groceries with a dedicated Bluetooth barcode scanner — no phone, no
browser. Each scan lands in FoodAssistant's **Pending** page
(`/ui/pending`) with name, category, storage, and best-by date pre-filled
from Open Food Facts + your defaults rules. Review and commit when ready.

## How it works

```
BT scanner ──HID──► Home Assistant host ──keyboard_remote──► automation
                                                              │
                                          rest_command ◄──────┘
                                              │
                                              ▼
                            POST /pending/scan  (FoodAssistant)
                                              │
                                              ▼
                                     /ui/pending  (review + commit)
```

Almost every Bluetooth barcode scanner (Eyoyo, Tera, Inateck, NETUM…)
pairs as a **HID keyboard**: it "types" the barcode digits followed by
Enter. Home Assistant's
[`keyboard_remote`](https://www.home-assistant.io/integrations/keyboard_remote/)
integration listens to that input device and fires an event per keypress.

> **Requirement:** `keyboard_remote` only works on Home Assistant OS /
> Supervised / Core on Linux with access to `/dev/input`. It does not work
> on HA running in Docker without `--device` mappings for the input device.

## Setup

### 1. Confirm the scanner appears as an input device

Once the scanner is connected to the HA host, go to
**Settings → System → Hardware → ⋮ → All Hardware** and confirm it appears
as an input device. Note its exact name (e.g. `Barcode Scanner HID`) — you'll
need it in the next step.

### 2. Configure keyboard_remote

In `configuration.yaml`:

```yaml
keyboard_remote:
  - device_name: "Barcode Scanner HID"   # exact name from /dev/input
    type:
      - key_down
```

### 3. Add the rest_command

Already included in this repo's `configuration.yaml`:

```yaml
rest_command:
  foodassistant_scan:
    url: http://192.168.1.170:9284/pending/scan
    method: POST
    content_type: "application/json"
    payload: '{"barcode": "{{ barcode }}", "source": "ha"}'
```

### 4. Add the barcode-assembly automation

Scanners type one key per digit, so the automation buffers digits in a
helper and submits on Enter. First create the helper — **Settings →
Devices & Services → Helpers → Create Helper → Text**, name it
`barcode_buffer` — then add:

```yaml
automation:
  - alias: "Barcode scanner → FoodAssistant"
    mode: queued
    trigger:
      - platform: event
        event_type: keyboard_remote_command_received
        event_data:
          device_name: "Barcode Scanner HID"   # match your scanner
    action:
      - variables:
          # key_code 28 = Enter; 2-11 are digits 1234567890 on row keys
          key: "{{ trigger.event.data.key_code }}"
          digit_map: { 2: "1", 3: "2", 4: "3", 5: "4", 6: "5",
                       7: "6", 8: "7", 9: "8", 10: "9", 11: "0" }
      - choose:
          # Enter pressed → submit the buffered barcode
          - conditions: "{{ key == 28 }}"
            sequence:
              - service: rest_command.foodassistant_scan
                data:
                  barcode: "{{ states('input_text.barcode_buffer') }}"
              - service: input_text.set_value
                target: { entity_id: input_text.barcode_buffer }
                data: { value: "" }
          # Digit pressed → append to the buffer
          - conditions: "{{ key in digit_map }}"
            sequence:
              - service: input_text.set_value
                target: { entity_id: input_text.barcode_buffer }
                data:
                  value: "{{ states('input_text.barcode_buffer') ~ digit_map[key] }}"
```

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
            Scanned item queued — {{ states('sensor.food_pending_scans') }} pending review.
          data:
            url: https://fass.korolev.link/ui/pending
```

## ESP32 alternative

If your scanner can't reach the HA host (e.g. scanning in the garage
pantry), an ESP32 can host the scanner instead. Two options:

- **USB scanner + ESP32-S3**: use ESPHome's `usb_host` support or a
  USB-HID-to-serial firmware, then an `http_request` to
  `POST /pending/scan` per read. Wired, reliable.
- **BLE scanner + ESP32**: requires the scanner to support BLE mode (not
  just classic Bluetooth HID) and custom NimBLE HID-host firmware —
  significantly more work; recommended only if the HA path is impossible.

Either way the ESP32 just needs to send:

```
POST http://192.168.1.170:9284/pending/scan
Content-Type: application/json
X-API-Key: <your key, if auth enabled>

{"barcode": "049000028935", "source": "esp32"}
```

## Testing without a scanner

```bash
curl -s -X POST http://192.168.1.170:9284/pending/scan \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_KEY" \
  -d '{"barcode": "049000028935", "source": "manual"}'
```

Then check `/ui/pending` — a Coca-Cola entry should appear with defaults
filled in. Unknown barcodes are queued too (flagged ⚠), so a scan is
never lost.
