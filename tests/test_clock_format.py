"""12/24-hour clock format setting (FoodAssistant-v3ui).

clock_format ("auto" | "12" | "24") controls how times of day read on every
surface: the screensaver clock, the weather page's hourly strip and
sunrise/sunset, and server-rendered timestamps like "last checked". It syncs
across the fleet next to the timezone, so all the clocks in a kitchen agree.
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import (  # noqa: E402
    CLOCK_FORMATS, _DEFAULT_CLOCK_FORMAT, _SAVEABLE, SATELLITE_PULL_FIELDS,
    format_local, format_time_of_day, settings,
)
from app.services import weather  # noqa: E402

# 2025-07-01 16:00:00 UTC (noon EDT), same instant test_timezone.py uses.
_EPOCH = 1751385600


# -- the setting itself ------------------------------------------------------

def test_default_is_auto_and_values_are_pinned():
    assert type(settings)().clock_format == "auto"
    assert _DEFAULT_CLOCK_FORMAT == "auto"
    assert CLOCK_FORMATS == ("auto", "12", "24")


def test_clock_format_persists_and_syncs_with_the_timezone():
    assert "clock_format" in _SAVEABLE
    # Fleet-synced next to the timezone: both are "how does a time read"
    # choices and every surface in one kitchen should agree.
    assert "clock_format" in SATELLITE_PULL_FIELDS
    assert "timezone" in SATELLITE_PULL_FIELDS


def test_setup_payload_round_trips_clock_format():
    from app.routers.setup import SetupPayload

    assert SetupPayload(clock_format="12").clock_format == "12"
    # Absent from a partial save = the stored value is left alone.
    assert "clock_format" not in SetupPayload().model_dump(exclude_unset=True)


# -- format_time_of_day (pure resolver) --------------------------------------

def test_time_of_day_12_hour():
    assert format_time_of_day(15, 42, "12") == "3:42 PM"
    assert format_time_of_day(0, 5, "12") == "12:05 AM"
    assert format_time_of_day(12, 0, "12") == "12:00 PM"
    assert format_time_of_day(11, 59, "12") == "11:59 AM"


def test_time_of_day_24_hour_and_auto_keep_the_24h_reading():
    assert format_time_of_day(15, 42, "24") == "15:42"
    assert format_time_of_day(15, 42, "auto") == "15:42"
    assert format_time_of_day(9, 5, "24") == "09:05"


def test_time_of_day_out_of_range_never_raises():
    assert format_time_of_day(24, 60, "12") == "12:00 AM"
    assert format_time_of_day(25, 61, "24") == "01:01"


# -- format_local (the "last checked" style timestamps) -----------------------

def test_format_local_12_hour_reading():
    assert format_local(_EPOCH, "America/New_York", clock_format="12") == \
        "2025-07-01 12:00 PM EDT"
    assert format_local(_EPOCH, "UTC", clock_format="12") == \
        "2025-07-01 4:00 PM UTC"


def test_format_local_auto_and_24_keep_the_existing_reading():
    for cf in ("auto", "24"):
        assert format_local(_EPOCH, "UTC", clock_format=cf) == "2025-07-01 16:00 UTC"


def test_format_local_12_with_a_format_lacking_a_time_part():
    # No %H:%M in the format = nothing to re-read; strftime output unchanged.
    assert format_local(_EPOCH, "UTC", "%Y%m%d", clock_format="12") == "20250701"


# -- weather labels -----------------------------------------------------------

def test_hour_label_12_hour():
    assert weather.format_hour_label("15:00", "12") == "3 PM"
    assert weather.format_hour_label("00:00", "12") == "12 AM"
    assert weather.format_hour_label("12:00", "12") == "12 PM"
    # Sunrise/sunset keep their minutes.
    assert weather.format_hour_label("05:31", "12") == "5:31 AM"
    assert weather.format_hour_label("20:47", "12") == "8:47 PM"


def test_hour_label_auto_24_and_garbage_pass_through():
    assert weather.format_hour_label("15:00", "auto") == "15:00"
    assert weather.format_hour_label("15:00", "24") == "15:00"
    assert weather.format_hour_label("99:99", "12") == "99:99"
    assert weather.format_hour_label("", "12") == ""
    assert weather.format_hour_label("noonish", "12") == "noonish"


def _forecast():
    return {
        "location": "Syracuse", "units": "f",
        "current": {"temp": "72"},
        "days": [{
            "label": "Today", "sunrise": "05:31", "sunset": "20:47",
            "hourly": [{"time": "09:00", "temp": "70"},
                       {"time": "15:00", "temp": "78"}],
        }],
    }


def test_apply_clock_format_re_reads_every_time_label():
    out = weather.apply_clock_format(_forecast(), "12")
    day = out["days"][0]
    assert day["sunrise"] == "5:31 AM"
    assert day["sunset"] == "8:47 PM"
    assert [h["time"] for h in day["hourly"]] == ["9 AM", "3 PM"]


def test_apply_clock_format_never_mutates_the_cached_forecast():
    original = _forecast()
    weather.apply_clock_format(original, "12")
    assert original == _forecast()


def test_apply_clock_format_auto_and_24_return_the_input_untouched():
    fc = _forecast()
    assert weather.apply_clock_format(fc, "auto") is fc
    assert weather.apply_clock_format(fc, "24") is fc


# -- surface wiring -----------------------------------------------------------

def test_base_template_stamps_the_setting_on_html():
    html = (SERVICE / "app" / "templates" / "base.html").read_text()
    assert 'data-clock-format="{{ clock_format' in html


def test_theme_context_exposes_clock_format():
    import inspect
    from app import templating

    assert '"clock_format"' in inspect.getsource(templating.theme_context)


def test_screensaver_clock_reads_the_setting():
    js = (SERVICE / "app" / "static" / "js" / "screensaver.js").read_text()
    assert "data-clock-format" in js
    # 12-hour mode renders the small AM/PM tag next to 3:42.
    assert "ss-ampm" in js and "'AM' : 'PM'" in js


def test_advanced_pane_has_the_control_next_to_the_timezone():
    pane = (SERVICE / "app" / "templates" / "setup" /
            "_pane_advanced.html").read_text()
    assert 'id="clock_format"' in pane
    assert pane.index('id="timezone"') < pane.index('id="clock_format"')
