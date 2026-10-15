"""Text that has to be a plain number: the ASCII digits 0 to 9 and nothing else.

str.isdigit() is True for superscripts ("²"), circled digits ("①") and other
characters with a digit value that int() will not read, so a check written as
``s.isdigit()`` followed by ``int(s)`` raises ValueError on them and the
request answers 500. isdecimal() is no better for a check like this: it passes
Arabic-Indic and full-width digits, which int() does read, so they would be
treated as numbers here when nothing else in the app expects them. A barcode,
an id or a port is only ever the ASCII digits.
"""
from __future__ import annotations

import re

_PLAIN = re.compile(r"[0-9]+")


def is_plain_digits(value) -> bool:
    """True when ``value`` is a non-empty string of the ASCII digits 0-9."""
    return isinstance(value, str) and _PLAIN.fullmatch(value) is not None


def plain_int(value, max_digits: int = 18) -> int | None:
    """``int(value)`` when it is plain ASCII digits no longer than
    ``max_digits``, else None. The length cap keeps a header or a pasted value
    with thousands of digits away from int()'s digit limit."""
    if not is_plain_digits(value) or len(value) > max_digits:
        return None
    return int(value)
