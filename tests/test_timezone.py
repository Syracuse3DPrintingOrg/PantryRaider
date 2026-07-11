"""Timezone-aware timestamp formatting (FoodAssistant-amp0/-lq01).

format_local stores UTC epochs and renders them in a chosen IANA zone, so the
"last checked" line and other timestamps read correctly wherever the device is.
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import format_local, resolve_timezone  # noqa: E402

# 2025-07-01 16:00:00 UTC (noon EDT).
_EPOCH = 1751385600


def test_format_local_renders_in_named_zone():
    assert format_local(_EPOCH, "America/New_York") == "2025-07-01 12:00 EDT"
    assert format_local(_EPOCH, "UTC") == "2025-07-01 16:00 UTC"


def test_format_local_empty_epoch_is_blank():
    assert format_local(0, "UTC") == ""
    assert format_local(None, "UTC") == ""


def test_format_local_invalid_zone_falls_back_not_raises():
    # A bogus zone must not raise; it degrades to the system/local zone.
    out = format_local(_EPOCH, "Not/AZone")
    assert out and "2025-07-01" in out


def test_format_local_custom_format():
    assert format_local(_EPOCH, "UTC", "%Y%m%d") == "20250701"


def test_resolve_timezone_returns_none_free_zone_object():
    assert resolve_timezone("UTC") is not None
    # Unset resolves to the system local zone (also non-None on a normal host).
    assert resolve_timezone("") is not None
