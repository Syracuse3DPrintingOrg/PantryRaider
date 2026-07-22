# Wired UART barcode scanner

A barcode scan engine can wire straight to a Raspberry Pi appliance's serial
pins, with no USB port used. Two kinds of module are supported:

- A **Waveshare-style 3.3V scan engine**, which Pantry Raider drives directly:
  it stays dark and idle, and only lights up and reads while you have a scan
  page open. When you leave that page it goes dark again. This suits a built-in
  scanner in an enclosure, where a bright aiming dot and a scan lamp glowing all
  day would be a nuisance.
- The **SEENGREAT Barcode Scanner Reader**, a 5V module that scans hands-free on
  its own once you have scanned two of its setup barcodes. It needs a level
  shifter on one wire, because the Pi's serial input is not 5V tolerant.

Either way, a scan is looked up and queued for review exactly as an on-screen
scan would be, and the app only listens while a scan page is open.

If you would rather plug a scanner into a USB port, do that instead: any USB HID
scanner works with no setup, and the
[Waveshare Barcode Scanner Module guide](waveshare-barcode-scanner.md) covers
running one of those hands-free. This page is only for the wired-to-the-header
option.

## What you need

- One of the two modules above, plus jumper wires. The SEENGREAT also needs
  either two resistors (1k and 2k) or a small level shifter board.
- A Raspberry Pi appliance running Pantry Raider (a full pi_hosted install). The
  reader that talks to the scanner is the same host agent that reads the plug-in
  sensors, so it runs on the appliance, not on a remote kiosk.

## Wiring a Waveshare-style module (3.3V)

The module has a small header. Connect four of its pins to the Pi's 40-pin
header, with no level shifter: the module is 3.3V logic throughout. The
scanner's transmit line goes to the Pi's receive line and the other way round,
which is the one thing people cross up: TX to RX, RX to TX.

| Scanner pin | What it is | Pi pin | Pi signal |
| --- | --- | --- | --- |
| PIN2 VCC | Power, 3.3V (needs at least 3.1V) | Pin 1 (or pin 17) | 3.3V |
| PIN3 GND | Ground | Pin 6 (or any ground) | GND |
| PIN4 RX | Scanner receive (data in) | Pin 8 | GPIO14 (UART TX) |
| PIN5 TX | Scanner transmit (data out) | Pin 10 | GPIO15 (UART RX) |

Take power from a 3.3V pin, not a 5V pin. The module's TRIG pin (PIN12, a
hardware trigger) is left unconnected: the reader triggers scans over the serial
line, so the wire is not needed. This module needs no barcode setup, and it is
the one that stays dark until a scan page is open.

## Wiring a SEENGREAT reader (5V)

The SEENGREAT reader runs on 5V, and its serial lines are 5V too. The Pi's
receive pin is 3.3V and is **not 5V tolerant**, so the module's transmit line
must be dropped to 3.3V before it reaches the Pi. Wiring it straight across
risks the Pi. Two easy ways to drop it:

- A **voltage divider** on the module's TX line: 1k from module TX to Pi pin 10,
  and 2k from Pi pin 10 to ground. Two resistors, done.
- A **TXS0108E level shifter board**: module TX and RX on the 5V side, Pi pins
  10 and 8 on the 3.3V side, VA to 3.3V, VB to 5V, and the board's OE pin tied
  to 3.3V so it is always on.

Only that one wire needs shifting. The module reads the Pi's 3.3V transmit line
fine, so Pi pin 8 connects straight to the module's RX.

| Scanner wire | What it is | Connects to |
| --- | --- | --- |
| VCC | Power, 5V | Pi pin 2 or 4 (5V) |
| GND | Ground | Pi pin 6 (or any ground) |
| TX | Scanner transmit (data out), 5V | Pi pin 10 (GPIO15, UART RX), **through the divider or shifter** |
| RX | Scanner receive (data in) | Pi pin 8 (GPIO14, UART TX), direct |

### Scan the two setup barcodes

Out of the box the SEENGREAT pretends to be a USB keyboard and its serial port
says nothing at all, so this step is not optional. In the module's manual (on
seengreat.com, the module's own wiki page) find the configuration barcodes and
scan two of them with the module itself:

1. **Serial port output mode**, so decoded barcodes go out over the wires you
   just connected instead of over USB.
2. **Continuous (auto-sensing) mode**, so the module reads hands-free whenever a
   barcode comes into view, no trigger needed.

The module remembers both settings across power cycles, so this is a one-time
step. Its serial port runs at 9600 baud, 8N1 by default, which matches the
app's defaults: leave those alone.

Unlike the Waveshare module, the SEENGREAT decides for itself when to light up
and read. Pantry Raider only listens while you have a scan page open, and codes
it reads in between are quietly discarded.

## Pin conflicts to know about

Pins 8 and 10 are the Pi's hardware UART and are otherwise free in a Pantry
Raider build. They do not clash with the I2C sensors (pins 3 and 5), the mmWave
presence sensor (pin 11), or an I2S amplifier (pins 12, 35, and 40): those are
all different pins on different buses. The one thing to know is that GPIO14 (pin
8) is also where the official Raspberry Pi 4 case fan expects its control wire,
so a wired scanner and that particular fan want the same pin. Pick one, or run
the fan off the Pi 5 fan header instead.

## Turn the serial port on

By default the Pi keeps the serial port for a login console, and on a Pi with
built-in Bluetooth the Bluetooth chip holds the very UART these pins connect to,
so a freshly wired scanner reads nothing at all. Freeing the port is a one-time
step. If you flashed the Pantry Raider SD-card image and turned the host reader
on at install time, this is already done for you; otherwise run the reader's
setup once on the appliance:

```bash
sudo foodassistant-gadgets-setup
```

That turns the hardware UART on, turns the serial login console off, moves
Bluetooth to the Pi's mini UART so the scanner gets the real one (the
`dtoverlay=miniuart-bt` line it adds to the boot config; your Bluetooth
thermometers and sensors keep working), and adds the app's service user to the
`dialout` group so it can open the port. Reboot once afterwards so the port
(`/dev/serial0`) appears. The step is harmless to run on a Pi with no scanner
wired: it just leaves an unused serial port.

## Turn it on in the app

Open Settings, then Scanning, and turn the wired UART scanner on. The defaults
(port `/dev/serial0`, 9600 baud) match the wiring above, so there is normally
nothing else to set.

From then on the scanner follows the app: present a barcode while a scan page is
open and it is looked up and queued for review, exactly as an on-screen scan
would be. What a scan does (put an item away, use one up, add it to the shopping
list, or count it for an audit) follows the scanner mode you have selected, the
same setting the kiosk and a NeoKey pad use. Close the scan page and scans stop
reaching the app until you open one again; a Waveshare module also goes
physically dark.

## If it does not read

- **Nothing happens on a scan.** Check that TX and RX are not swapped (scanner TX
  toward Pi pin 10, scanner RX toward Pi pin 8), and that the module has the
  right power: 3.3V for a Waveshare module, 5V for a SEENGREAT.
- **A SEENGREAT beeps on a scan but nothing arrives.** Its serial port is still
  off: scan the **serial port output mode** configuration barcode from the
  vendor's manual. That mode is not the factory default, so a brand-new module
  is always silent on these wires. Then check the divider or shifter on its TX
  line is wired the right way around.
- **The port is missing.** If `/dev/serial0` is not there, the UART was not freed:
  run `sudo foodassistant-gadgets-setup` and reboot once.
- **It reads but nothing reaches the app.** Make sure you are on a scan page (the
  app does not listen otherwise by design), and that the wired UART scanner is
  turned on in Settings, Scanning.
