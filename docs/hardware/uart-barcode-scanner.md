# Wired UART barcode scanner

A Waveshare-style barcode scan engine can wire straight to a Raspberry Pi
appliance's serial pins, with no USB port used and no level shifter. The Pantry
Raider host reader drives it directly: the scanner stays dark and idle, and only
lights up and reads while you have a scan page open. When you leave that page it
goes dark again. This suits a built-in scanner in an enclosure, where a bright
aiming dot and a scan lamp glowing all day would be a nuisance.

If you would rather plug a scanner into a USB port, do that instead: any USB HID
scanner works with no setup, and the
[Waveshare Barcode Scanner Module guide](waveshare-barcode-scanner.md) covers
running one of those hands-free. This page is only for the wired-to-the-header
option.

## What you need

- A Waveshare Barcode Scanner Module, or another scan engine that speaks 3.3V
  UART at 9600 baud. It must be the 3.3V version: the Pi's serial pins are 3.3V
  and are not 5V tolerant.
- Four jumper wires.
- A Raspberry Pi appliance running Pantry Raider (a full pi_hosted install). The
  reader that talks to the scanner is the same host agent that reads the plug-in
  sensors, so it runs on the appliance, not on a remote kiosk.

## Wiring

The module has a small header. Connect four of its pins to the Pi's 40-pin
header. The scanner's transmit line goes to the Pi's receive line and the other
way round, which is the one thing people cross up: TX to RX, RX to TX.

| Scanner pin | What it is | Pi pin | Pi signal |
| --- | --- | --- | --- |
| PIN2 VCC | Power, 3.3V (needs at least 3.1V) | Pin 1 (or pin 17) | 3.3V |
| PIN3 GND | Ground | Pin 6 (or any ground) | GND |
| PIN4 RX | Scanner receive (data in) | Pin 8 | GPIO14 (UART TX) |
| PIN5 TX | Scanner transmit (data out) | Pin 10 | GPIO15 (UART RX) |

Take power from a 3.3V pin, not a 5V pin. The module's TRIG pin (PIN12, a
hardware trigger) is left unconnected: the reader triggers scans over the serial
line, so the wire is not needed.

Pins 8 and 10 are the Pi's hardware UART and are otherwise free in a Pantry
Raider build. They do not clash with the I2C sensors (pins 3 and 5), the mmWave
presence sensor (pin 11), or an I2S amplifier (pins 12, 35, and 40): those are
all different pins on different buses. The one thing to know is that GPIO14 (pin
8) is also where the official Raspberry Pi 4 case fan expects its control wire,
so a wired scanner and that particular fan want the same pin. Pick one, or run
the fan off the Pi 5 fan header instead.

## Turn the serial port on

By default the Pi keeps the serial port for a login console, which would fight
the scanner for the line. Freeing the port is a one-time step. If you flashed the
Pantry Raider SD-card image and turned the host reader on at install time, this
is already done for you; otherwise run the reader's setup once on the appliance:

```bash
sudo foodassistant-gadgets-setup
```

That turns the hardware UART on, turns the serial login console off, and adds the
app's service user to the `dialout` group so it can open the port. Reboot once
afterwards so the port (`/dev/serial0`) appears. The step is harmless to run on a
Pi with no scanner wired: it just leaves an unused serial port.

## Turn it on in the app

Open Settings, then Scanning, and turn the wired UART scanner on. The defaults
(port `/dev/serial0`, 9600 baud) match the wiring above, so there is normally
nothing else to set.

From then on the scanner follows the app. It lights up and reads only while a
scan session is active, which is what having a scan page open does: present a
barcode and it is looked up and queued for review, exactly as an on-screen scan
would be. What a scan does (put an item away, use one up, add it to the shopping
list, or count it for an audit) follows the scanner mode you have selected, the
same setting the kiosk and a NeoKey pad use. Close the scan page and the scanner
goes dark and stops reading until you open one again.

## If it does not read

- **Nothing happens on a scan.** Check that TX and RX are not swapped (scanner TX
  to Pi pin 10, scanner RX to Pi pin 8), and that the module has 3.3V power.
- **The port is missing.** If `/dev/serial0` is not there, the UART was not freed:
  run `sudo foodassistant-gadgets-setup` and reboot once.
- **It reads but nothing reaches the app.** Make sure you are on a scan page (the
  scanner is dark otherwise by design), and that the wired UART scanner is turned
  on in Settings, Scanning.
