"""Barcode-scanner setup wizard sequences (FoodAssistant-udpk).

Walks a user through programming a scan-engine module by showing its
configuration codes on screen one at a time. The module reads each code off
the display to change one setting, so the wizard is just an ordered list of
codes with a plain explanation for each.

The codes themselves are the printed images that ship with the Waveshare
"Barcode Scanner Module Setting Manual" (V2.1); they are stored under
static/img/scanner/ and pointed at here. Every code in this file matches the
determined hands-free kiosk setup in docs/hardware/waveshare-barcode-scanner.md,
so this module never invents a barcode payload: it only references the exact
images that setup settled on.

Pure data plus small lookup helpers, so it stays testable without a browser.
"""
from __future__ import annotations

# Where the code images live under the mounted static tree. Kept relative (no
# leading slash) so it composes with the ingress base like the rest of the UI.
_IMG = "static/img/scanner/waveshare"


def _code(name: str) -> str:
    return f"{_IMG}/{name}.png"


# Each step is one code to scan, in order. A step may carry `alternatives`: other
# codes that do the same job a different way, shown as a "you could instead"
# note under the recommended one so a user understands the choice.
_WAVESHARE_HANDS_FREE = [
    {
        "key": "reset",
        "title": "Start from a clean slate (optional)",
        "explain": (
            "Scan this only if the reader was set up before or is acting up. It "
            "returns every setting to the factory default so the codes below "
            "land on a known starting point. On a brand-new reader you can skip "
            "straight to the next step."),
        "image": _code("restore-factory"),
        "optional": True,
    },
    {
        "key": "sensing_mode",
        "title": "Turn on hands-free scanning",
        "explain": (
            "Puts the reader in Sensing Mode: it watches for something entering "
            "its view, scans on its own, then goes back to watching. No button "
            "press, so you can present a product and it reads it. You can still "
            "press the button to scan by hand."),
        "image": _code("sensing-mode"),
        "alternatives": [
            {
                "title": "Continuous Mode",
                "explain": (
                    "Scans on a repeating timer whether or not something is in "
                    "view. Simpler, but it reads more aggressively and picks up "
                    "more stray codes. Single-press the button to pause or "
                    "resume."),
                "image": _code("continuous-mode"),
            },
            {
                "title": "Back to button-press (Manual Mode)",
                "explain": (
                    "Returns the reader to the default, where it only scans "
                    "while you hold the button. Use this if you would rather aim "
                    "and press for each item."),
                "image": _code("manual-mode"),
            },
        ],
    },
    {
        "key": "nonscan_interval",
        "title": "Set the pause between scans to half a second",
        "explain": (
            "After each read the reader waits before it starts watching again. "
            "The factory wait is a full second; half a second feels snappier "
            "when you are adding items one after another."),
        "image": _code("sensing-nonscan-interval-500ms"),
        "alternatives": [
            {
                "title": "Wake faster: high sensitivity",
                "explain": (
                    "Makes the reader quicker to react when something enters its "
                    "view. Handy if hands-free scanning feels sluggish; it can "
                    "cause more stray reads in a busy spot."),
                "image": _code("sensitivity-high"),
            },
            {
                "title": "Settle faster: 100 ms image hold",
                "explain": (
                    "How long the reader steadies the picture after it notices a "
                    "change, before it reads. The factory hold is 400 ms; 100 ms "
                    "is more responsive."),
                "image": _code("image-stabilization-100ms"),
            },
        ],
    },
    {
        "key": "enable_delay",
        "title": "Stop the same item scanning over and over",
        "explain": (
            "Turns on a short cooldown so an item left sitting in front of the "
            "reader is not read again and again. Without it, one product in view "
            "floods the pending list."),
        "image": _code("enable-same-barcode-delay"),
    },
    {
        "key": "delay_3s",
        "title": "Set that cooldown to three seconds",
        "explain": (
            "Three seconds suits adding groceries one at a time: long enough to "
            "swap items, short enough to keep moving. Pantry Raider also bumps "
            "the quantity instead of adding a duplicate row, so a stray repeat "
            "is harmless either way."),
        "image": _code("same-barcode-delay-3000ms"),
        "alternatives": [
            {
                "title": "Longer cooldown: five seconds",
                "explain": (
                    "Use this instead of three seconds if single items are still "
                    "getting counted twice."),
                "image": _code("same-barcode-delay-5000ms"),
            },
        ],
    },
    {
        "key": "save",
        "title": "Save so it survives a power cycle",
        "explain": (
            "Scan this last. It writes everything above as the reader's default, "
            "so unplugging it and plugging it back in keeps your hands-free "
            "setup."),
        "image": _code("save-user-default"),
    },
]


