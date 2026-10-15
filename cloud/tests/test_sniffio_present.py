"""httpcore asks sniffio which async library is running on every pooled
request. Without sniffio installed the import fails each time, and a failed
import is not cached, so every AI call searched the import path on the event
loop. The image installs it from the lock; this keeps the pin from going."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

CLOUD = Path(__file__).resolve().parents[1]


def test_sniffio_is_pinned_and_locked():
    reqs = (CLOUD / "requirements.txt").read_text()
    lock = (CLOUD / "requirements.lock").read_text()
    assert re.search(r"^sniffio==", reqs, re.M)
    assert re.search(r"^sniffio==", lock, re.M)


def test_sniffio_is_importable_here():
    assert importlib.util.find_spec("sniffio") is not None
