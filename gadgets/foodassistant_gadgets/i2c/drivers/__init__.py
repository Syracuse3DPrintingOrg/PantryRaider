"""One module per device family (FoodAssistant-etsc).

Each driver exposes the same small surface, which is what lets discovery walk
them as a plain dict and what makes them drop-in inhabitants of the
integrations registry later (FoodAssistant-pjtq):

* ``KIND``: the family name the app registry stores.
* ``ADDRESSES``: every address the board can live at.
* ``probe(bus, address)``: a cheap identity check returning KIND or None.
* then either ``poll(bus, address)`` for a sensor, or an event/output
  interface for a control.

Frame decode and value scaling stay pure module-level functions taking bytes,
so the whole decode layer tests with no hardware.
"""
from . import neokey  # noqa: F401

__all__ = ["neokey"]
