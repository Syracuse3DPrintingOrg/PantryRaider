# Pin map and accessory compatibility

This page is for planning a build before you buy the parts. It says which pins
on a Raspberry Pi's 40-pin header Pantry Raider actually uses, which accessories
can share a board, and which combinations fight each other.

None of this is required. Pantry Raider is a web app you can reach from any
browser on your network, and every accessory on this page is optional. A Pi with
nothing attached but a power supply runs the whole thing. Read this page when you
are choosing between a PoE HAT and a USB-C supply, adding a plug-in sensor, or
wondering whether two boards you already own can sit on the same Pi. If you run
on a mini PC or a server, none of it applies.

Everything below is about Raspberry Pi 4 and Pi 5 unless a line says otherwise.

## Power input

Pick one primary source. The three options are not equal.

### USB-C power supply (recommended default)

A Pi 4 wants 5V at 3A (15W). A Pi 5 wants 5V at 5A (27W), and the official 27W
USB-C supply is the one to buy. The wattage is not vanity: a Pi 5 limits its four
USB ports to a shared 600mA until it detects a USB-C Power Delivery supply that
can carry more, at which point it raises the budget to 1.6A. A kitchen appliance
with a Stream Deck, a barcode scanner, and a label printer hanging off it lives
inside that budget, so the supply you choose decides whether the peripherals work
reliably or drop out at random.

Guidance for a full build (Pi 5, DSI display, Stream Deck, USB scanner, and a
printer): use the official 27W supply and nothing less. A DSI display draws its
power from the Pi, so it comes out of the same budget. On a Pi 4, a genuine 5V/3A
supply covers a display plus a Stream Deck, but a printer that charges over USB
is worth putting on its own supply.

