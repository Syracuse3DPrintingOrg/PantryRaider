"""Curated hardware picks for building a Pantry Raider display (FoodAssistant-ztly).

These are the physical parts we use and recommend for a Pantry Raider kitchen
build: a Raspberry Pi to run it, a small touchscreen to see it, an Elgato Stream
Deck for one-touch controls, a barcode scanner for fast pantry entry, and a few
accessories that hold it all together. The list ships with the app, so publishing
new picks is just an app update.

The Amazon affiliate tag is NEVER written here. Each pick carries only a search
term (or a bare 10-char ASIN to link straight to a specific product); the tag is
appended at render time from config (AMAZON_ASSOCIATES_TAG) by the shared
``affiliate.amazon_url`` helper, so the links stay correct even if the project's
tag changes and no stale tag is ever baked into the data or a template.

To add a pick, append a dict to OUR_PICKS with:

  - name        (required) the product's display name
  - category    (required) one of CATEGORIES; anything else lands under "Accessories"
  - search      (required) an Amazon search term, or a bare 10-char ASIN for a
                specific product page. No fabricated ASINs: a search term always
                resolves to a live listing even if a specific product changes.
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
    "Accessories",
]

# Short, honest affiliate disclosure shown with the picks. User-forward.
DISCLOSURE = (
    "These are the parts we use and recommend for building a Pantry Raider "
    "display. They are affiliate links, so buying through them supports the "
    "project at no extra cost to you. Thanks for the support."
)

OUR_PICKS = [
    # -- Raspberry Pi ------------------------------------------------------
    {
        "name": "Raspberry Pi 5 (8GB)",
        "category": "Raspberry Pi",
        "search": "raspberry pi 5 8gb",
        "description": "The little computer that runs the whole Pantry Raider display.",
        "note": "8GB of memory keeps the kiosk, camera feeds, and timers smooth.",
        "image": "",
    },
    {
        "name": "Raspberry Pi 4 Model B (4GB)",
        "category": "Raspberry Pi",
        "search": "raspberry pi 4 model b 4gb",
        "description": "A proven, lower-cost board that runs a Pantry Raider kiosk well.",
        "note": "A great pick if a Pi 5 is out of stock or over budget.",
        "image": "",
    },
    # -- Touchscreen -------------------------------------------------------
    {
        "name": "Raspberry Pi Touch Display 2",
        "category": "Touchscreen",
        "search": "raspberry pi touch display 2",
        "description": "Official 7-inch touchscreen that mounts cleanly to the Pi.",
        "note": "Powered from the Pi, so it is one tidy unit on the counter.",
        "image": "",
    },
    {
        "name": "10-inch HDMI touchscreen monitor",
        "category": "Touchscreen",
        "search": "10 inch hdmi touchscreen monitor raspberry pi",
        "description": "A roomier panel when you want the pantry and timers visible from across the kitchen.",
        "note": "Look for one with USB touch so a single cable carries taps back to the Pi.",
        "image": "",
    },
    # -- Stream Deck -------------------------------------------------------
    {
        "name": "Elgato Stream Deck MK.2",
        "category": "Stream Deck",
        "search": "elgato stream deck mk.2",
        "description": "15 LCD keys for one-touch scanning modes, timers, and recipe steps.",
        "note": "The 15-key layout is the sweet spot for the Pantry Raider deck controls.",
        "image": "",
    },
    {
        "name": "Elgato Stream Deck Mini",
        "category": "Stream Deck",
        "search": "elgato stream deck mini",
        "description": "A compact 6-key deck for the essentials when counter space is tight.",
        "note": "Plenty for scan, consume, and a couple of timers.",
        "image": "",
    },
    # -- Barcode Scanner ---------------------------------------------------
    {
        "name": "USB handheld barcode scanner",
        "category": "Barcode Scanner",
        "search": "usb handheld barcode scanner 1d 2d wired",
        "description": "Plug-and-play scanner that types barcodes straight into pantry entry.",
        "note": "A wired keyboard-emulation model needs no drivers and just works.",
        "image": "",
    },
    {
        "name": "Wireless 2.4GHz barcode scanner",
        "category": "Barcode Scanner",
        "search": "wireless barcode scanner 2.4ghz usb",
        "description": "Scan items where they sit in the pantry, then sync when you are back at the display.",
        "note": "Handy when the scanner and the screen are not side by side.",
        "image": "",
    },
    # -- Accessories -------------------------------------------------------
    {
        "name": "Official Raspberry Pi 5 power supply (27W)",
        "category": "Accessories",
        "search": "raspberry pi 5 official power supply 27w usb-c",
        "description": "The right USB-C supply so the Pi runs a display without brownouts.",
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
        "name": "Raspberry Pi case with active cooling",
        "category": "Accessories",
        "search": "raspberry pi 5 case active cooling fan",
        "description": "Keeps the board cool and dust-free next to the stove.",
        "note": "Active cooling keeps a hardworking kiosk from throttling.",
        "image": "",
    },
]