# A short recovery sequence for when a reader stops typing into the page (for
# example after a factory reset left it in serial mode). Two codes put it back
# into keyboard-wedge mode. Offered as its own pick in the model dropdown.
_WAVESHARE_KEYBOARD_FIX = [
    {
        "key": "usb_hid",
        "title": "Set the reader to USB device mode",
        "explain": (
            "The first of two codes that put the reader back to acting as a USB "
            "device your computer understands."),
        "image": _code("usb-hid-device"),
    },
    {
        "key": "hid_kbw",
        "title": "Make it type like a keyboard",
        "explain": (
            "Switches the reader into keyboard-wedge mode, so a scan types the "
            "code straight into whatever field is focused. After this it should "
            "type into the test box in Settings again."),
        "image": _code("hid-kbw"),
    },
]


# A reader that is not a Waveshare scan module needs no on-screen codes: any USB
# or Bluetooth HID scanner already types codes like a keyboard. One explain-only
# step tells the user that and points them at the test box.
_GENERIC = [
    {
        "key": "plug_in",
        "title": "This reader is ready as-is",
        "explain": (
            "A USB or Bluetooth scanner works with no on-screen setup: it types "
            "the code like a keyboard, so it lands wherever you are typing. Plug "
            "it in (or pair it), then use the test box in Settings to confirm a "
            "scan comes through."),
        "image": "",
    },
]


# The reader picker. `recommended` marks the default the dropdown lands on, and
# ties each entry to the scanner_type it best matches (a Waveshare module reads
# as a built-in scan module; a plain wedge scanner is usb). scanner_type is a
# hint only, so this never forces it.
MODELS = [
    {
        "id": "waveshare",
        "label": "Waveshare Barcode Scanner Module (hands-free kiosk)",
        "recommended": True,
        "scanner_type": "camera",
        "blurb": (
            "The small 1D and 2D scan-engine module. These codes make it scan on "
            "sight, no button, which is what you want at a kitchen kiosk."),
        "steps": _WAVESHARE_HANDS_FREE,
    },
    {
        "id": "waveshare_keyboard_fix",
        "label": "Waveshare module: it stopped typing",
        "recommended": False,
        "scanner_type": "camera",
        "blurb": (
            "A two-code fix that puts a Waveshare module back into keyboard mode "
            "if a reset left it silent."),
        "steps": _WAVESHARE_KEYBOARD_FIX,
    },
    {
        "id": "generic",
        "label": "Other USB or Bluetooth scanner",
        "recommended": False,
        "scanner_type": "usb",
        "blurb": (
            "Any ordinary keyboard-wedge scanner. Nothing to program on screen; "
            "it works the moment it is plugged in or paired."),
        "steps": _GENERIC,
    },
]

DEFAULT_MODEL_ID = next(m["id"] for m in MODELS if m.get("recommended"))


def model_ids() -> list[str]:
    return [m["id"] for m in MODELS]


def get_model(model_id: str | None) -> dict:
    """The model matching `model_id`, or the recommended default when the id is
    blank or unknown. Always returns a usable model so a route never 404s on a
    stale link."""
    for m in MODELS:
        if m["id"] == model_id:
            return m
    return get_model(DEFAULT_MODEL_ID)


def default_model_for(scanner_type: str | None) -> str:
    """Pick the wizard the saved scanner_type points at, so opening the wizard
    from Settings lands on the right reader. Falls back to the recommended
    default when the type does not single one out."""
    stype = (scanner_type or "").strip().lower()
    if stype:
        for m in MODELS:
            if m.get("scanner_type") == stype and m.get("recommended"):
                return m["id"]
        for m in MODELS:
            if m.get("scanner_type") == stype:
                return m["id"]
    return DEFAULT_MODEL_ID