Undervoltage does not announce itself politely. It shows up as a Stream Deck that
disconnects at random, SD-card corruption, or throttling. See
[Power and cabling](../hardware.md#power-and-cabling) for how to check a
suspect supply.

### PoE HAT

One cable for power and network is the tidiest way to put a Pi in a kitchen, and
it is a genuinely good choice for a wall-mounted or shelf-mounted appliance. It
also has the most compatibility consequences of anything on this page, so it has
[its own section below](#poe-hats).

Buy a **PoE+ (802.3at)** HAT and switch port, not plain PoE (802.3af). Plain PoE
budgets around 13W, which is under what a Pi 5 with a display and USB peripherals
wants. PoE+ carries roughly 25W.

### The GPIO 5V pins (not recommended as your primary supply)

You can feed 5V into physical pins 2 or 4 and the Pi will run. Pantry Raider does
not recommend it as the way you power an appliance, for one blunt reason: that
path skips the board's input protection. The USB-C input has fusing and
protection circuitry in front of it; the 5V header pins are wired more or less
straight to the board's 5V rail. A wiring slip, a supply that overshoots, or
reversed polarity has nothing standing between it and the Pi. Raspberry Pi's own
HAT design guide requires boards that back-power the Pi this way to bring their
own safety diode and to supply 5V at 2.5A or better, which tells you what the
header expects of anything driving it.

The same guide is blunter about the other rail: never connect a power source to
the 3.3V pins at all.

Feeding 5V in through the header is fine when a HAT designed to do it (a PoE HAT,
for example) is doing it. It is not fine as a way to save the price of a proper
supply.

## The 40-pin header, as Pantry Raider uses it

This is every pin the project claims. Everything not listed is free.

| Physical pin | Signal | What Pantry Raider puts here | Optional? |
| --- | --- | --- | --- |
| 1 | 3.3V | Power for a STEMMA QT / Qwiic device wired by cable, and for the soldered accelerometer | Yes |
| 2 | 5V | LD2410C presence sensor VCC | Yes |
| 3 | GPIO2 (SDA, I2C-1) | The I2C bus: every STEMMA QT / Qwiic device, and the accelerometer | Yes |
| 5 | GPIO3 (SCL, I2C-1) | The I2C bus, same as above | Yes |
| 6 | GND | Ground for the presence sensor and the accelerometer | Yes |
| 9 | GND | Ground for a Qwiic device wired by cable | Yes |
| 11 | GPIO17 | LD2410C presence sensor OUT (wakes the display) | Yes |
| 12 | GPIO18 | I2S bit clock, only if you fit an I2S amplifier | Yes |
| 35 | GPIO19 | I2S word select, only with an I2S amplifier | Yes |
| 40 | GPIO21 | I2S data, only with an I2S amplifier | Yes |
| 27, 28 | ID_SD, ID_SC | Nothing of ours. Reserved by Raspberry Pi for HAT identification | Reserved |

Notes that matter when you start stacking things:

- **The I2C pins are shared, not owned.** I2C is a bus. Every device on pins 3
  and 5 coexists as long as each has a different address. Two devices at the same
  address is the only thing that breaks, and Pantry Raider's Settings screen shows
  you what it found on the bus, so a collision is visible rather than mysterious.
- **Finding pin 1, which you will need for every check on this page.** The header
  has two rows of twenty. Pin 1 is the end nearest the microSD card slot, which on
  a Pi 4 is also the end furthest from the USB and Ethernet ports, next to the USB-C
  power connector. From there the odd pins (1, 3, 5, and on up to 39) run along the
  row toward the middle of the board, and the even pins (2, 4, 6, up to 40) run
  along the row on the outside edge. So pins 1 to 6 are the block of six at that
  corner, and counting to pin 11 means counting six positions down the odd row.
- **3.3V and 5V and ground are duplicated.** Pins 1 and 17 are both 3.3V; pins 2
  and 4 are both 5V; pins 6, 9, 14, 20, 25, 30, 34, and 39 are all ground. When
  two accessories want "the" ground pin, move one to another ground. That is not a
  conflict, just a wire.
- **Pins 27 and 28 are for HATs to identify themselves.** Raspberry Pi reserves
  them for an ID EEPROM that a HAT carries so the Pi knows what is attached and
  loads the right configuration at boot. Do not wire anything to them. They are
  also why some boards stack politely and others do not: a HAT that declares
  itself properly can be combined with other boards predictably, and a board with
  no EEPROM (which includes Raspberry Pi's own PoE and PoE+ HATs) is a
  suck-it-and-see proposition by Raspberry Pi's own admission.
- **A GPIO pin is not a power supply.** Raspberry Pi's documentation puts the safe
  figure at 16mA per pin, but also notes the 3.3V supply was designed around
  roughly 3mA per GPIO pin: load every pin to 16mA and the rail collapses. This is
  the reason a buzzer bigger than a small piezo wants a transistor rather than a
  direct connection.

Free after the above, and genuinely free (not shared with anything the project
uses): GPIO4, GPIO5, GPIO6, GPIO12, GPIO13, GPIO16, GPIO20, GPIO22 through
GPIO27, and the SPI block (GPIO7 through GPIO11). Two cautions on that list. If
you run an HDMI panel with SPI resistive touch, the touch controller takes the SPI
block (physical pins 19, 21, 23, 24, and 26). And GPIO14 is where the official Pi
4 case fan expects its control wire.

## STEMMA QT and Qwiic

Adafruit's STEMMA QT and SparkFun's Qwiic are the same thing electrically: a 3.3V
I2C bus on a small 4-pin JST-SH connector. One cable standard, no soldering, and
hundreds of sensors and controls that chain together. A Pi speaks it natively, so
the only question is how you get from the header to the first connector.

The connector carries four wires, always in this order:

| Position | Signal | Wire color |
| --- | --- | --- |
| 1 | GND | Black |
| 2 | 3.3V | Red |
| 3 | SDA (data) | Blue |
| 4 | SCL (clock) | Yellow |

### The 3.3V rule

Qwiic and STEMMA QT devices are 3.3V parts. Do not power them from 5V.

There is a wrinkle worth knowing if you mix brands. SparkFun requires every Qwiic
board to be a 3.3V design. Adafruit's STEMMA QT specification is looser: it allows
a host to put anywhere from 3V to 5V on the power wire, and expects the device to
regulate for itself. So the dangerous direction is a STEMMA QT host feeding 5V to
a SparkFun Qwiic device that assumes 3.3V and has no regulator of its own. Going
the other way is safe. On a Raspberry Pi the practical rule is simply: the red wire
comes from 3.3V, and if a board offers you a choice, choose 3.3V.

There is a related trap in the names. Plain **STEMMA** (without the QT) is a
different, larger connector, it can carry analog signals rather than I2C, and its
4-pin I2C variant uses a different color order. If the connector is 1mm and tiny,
it is STEMMA QT and it matches Qwiic. If it is chunky, it is not.

### Getting from the header to the first connector

Three routes, in the order most builds should consider them.

**A plain cable to the header.** A STEMMA QT / Qwiic cable with female jumper
sockets on the far end (Adafruit 4397 is the common one) lands on four header
pins: red to pin 1 (3.3V), blue to pin 3 (SDA), yellow to pin 5 (SCL), and black to
pin 9 (GND). Use pin 9 for ground rather than pin 6, even though both are ground:
pin 9 keeps the cable away from pins 2 and 4, which are 5V, and putting the red
wire on a 5V pin by mistake is exactly the failure this section is warning about.
This route costs a dollar, blocks nothing, and leaves the rest of the header free.

**A Qwiic SHIM.** A thin board that friction-fits onto pins 1 through 6 and gives
you one Qwiic port, with no soldering. Its trick is that it is thin enough that a
HAT still seats fully on the header on top of it, so a PoE HAT and a Qwiic port can
coexist. It draws from 5V and regulates its own 3.3V, so it does not lean on the
Pi's small 3.3V rail. The catch is mechanical: it occupies pins 1 through 6, which
is where a DSI display's power jumpers and the presence sensor's power and ground
also want to be. See the matrix below.

**A Qwiic HAT or pHAT.** A board that gives you four Qwiic ports at once. It is the
tidiest option if the Pi has nothing else on its header, and the least flexible if
it does. Note that the SparkFun Qwiic HAT that Adafruit resold as 4688 is no longer
stocked, and that the Qwiic pHAT v2.0 puts its own button on GPIO17, which is the
pin our presence sensor uses. If you fit that board and the presence sensor, one of
them has to move.

### Chaining, addresses, and cable length

Every Qwiic device has two connectors so you can daisy-chain them, and there is no
device-count limit worth quoting. The real limits are two:

- **Addresses.** Each device on the bus needs its own I2C address. Many parts let
  you change theirs with a jumper, and the ones this project supports are chosen
  with that in mind. Pantry Raider's Settings screen lists what it sees on the bus
  with each device's address, so a collision shows up as a device that will not
  appear rather than a puzzle. Where two identical sensors have to coexist, an I2C
  multiplexer gives each one its own branch.
- **Total cable length.** I2C was not designed to go far. A metre of chained cable
  is comfortable, and past that the bus slows down and eventually stops working, as
  cable capacitance blunts the signal edges. Keep the chain inside the enclosure,
  and if a sensor genuinely has to live a few metres away, that is what an active
  extender is for. In practice, a sensor in a fridge across the kitchen is a job for
  a Bluetooth sensor, not a long I2C cable.

The Pi provides the bus pull-up resistors itself, so a plain cable and a bare
breakout work with nothing else added.

## mmWave presence

The LD2410C presence sensor wakes the display as you walk up. Three wires:

| LD2410C pin | Raspberry Pi physical pin |
| --- | --- |
| VCC | Pin 2 (5V) |
| GND | Pin 6 |
| OUT | Pin 11 (GPIO17) |

The full guide, including mounting and turning it on, is the
[mmWave presence sensor](presence-sensor.md) page. The compatibility facts to
carry into a build:

- **GPIO17 (pin 11) is claimed** whenever this sensor is fitted. Nothing else in
  the project uses it, but some third-party boards do, most notably the SparkFun
  Qwiic pHAT v2.0's onboard button.
- **The sensor is 5V-powered with a 3.3V-safe output**, so OUT connects straight to
  GPIO17 with nothing in between.
- **Its power and ground are on pins 2 and 6**, which is the crowded corner of the
  header. Both are duplicated elsewhere (pin 4 is also 5V, and there are seven more
  grounds), so if something else wants that corner, move these wires rather than
  giving up the sensor.
- There is a plug-in alternative. A distance or proximity sensor on the QT bus can
  do the same job with no GPIO pins at all, which is the tidier answer when the
  header is busy.

## Sound

Pantry Raider makes noise for timers, alarms, and alerts. The chime is generated by
the kiosk browser, so the app does not need a particular sound card or driver: it
needs the Pi to have a working default audio output. Anything the operating system
can play through, Pantry Raider can chime through. (If you would rather it stayed
silent, Quiet mode under Settings, Screen turns the chime off per device and leaves
the on-screen alert.)

The Pi's own analog audio is not the answer. The Pi 5 has no headphone jack at all,
and the Pi 4's jack is derived from PWM rather than a real DAC: Raspberry Pi's own
documentation describes its low-quality mode, its 11-bit default depth, and the
audible hiss its dithering can produce at low volume. It is adequate for a beep and
disappointing for anything else.

The real options, best first for an appliance:

**A USB speaker or USB sound card. This is the recommended default.** It uses zero
GPIO pins, so it conflicts with nothing on this page: no HAT, no header pin, no
bus. Raspberry Pi OS supports USB audio out of the box and routes to it
automatically when nothing else is connected, and you can pick the output
explicitly if several exist. A cheap powered USB speaker is louder than an I2S amp
plus a bare speaker, costs about the same, and can be moved without a screwdriver.
Its only cost is a USB port and some of the USB current budget, which is another
reason for the 27W supply above.

**HDMI audio, when the screen has speakers.** Free if you already have it, and on
by default. Two caveats: audio goes to the first HDMI port unless you tell it
otherwise, and most small kitchen panels have no speakers. DSI displays never do,
including Raspberry Pi's own Touch Display, because the DSI ribbon carries video
and touch only.

**An I2S amplifier, when you want sound built into the enclosure.** This is a real
DAC and a class-D amplifier on the Pi's digital audio pins, and it sounds
genuinely good. The common part is the Adafruit MAX98357A breakout: roughly 3W into
a 4 ohm speaker, mono, about six dollars. It claims three pins plus power and
ground:

| Amplifier pin | Raspberry Pi pin | Signal |
| --- | --- | --- |
| BCLK | Pin 12 (GPIO18) | Bit clock |
| LRCLK | Pin 35 (GPIO19) | Word select |
| DIN | Pin 40 (GPIO21) | Data, from the Pi's PCM_DOUT |
| Vin | 5V | |
| GND | Ground | |

Two things to be clear about, because the names invite confusion. **I2S is not
I2C.** They are different buses on different pins, and an I2S amplifier and your
whole STEMMA QT chain coexist happily: the amplifier claims GPIO18, 19, and 21,
while QT uses GPIO2 and 3. They never touch. And the amplifier's DIN connects to
the Pi's data *output*, which is why the pin names look inverted.

There is no STEMMA QT version of this amplifier, and there cannot usefully be one:
QT is an I2C connector, and this is an I2S device. Adafruit does sell a "STEMMA
Speaker", but it is a different product with a different, larger connector that
takes an analog signal. It is not a QT device and it is not this.

If you want stereo in an enclosure, Adafruit's I2S 3W Stereo Speaker Bonnet uses
the same three pins and leaves the rest of the header alone, by Adafruit's own
description. It ships with a plain female header, so treat it as occupying the
header unless you can see otherwise on the board in front of you. (The Pimoroni
Speaker pHAT that used to fill this slot is discontinued. Do not design it in.)

**A piezo buzzer on a spare GPIO,** if all you want is a cheap beep. After I2C
(pins 3 and 5), the presence sensor (pin 11), and I2S if fitted (pins 12, 35, 40),
GPIO22, GPIO23, GPIO24, and GPIO25 (physical pins 15, 16, 18, and 22) are free on
every build in this document and are the natural home for one. A small piezo
drawing a couple of milliamps can sit directly on a pin; anything louder, and
certainly a magnetic buzzer, wants a transistor to switch it rather than pulling
current through the GPIO. An active buzzer needs only on and off; a passive one
needs a PWM tone.

One genuine conflict to know about: GPIO18 is both the I2S bit clock and the Pi's
main PWM pin. Addressable LED strips driven on GPIO18 use that PWM hardware and
cannot coexist with an I2S amplifier, and they also fight the onboard analog audio.
Driving the LEDs over SPI instead resolves it, and is the standard fix.

## PoE HATs

This is where compatibility gets real, and where the honest answer is often "it
depends on the model".

A PoE HAT takes 48V from the Ethernet cable and turns it into 5V for the Pi. It
connects to two things: the 4-pin PoE header (separate from the 40-pin header) and,
usually, the 40-pin header itself for its 5V and ground. That second connection is
the crux of this whole page, because it decides how much of the header you have
left. If a HAT covers the header and gives nothing back, everything else on this
page that needs a header pin is off the table.

Two facts before the models:

- **A Pi 5 needs a Pi 5 PoE HAT.** The 4-pin PoE header moved to the other edge of
  the board on the Pi 5. This is mechanical, not electrical: a Pi 4 PoE HAT will not
  reach. Raspberry Pi's own PoE+ HAT lists compatibility as Pi 3B+ and Pi 4 only,
  and at the time of writing there is no shipping official Pi 5 PoE+ HAT, though one
  has been designed publicly. For a Pi 5, that means a third-party board today.
- **Raspberry Pi's own PoE HATs carry no ID EEPROM**, which Raspberry Pi
  acknowledges puts them outside the HAT+ standard, and means combining them with
  other boards is not guaranteed.

### Three shapes, not two

Most buying advice splits PoE HATs two ways, passthrough or not, because most PoE
HATs put a 40-pin connector on the board and the only question left is whether the
pins reappear on top. There is a third shape, and it is the one to look for.

- **Covers the header.** The connector spans all 40 pins and stops there. Every
  header route on this page is gone.
- **Covers the header and passes it through.** The connector spans all 40 pins and
  presents them again on a second row on top. Everything works, subject to how much
  room is left above the board.
- **Lands on a corner and breaks the rest back out.** The connector spans only the
  handful of pins the board actually needs, usually the block at the pin 1 corner,
  and the other thirty-odd pins are not part of the connector at all. The board then
  brings its own small header for the signals it does consume, typically 5V, 3.3V,
  and ground for a fan.

**Call the third shape what it is: the friendly one.** The reason is worth stating
plainly, because it is the single most useful thing on this page when you are
choosing a PoE HAT. A PoE HAT needs 5V and ground, and those live at the pin 1
corner. Everything else this project wants is somewhere else entirely: GPIO17 on pin
11 for the presence sensor, and pins 12, 35, and 40 for an I2S amplifier. A board
that only lands on the corner cannot reach them, so it cannot break them. Instead of
losing the header, you lose a named block of six pins, and the board hands most of
that block back on its own connector.

It has a catch, and it is the mirror image of the passthrough problem. The pins the
HAT does not claim are electrically free, but the board is still a board, sitting
above the Pi on standoffs. Whether you can physically land a jumper on pin 11 with
the HAT fitted depends on the board's footprint and the clearance underneath it, and
that is not something PoE vendors publish. Wire the pins you need before the HAT
goes on, route the wires out sideways, and treat the fit as something to check with
the parts in your hands.

The two shapes trade off cleanly. A passthrough board gives you every pin and asks
you to worry about height. A corner-landing board gives you most pins with no height
problem at all, and asks you to worry about the six it took.

### By family

**Raspberry Pi PoE+ HAT (Pi 3B+ and Pi 4).** Its fan is controlled over I2C, but on
**i2c-0**, the bus on the ID EEPROM pins (27 and 28), at address 0x51. That is the
good news for this project: **it does not touch I2C-1 on pins 3 and 5, so it leaves
the whole STEMMA QT bus free and cannot collide with a Qwiic device's address.**
Whether it exposes a stacking header is not something Raspberry Pi states either
way in its documentation; its product brief says only that it connects to the two
headers. Check the board in front of you.

**Waveshare PoE HAT (B), (C), and (F).** These are the ones that answer the
question in writing. All three state a "standard Raspberry Pi 40PIN GPIO stackable
header, allows connecting other HATs". (F) is the Pi 5 model; (B) and (C) are Pi
3B+ and Pi 4. The catch is on (B): it carries an OLED display and a fan controller
that both live on **I2C-1**, the same bus as your Qwiic devices, at addresses 0x3C
(the OLED) and 0x20 (the fan expander). That is coexistence, not conflict, as long
as no Qwiic device you add is at 0x3C or 0x20. (C) and (F) have no OLED and no I2C
presence. Waveshare's (G), (H), and (J) are Pi 5 boards that do not use the word
"stackable", so treat them as unknown until you see the board.

**LoveRPi PoE HAT (Pi 3B+ and Pi 4).** This is the corner-landing shape, and it is
the only PoE family on this page that tells you its pin count in its own
installation instructions rather than leaving you to guess. Their step 2 tells you to
"align the first 6 pins of the 40-pin header" with the HAT's 6-pin connector, and to
align the separate 4-pin PoE header with its own connector. Six pins, named, from the
vendor.

That single sentence settles most of the compatibility question:

- **The six pins are 1 to 6.** So the HAT takes 3.3V (pin 1), both 5V pins (2 and
  4), a ground (6), and, importantly, SDA and SCL on pins 3 and 5.
- **Pins 7 to 40 are not part of the connector.** **GPIO17 on pin 11 for the
  presence sensor, and pins 12, 35, and 40 for an I2S amplifier, are pins this HAT
  does not claim.** That is a real verdict and it is the good news here.
- **It brings its own header back out.** The fan plugs into a header on the HAT, and
  LoveRPi's own instructions for the model without the fan controller tell you to
  attach the fan's red wire to "5V (performance) or 3.3V (quiet) pin on PoE HAT", so
  that header carries 5V, 3.3V, and ground. Their Professional model is sold as
  having "4-pin PWM fan header and 2-pin 5V header for displays and other
  accessories". That is where the presence sensor's 5V comes from once the HAT is
  sitting on pins 2 and 4.

**The fan goes on your I2C bus, not a private one.** This is the opposite of the
official Raspberry Pi PoE+ HAT above, and it is worth knowing before you plan
addresses. The fan-controller models carry an EMC2301, and LoveRPi's spec puts its
"I2C bus address on 40-pin header pin 3 and 5", which is **I2C-1, the same bus as
your whole STEMMA QT chain**. Their own fan daemon ships configured for bus 1 at
address **0x2f**, and a customer's `i2cdetect -y 1` on their forum shows the driver
holding 0x2f with the HAT fitted. So this is the Waveshare (B) situation rather than
the official HAT's: coexistence, not conflict, as long as no QT device you add sits
at 0x2f, which few do. In exchange you get genuine temperature-driven fan control
from `dtoverlay=i2c-fan,emc2301`, which is more than any UCTRONICS page will promise.

**Buy the isolated model, and treat that as a rule rather than an upsell.** LoveRPi
is unusually direct that their non-isolated variant "must not be used with grounded
HDMI or USB devices" and will create a ground loop, and their own listings say to
always use the Professional model with the isolation transformer in any
non-controlled installation. Every build in this document has grounded USB hanging
off it (a Stream Deck, a scanner, a speaker, a printer) and often an HDMI panel, so
the non-isolated model is simply the wrong part for this project. Take the 802.3at
variant too: LoveRPi rates the 802.3af variants at 10.1W of DC output and the 802.3at
at 20.4W, and 10.1W does not cover a Pi 4 with a display and peripherals. Their
non-fan model's page rates its output at "5V +-5% up to 2.5A", which is exactly the
floor Raspberry Pi's HAT design guide sets for a board that back-powers the Pi
through the header, so the board is doing that job to spec.

**What LoveRPi does not tell you is the last thing you need: does the 6-pin block
pass through?** Six is a count of pins used, not a promise that those six are
reachable once the HAT is on. It matters for exactly one accessory, and it is the
one this project cares most about: a STEMMA QT cable needs 3.3V, SDA, and SCL, which
are pins 1, 3, and 5, all under that block, and the header has no second SDA or SCL
to move to the way it has spare grounds. The presence sensor and the I2S amp do not
care, because their pins are outside the block and their power can come from the
HAT's own 5V header. The evidence is genuinely mixed, so here it is rather than a
guess:

- LoveRPi's live product pages and installation instructions never use the words
  passthrough, stackable, or extended header, which is the same silence the
  UCTRONICS section below is about.
- On their legacy forum, a customer asking where to plug a fan in "given that the
  usual pins are covered by the block of six socket on the GPIO" got a reply from
  LoveRPi saying "it is a direct passthrough as the normal pins except there were two
  ground pins at the very top to make it easy to connect". That is the vendor saying
  the block passes through, and it is the only such statement anywhere. It is a
  2021 forum reply about fan wiring, not documentation, and the customer in that same
  thread reported that the pinout is not printed on his board.

So the verdict is **check the board**, and it is a ten-second check rather than a
research project. Find pin 1 using the landmark above, then look at the block sitting
on pins 1 to 6. If the top face of that block has pins or sockets standing proud of
it, the six pins are reachable and a QT cable plugs straight in, which makes this the
one PoE HAT on this page that gives you PoE, STEMMA QT, and presence wake at once. If
its top face is closed, the QT bus is physically unreachable and you need the SHIM
route or a different HAT, no matter what the addresses say.

**There is no Pi 5 model.** The line covers Pi 3B+ and Pi 4 only, and per the PoE
header note above that is mechanical and not something a Pi 5 can be talked into. If
you are building on a Pi 5, this family is not an option today.

**UCTRONICS PoE HATs.** These come up more than any other, and the honest answer
is: **it depends on the model, UCTRONICS does not publish it, and you have to check
the board.** Across their whole PoE range, no product page, manual, or wiki
uses the words "passthrough", "stacking header", or "extended header". The
strongest statements available point the wrong way: their Pi 5 PoE HAT describes
itself as "occupying only 6 pins of the 40-pin GPIO", which is a count of pins used
and not a promise that the rest are reachable, and their rack-mount instructions say
to plug the HAT onto the GPIO pins. Both describe a board that sits on the header
rather than one that re-presents it on top.

**One line in their catalogue will mislead you if you let it.** The Mini PoE HAT
(U6241) says it "does not occupy the GPIO pins of Raspberry Pi, saving more space
for your other hardware". Read in context, that sits under a heading about the
board's small size and is a claim about physical footprint: the board is small and
stays out of the way. It is not a statement that the 40-pin header is electrically
passed through or left free for you to plug into. Do not buy this family on the
strength of that sentence.

So: is a STEMMA QT breakout compatible with a UCTRONICS PoE HAT?

- **Electrically, yes, and comfortably.** Nothing in a UCTRONICS PoE HAT conflicts
  with a QT device's address unless the HAT has an OLED. The models with an OLED
  (their rack-mount and rerouting boards) put it at **0x3C on I2C-1**, the same bus
  your QT chain uses. Sharing a bus is fine, and different addresses coexist all day.
  The one real collision: 0x3C is also the usual address for small SSD1306 and SH1106
  OLED breakouts sold with QT connectors, so a QT OLED plus a UCTRONICS OLED model is
  a genuine clash. Most other QT parts are nowhere near that address.
- **Mechanically, that is the question, and it turns on one thing: can you still
  reach pins 1, 3, 5, and 9 with the HAT fitted?** If the HAT presents a second row
  of pins on top, yes, and everything works. If it covers the header, no, and no
  amount of address planning helps, because the bus has to be physically reachable
  before its addresses matter.
- **How to tell, in ten seconds.** Look at the top face of the HAT. A passthrough
  board presents a tall header with 40 pins standing proud on top, in the same 2x20
  arrangement as the Pi's own, ready for jumpers or another board. A board with no
  pins on its top face has consumed the header, and STEMMA QT needs a different route
  (a SHIM underneath) or a different PoE HAT. If you own the board, that check is
  definitive. If you are still shopping, look for those upward pins in the product
  photos, and if you cannot see them, assume they are not there.
- **Fan control on these boards is undocumented too.** Only their Pi 5 NVMe models
  mention a PWM fan, and none of the pages say whether the fan is driven by the Pi or
  simply powered by the HAT. It does not affect the QT question, but do not assume you
  will get software fan control.
- **When searching for these, the PoE boards are the U6xxx part numbers.** The
  B0-prefixed codes that come up are Amazon listing IDs, and UCTRONICS' own B0xxx
  numbers belong to unrelated camera parts.
- **If it covers the header,** you have three ways out: fit a Qwiic SHIM under the
  HAT (thin enough that the HAT still seats, and it gives you a QT port), choose a
  Waveshare (C) or (F) instead, which say "stackable" in writing, or power the Pi
  over USB-C and skip the PoE HAT.

**Stacking clearance, honestly.** Even where a PoE HAT does pass the header
through, no vendor publishes how much vertical room is left above it, and PoE HATs
are tall boards with a transformer on them. Raspberry Pi recommends HAT designers
allow 15 to 16mm of board-to-board spacing, but that is advice to designers, not a
measurement of any specific PoE HAT. If you plan to stack a Qwiic HAT on top of a
PoE HAT, treat it as something to verify with the parts in hand. The cable route
and the SHIM route both sidestep this entirely, which is why they are recommended
first.

## Compatibility matrix

Each cell is what happens when you put the row and the column on the same Pi.

**Works** means fit both, no thought required. **Check** means it depends on a
specific thing, named in the note. **Conflicts** means pick one.

| | PoE HAT (passthrough) | PoE HAT (no passthrough) | PoE HAT (6-pin corner) | Qwiic HAT / pHAT | Qwiic SHIM | QT cable to pins 1/3/5/9 | mmWave on GPIO17 | DSI display | I2S amp | USB speaker | GPIO fan | Stream Deck | Printers |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **PoE HAT (passthrough)** | | | | Check: clearance above the PoE HAT is unpublished | Works: SHIM fits under the HAT | Works | Works | Works | Works | Works | Check: Pi 5 fan header is free; a Pi 4 GPIO fan needs a reachable pin | Works | Works |
| **PoE HAT (no passthrough)** | | | | Conflicts: header covered | Works: SHIM fits under the HAT | Conflicts: header covered | Conflicts: header covered | Check: display power needs pins 2/4/6 | Conflicts: header covered | Works | Check: Pi 5 fan header is free; Pi 4 has no reachable pin | Works | Works |
| **PoE HAT (6-pin corner)** | | | | Conflicts: the pHAT's 40-pin connector covers pins 1 to 6, where the PoE HAT has to seat | Check: both want pins 1 to 6; the SHIM is built for a HAT to seat on top of it, but on a 6-pin block that is unverified | Check: pins 1, 3, and 5 are under the block, and the header has no second SDA/SCL; depends whether the block passes through | Check: pin 11 is not claimed, but verify you can reach it under the board, and take the sensor's 5V from the HAT's own header | Check: pins 2/4/6 are under the block; take display power from the HAT's 5V header | Check: pins 12/35/40 are not claimed; take the amp's 5V and ground from the HAT's header | Works: with the isolated model | Works: the HAT brings its own PWM fan header, at 0x2f on I2C-1 | Works: with the isolated model | Works: with the isolated model |
| **Qwiic HAT / pHAT** | Check: clearance above the PoE HAT is unpublished | Conflicts: header covered | Conflicts: the pHAT's 40-pin connector covers pins 1 to 6, where the PoE HAT has to seat | | Works: same bus, no need for both | Works: same bus | Check: the SparkFun Qwiic pHAT v2.0 puts a button on GPIO17 | Check: pHAT covers the header the display power cable needs | Check: pHAT covers pins 12/35/40 | Works | Check: pHAT covers the header | Works | Works |
| **Qwiic SHIM** | Works | Works | Check: both want pins 1 to 6; the SHIM is built for a HAT to seat on top of it, but on a 6-pin block that is unverified | Works: same bus, no need for both | | Works: same bus | Conflicts: SHIM occupies pins 1 to 6, the sensor needs 2 and 6 | Conflicts: SHIM occupies pins 1 to 6, display power needs 2/4/6 | Works | Works | Works | Works | Works |
| **QT cable to pins 1/3/5/9** | Works | Conflicts: header covered | Check: pins 1, 3, and 5 are under the block, and the header has no second SDA/SCL; depends whether the block passes through | Works: same bus | Works: same bus | | Works | Works: display avoids pins 1/3/5/9 | Works: I2S and I2C are different pins | Works | Works | Works | Works |
| **mmWave on GPIO17** | Works | Conflicts: header covered | Check: pin 11 is not claimed, but verify you can reach it under the board, and take the sensor's 5V from the HAT's own header | Check: the SparkFun Qwiic pHAT v2.0 puts a button on GPIO17 | Conflicts: SHIM occupies pins 1 to 6, the sensor needs 2 and 6 | Works | | Check: both want pins 2 and 6; move the sensor to pin 4 and another ground | Works | Works | Works | Works | Works |
| **DSI display** | Works | Check: display power needs pins 2/4/6 | Check: pins 2/4/6 are under the block; take display power from the HAT's 5V header | Check: pHAT covers the header the display power cable needs | Conflicts: SHIM occupies pins 1 to 6, display power needs 2/4/6 | Works: display avoids pins 1/3/5/9 | Check: both want pins 2 and 6; move the sensor to pin 4 and another ground | | Works | Works: DSI panels have no speakers | Works | Works | Works |
| **I2S amp** | Works | Conflicts: header covered | Check: pins 12/35/40 are not claimed; take the amp's 5V and ground from the HAT's header | Check: pHAT covers pins 12/35/40 | Works | Works: I2S and I2C are different pins | Works | Works | | Works: pick one as the default output | Check: a Pi 4 fan on GPIO18 collides with the I2S clock; use GPIO14 | Works | Works |
| **USB speaker** | Works | Works | Works: with the isolated model | Works | Works | Works | Works | Works | Works: pick one as the default output | | Works | Works: uses a USB port | Works: uses a USB port |
| **GPIO fan** | Check: Pi 5 fan header is free; a Pi 4 GPIO fan needs a reachable pin | Check: Pi 5 fan header is free; Pi 4 has no reachable pin | Works: the HAT brings its own PWM fan header, at 0x2f on I2C-1 | Check: pHAT covers the header | Works | Works | Works | Works | Check: a Pi 4 fan on GPIO18 collides with the I2S clock; use GPIO14 | Works | | Works | Works |
| **Stream Deck** | Works | Works | Works: with the isolated model | Works | Works | Works | Works | Works | Works | Works | Works | | Works: watch the USB current budget |
| **Printers** | Works | Works | Works: with the isolated model | Works | Works | Works | Works | Works | Works | Works | Works | Works: watch the USB current budget | |

Three rules explain most of the "conflicts" and "check" cells above, and all three
are worth internalising before buying anything:

1. **A HAT that covers the 40-pin header ends the conversation** for everything
   that needs a header pin. The Qwiic SHIM is the exception, because it fits
   underneath.
2. **The DSI display and the presence sensor both want the pin 2 / pin 6 corner.**
   Because 5V and ground are duplicated elsewhere on the header, this is a
   move-a-wire problem, not a pick-one problem. The Qwiic SHIM is the case where it
   really is pick-one, because the SHIM wants all six of those pins physically.
3. **Everything that fights over pins 1 to 6 is fighting over the same corner.** The
   Qwiic SHIM, a DSI display's power jumpers, the presence sensor's power and
   ground, and a corner-landing PoE HAT all want that block. This is why the
   corner-landing PoE HAT's whole row is "check" rather than "works": it does not
   cover the header, it moves the argument to the corner. The pin that decides most
   of it is SDA and SCL on 3 and 5, because unlike 5V and ground they exist in
   exactly one place on the header and cannot be moved.

On the I2C bus specifically, coexistence is the normal case, not the exception.
The addresses in play on a busy build are the PoE HAT's OLED at 0x3C and its fan
expander at 0x20 (Waveshare B and the UCTRONICS OLED models), a LoveRPi fan
controller at 0x2f, and whatever your QT devices use. The official Raspberry Pi PoE+
HAT is the tidiest of the lot: its fan sits on i2c-0, a different bus entirely, and
never touches yours. And a DSI display's touch controller does not sit on I2C-1
either. It rides its own bus up the display ribbon, so it cannot collide with a
Qwiic device no matter how many you chain.

## What we recommend

Two builds that are known to work as a whole, rather than as a list of parts that
individually work.

**PoE-powered kitchen appliance, with presence wake and a NeoKey.**

- Raspberry Pi 5 (4GB or 8GB).
- Waveshare PoE HAT (F), a Pi 5 board that states a stackable header, fed from a
  PoE+ (802.3at) switch or injector. No OLED, so nothing of its own on your I2C bus.
- A STEMMA QT cable from header pins 1, 3, 5, and 9 to the first QT device, through
  the HAT's stackable header.
- An Adafruit NeoKey 1x4 on the QT chain as the physical scan-mode selector.
- The LD2410C presence sensor on pins 2, 6, and 11 for wake-on-approach.
- A DSI touch panel, powered from the header. Move the presence sensor's 5V to pin
  4 and its ground to pin 14 so the display's power cable can have the pin 2 and
  pin 6 corner.
- A small powered USB speaker for timers and alarms.
- Cooling: the Pi 5's own 4-pin fan header is free in this build, so use an Active
  Cooler or a case fan there rather than anything on the 40-pin header.

**On choosing the PoE HAT for that build.** The Waveshare (F) is named above because
it is a Pi 5 board that says "stackable" in writing, which is the best commitment
anyone makes on a Pi 5 today. If you are shopping rather than copying the list,
prefer the corner-landing shape described earlier: a board that lands on a block of
six pins and breaks 5V, 3.3V, and ground back out on its own header is the tidiest
way to have PoE and STEMMA QT and presence wake on one Pi, because it cannot reach
GPIO17 or the I2S pins to break them. Ask the vendor two questions before you buy:
how many header pins does the connector span, and does that block pass through?
Almost nobody answers either in their documentation, which is why "check the board"
appears as often as it does on this page.

**The same build on a Pi 4, if PoE matters more than the Pi 5.** LoveRPi's isolated
802.3at PoE HAT is the corner-landing shape and the only one that publishes its pin
count (the first six pins), so a Pi 4 keeps pin 11 for the presence sensor and the
I2S pins for an amplifier, with the sensor's 5V coming off the HAT's own header. Take
the isolated variant, not the compact non-isolated one, because this build has
grounded USB peripherals on it. Settle the passthrough question on the board before
you count on STEMMA QT: if the six-pin block has no pins on its top face, add a Qwiic
SHIM underneath for the QT port. There is no Pi 5 version of this HAT, so this is a
Pi 4 build or nothing.

**USB-C powered, the simplest thing that works.**

- Raspberry Pi 5 with the official 27W USB-C supply, which is also what raises the
  USB budget to 1.6A for the peripherals below.
- A Qwiic SHIM on pins 1 to 6 for a solder-free QT port, or the plain cable to pins
  1, 3, 5, and 9 if you also want the presence sensor or a DSI display (the SHIM
  wants that whole corner to itself).
- NeoKey 1x4 and any QT sensors on the chain.
- The LD2410C on pins 2, 6, and 11, if you are not using the SHIM.
- A DSI touch panel or an HDMI panel.
- A USB speaker, a Stream Deck, and a USB barcode scanner. All USB, all conflict
  with nothing.

Both builds leave GPIO22 through GPIO25 free if you want a buzzer, and both leave
the I2C bus with room for temperature, light, and distance sensors as you add them.

## Custom boards are coming

Consolidating this wiring onto a purpose-built board, so a build is one connector
rather than a handful of jumper wires, is planned for a later phase of the project.
This page describes what to do with off-the-shelf parts today.
