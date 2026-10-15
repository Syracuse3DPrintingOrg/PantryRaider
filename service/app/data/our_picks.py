"""Curated hardware picks for building a Pantry Raider display (FoodAssistant-ztly).

These are the physical parts we use and recommend for a Pantry Raider kitchen
build: a Raspberry Pi to run it, a touchscreen to see it, an Elgato Stream Deck
for one-touch controls, a barcode scanner for fast pantry entry, a Bluetooth
label printer for shelf labels, and a few accessories that hold it all
together. This is the same build we run ourselves. The list ships with the
app, so publishing new picks is just an app update.

No store link or affiliate tag is ever written here. On the Shop page every
pick links to the project's picks page (https://pantryraider.app/gear) at an
anchor made from its name by ``affiliate.gear_anchor``, and the store links
with their disclosure live on that page. Renaming a pick changes its anchor,
so keep the picks page in step.

To add a pick, append a dict to OUR_PICKS with:

  - name        (required) the product's display name
  - category    (required) one of CATEGORIES; anything else lands under "Accessories"
  - search      (required) the search the picks page uses for the product
  - description (required) one honest, user-forward line about the part
  - note        (optional) "why we like it", a short reason
  - image       (optional) an image URL; leave "" for a text-only card

Keep it honest: recommend parts we would actually put in a build.
"""
from __future__ import annotations

# Category display order for the Our Picks section. A pick's ``category`` should
# match one of these exactly; an unknown category is grouped under "Accessories"
# so a typo never drops a pick.
CATEGORIES = [
    "Raspberry Pi",
    "Touchscreen",
    "Stream Deck",
    "Barcode Scanner",
    "Label Printer",
    "Accessories",
]

# Short note shown with the picks. User-forward.
DISCLOSURE = (
    "These are the parts we use and recommend for building a Pantry Raider "
    "display. Each one opens on pantryraider.app."
)

OUR_PICKS = [
    # -- Raspberry Pi ------------------------------------------------------
    {
        "name": "Raspberry Pi 4 Model B (4GB)",
        "category": "Raspberry Pi",
        "search": "raspberry pi 4 model b 4gb",
        "description": "The little computer that runs the whole Pantry Raider display.",
        "note": "This is the board in our own build; 4GB is plenty for the kiosk, camera feeds, and timers.",
        "image": "",
    },
    # -- Touchscreen -------------------------------------------------------
    {
        "name": "Hoysund 7-inch touchscreen display",
        "category": "Touchscreen",
        "search": "hoysund 7 inch touchscreen display raspberry pi",
        "description": "A 7-inch capacitive touch panel that mounts cleanly next to the Pi.",
        "note": "This is the display in our own build; counter-friendly size for pantry and timer taps.",
        "image": "",
    },
    # -- Stream Deck -------------------------------------------------------
    {
        "name": "Elgato Stream Deck MK.2 (15-key)",
        "category": "Stream Deck",
        "search": "elgato stream deck mk.2",
        "description": "15 LCD keys for one-touch scanning modes, timers, and recipe steps.",
        "note": "This is the deck in our own build; the 15-key layout is the sweet spot for the Pantry Raider controls.",
        "image": "",
    },
    # -- Barcode Scanner ---------------------------------------------------
    {
        "name": "Waveshare Barcode Scanner Module",
        "category": "Barcode Scanner",
        "search": "waveshare barcode scanner module",
        "description": "A hands-free 1D/2D scan-engine module that reads barcodes without a trigger button.",
        "note": "This is the scanner in our own build; set it up in scan mode with the app's guided setup codes.",
        "image": "",
    },
    # -- Label Printer -------------------------------------------------------
    {
        "name": "SUPVAN T50M Pro label printer",
        "category": "Label Printer",
        "search": "supvan t50m pro label printer",
        "description": "A pocket-size Bluetooth thermal printer for shelf and container labels.",
        "note": "This is the printer in our own build; pair it once and it shows up as a normal printer in the app.",
        "image": "",
    },
    # -- Accessories -------------------------------------------------------
    {
        "name": "Official Raspberry Pi USB-C power supply",
        "category": "Accessories",
        "search": "official raspberry pi usb-c power supply",
        "description": "The right power supply so the Pi runs a display without brownouts.",
        "note": "An underpowered supply is the most common cause of flaky kiosks.",
        "image": "",
    },
    {
        "name": "High-endurance microSD card (32GB+)",
        "category": "Accessories",
        "search": "high endurance microsd card 32gb",
        "description": "The storage the Pi boots from; high-endurance cards last far longer.",
        "note": "An always-on kiosk writes constantly, so endurance matters here.",
        "image": "",
    },
    {
        "name": "Raspberry Pi 4 case with active cooling",
        "category": "Accessories",
        "search": "raspberry pi 4 case active cooling fan",
        "description": "Keeps the board cool and dust-free next to the stove.",
        "note": "Active cooling keeps a hardworking kiosk from throttling.",
        "image": "",
    },
]
