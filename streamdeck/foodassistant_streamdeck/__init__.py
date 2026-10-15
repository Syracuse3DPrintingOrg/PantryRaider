"""FoodAssistant Stream Deck controller.

Drives an Elgato Stream Deck (or Stream Deck Module) as a physical control
surface for a FoodAssistant install. It renders live status on the keys
(items expiring soon, pending scans waiting to commit) and triggers app
actions over the HTTP API, so a countertop appliance can run with no
touchscreen at all, or with the deck alongside one.

The hardware-free pieces (config, layout, actions, rendering) live in their
own modules so they can be unit-tested without a deck attached. Only
``controller`` imports the StreamDeck device library.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
