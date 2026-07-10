"""Server-side weather: Open-Meteo (primary) + wttr.in (fallback)
(FoodAssistant-afqd)."""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import weather  # noqa: E402


# --- Open-Meteo parser -----------------------------------------------------

_OM = {
    "current": {
        "temperature_2m": 72.4, "apparent_temperature": 75.1,
        "relative_humidity_2m": 40, "wind_speed_10m": 8.2, "weather_code": 2,
    },
    "daily": {
        "time": ["2026-06-30", "2026-07-01", "2026-07-02"],
        "temperature_2m_max": [80.6, 78.1, 76.0],
        "temperature_2m_min": [60.2, 58.9, 57.4],
        "weather_code": [0, 61, 3],
        "precipitation_probability_max": [10, 80, 30],
        "wind_speed_10m_max": [12.4, 9.1, 15.6],
        "sunrise": ["2026-06-30T05:31", "2026-07-01T05:32", "2026-07-02T05:33"],
        "sunset": ["2026-06-30T20:42", "2026-07-01T20:42", "2026-07-02T20:41"],
    },
    "hourly": {
        "time": [
            "2026-06-30T00:00", "2026-06-30T12:00",
            "2026-07-01T00:00", "2026-07-01T12:00",
        ],
        "temperature_2m": [61.0, 79.4, 59.0, 77.2],
        "precipitation_probability": [5, 20, 70, 85],
        "weather_code": [0, 2, 61, 63],
    },
}

# An Open-Meteo payload with only the original (pre-detail) fields, used to
# confirm the new day keys degrade to None / [] when the source omits them.
_OM_BASE = {
    "current": _OM["current"],
    "daily": {
        "time": ["2026-06-30", "2026-07-01"],
        "temperature_2m_max": [80.6, 78.1],
        "temperature_2m_min": [60.2, 58.9],
        "weather_code": [0, 61],
    },
}


def test_parse_open_meteo_fahrenheit():
    fc = weather.parse_open_meteo(_OM, "f", "Syracuse, NY")
    assert fc["location"] == "Syracuse, NY" and fc["units"] == "f"
    c = fc["current"]
    assert c["temp"] == "72" and c["feels"] == "75" and c["humidity"] == "40"
    assert c["wind"] == "8" and c["wind_unit"] == "mph" and c["unit"] == "F"
    assert c["desc"] == "Partly cloudy"           # WMO code 2
    assert fc["days"][0]["label"] == "Today" and fc["days"][0]["hi"] == "81"
    assert fc["days"][0]["desc"] == "Clear"       # code 0
    assert fc["days"][1]["label"] == "Tomorrow" and fc["days"][1]["desc"] == "Light rain"


def test_icon_for_maps_conditions():
    assert weather.icon_for("Clear") == "sun"
    assert weather.icon_for("Sunny") == "sun"
    assert weather.icon_for("Partly cloudy") == "cloud-sun"
    assert weather.icon_for("Overcast") == "clouds"
    assert weather.icon_for("Light rain") == "cloud-rain"
    assert weather.icon_for("Heavy rain") == "cloud-rain-heavy"
    assert weather.icon_for("Snow") == "cloud-snow"
    assert weather.icon_for("Thunderstorm") == "cloud-lightning-rain"
    assert weather.icon_for("Fog") == "cloud-fog2"
    assert weather.icon_for("") == "cloud"


def test_food_weather_note_hot():
    note = weather.food_weather_note(95, "F", "Sunny")
    assert "fry an egg" in note


def test_food_weather_note_hot_celsius_converts():
    # 35C is about 95F, so the Celsius reading should hit the same hot band.
    note = weather.food_weather_note(35, "C", "Clear")
    assert "fry an egg" in note


def test_food_weather_note_cold_is_soup_weather():
    note = weather.food_weather_note(20, "F", "Clear")
    assert "Soup weather" in note


def test_food_weather_note_rain_while_warm_mentions_grill():
    note = weather.food_weather_note(72, "F", "Light rain")
    assert "grill" in note.lower()
    assert "wet" in note.lower()


def test_food_weather_note_rain_while_cold_skips_grill():
    note = weather.food_weather_note(45, "F", "Rain")
    assert "grill" not in note.lower()
    assert "stove" in note.lower()


def test_food_weather_note_thunderstorm():
    note = weather.food_weather_note(75, "F", "Thunderstorm")
    assert "thunder" in note.lower()


def test_food_weather_note_snow():
    note = weather.food_weather_note(25, "F", "Heavy snow")
    assert "snow day" in note.lower()


def test_food_weather_note_windy():
    note = weather.food_weather_note(70, "F", "Clear", wind=25)
    assert "windy" in note.lower() or "napkins" in note.lower()


def test_food_weather_note_mild_default():
    note = weather.food_weather_note(72, "F", "Partly cloudy")
    assert note  # some pleasant fallback note, not empty


def test_food_weather_note_missing_temp_does_not_crash():
    assert weather.food_weather_note(None, "F", "") == ""


def test_parse_includes_icon():
    fc = weather.parse_open_meteo(_OM, "f", "X")
    assert fc["current"]["icon"] == "cloud-sun"   # WMO 2 = Partly cloudy
    assert fc["days"][0]["icon"] == "sun"          # WMO 0 = Clear


def test_parse_open_meteo_day_detail_fields():
    fc = weather.parse_open_meteo(_OM, "f", "X")
    d0 = fc["days"][0]
    assert d0["precip"] == "10"
    assert d0["wind"] == "12" and d0["wind_unit"] == "mph"
    assert d0["sunrise"] == "05:31" and d0["sunset"] == "20:42"
    d1 = fc["days"][1]
    assert d1["precip"] == "80"


def test_parse_open_meteo_hourly_strip_filters_by_day():
    fc = weather.parse_open_meteo(_OM, "f", "X")
    hours0 = fc["days"][0]["hourly"]
    assert [h["time"] for h in hours0] == ["00:00", "12:00"]   # only 2026-06-30 rows
    assert hours0[1]["temp"] == "79" and hours0[1]["precip"] == "20"
    assert hours0[0]["icon"] == "sun"                          # WMO 0 = Clear
    hours1 = fc["days"][1]["hourly"]
    assert [h["temp"] for h in hours1] == ["59", "77"]         # only 2026-07-01 rows


def test_parse_open_meteo_celsius_wind_unit():
    fc = weather.parse_open_meteo(_OM, "c", "X")
    assert fc["days"][0]["wind_unit"] == "km/h"


def test_parse_open_meteo_missing_detail_degrades():
    fc = weather.parse_open_meteo(_OM_BASE, "f", "X")
    d0 = fc["days"][0]
    assert d0["precip"] is None and d0["wind"] is None
    assert d0["sunrise"] is None and d0["sunset"] is None
    assert d0["hourly"] == []


def test_parse_open_meteo_rejects_garbage():
    assert weather.parse_open_meteo(None) is None
    assert weather.parse_open_meteo({}, "f") is None          # no current
    assert weather.parse_open_meteo({"current": {}}, "f") is None  # no temp


def test_is_lat_lon():
    assert weather._is_lat_lon("43.05,-76.15") == (43.05, -76.15)
    assert weather._is_lat_lon(" 43.0 , -76.1 ") == (43.0, -76.1)
    assert weather._is_lat_lon("Syracuse, NY") is None
    assert weather._is_lat_lon("999,999") is None             # out of range


# --- wttr.in parser (fallback) --------------------------------------------

_WTTR = {
    "current_condition": [{
        "temp_F": "72", "temp_C": "22", "FeelsLikeF": "75", "FeelsLikeC": "24",
        "humidity": "40", "windspeedMiles": "8", "windspeedKmph": "13",
        "weatherCode": "116", "weatherDesc": [{"value": "Partly cloudy"}],
    }],
    "weather": [
        {"date": "2026-06-30", "maxtempF": "80", "mintempF": "60",
         "maxtempC": "27", "mintempC": "16",
         "astronomy": [{"sunrise": "05:31 AM", "sunset": "08:42 PM"}],
         "hourly": [
             {"time": "0", "tempF": "62", "tempC": "17", "chanceofrain": "5",
              "windspeedMiles": "6", "windspeedKmph": "10", "weatherCode": "116"},
             {"time": "1200", "tempF": "79", "tempC": "26", "chanceofrain": "40",
              "windspeedMiles": "14", "windspeedKmph": "22", "weatherCode": "113"},
             {"time": "2100", "tempF": "66", "tempC": "19", "chanceofrain": "20",
              "windspeedMiles": "8", "windspeedKmph": "13", "weatherCode": "116"},
         ]},
    ],
}


def test_parse_wttr_forecast():
    fc = weather.parse_forecast(_WTTR, "f")
    assert fc["current"]["temp"] == "72" and fc["current"]["desc"] == "Partly cloudy"
    assert fc["days"][0]["hi"] == "80" and fc["days"][0]["desc"] == "Sunny"


def test_parse_wttr_day_detail_and_hours():
    fc = weather.parse_forecast(_WTTR, "f")
    d0 = fc["days"][0]
    assert d0["precip"] == "40"            # max chanceofrain across the day
    assert d0["wind"] == "14" and d0["wind_unit"] == "mph"
    assert d0["sunrise"] == "05:31 AM" and d0["sunset"] == "08:42 PM"
    hours = d0["hourly"]
    assert [h["time"] for h in hours] == ["00:00", "12:00", "21:00"]
    assert hours[1]["temp"] == "79" and hours[1]["precip"] == "40"
    assert hours[1]["icon"] == "sun"       # wttr code 113 = Sunny


def test_parse_wttr_celsius_wind_unit():
    fc = weather.parse_forecast(_WTTR, "c")
    assert fc["days"][0]["wind_unit"] == "km/h" and fc["days"][0]["wind"] == "22"


def test_parse_wttr_rejects_garbage():
    assert weather.parse_forecast({}, "f") is None
    assert weather.parse_forecast(None) is None


# --- fetch flow: Open-Meteo first, wttr fallback ---------------------------

def test_fetch_prefers_open_meteo(monkeypatch):
    import asyncio
    used = []

    async def fake_om(client, loc, units):
        used.append("om")
        return ({"location": loc, "units": units, "current": {"temp": "70"}, "days": []}, "")

    async def fake_wttr(client, loc, units):
        used.append("wttr")
        return (None, "should not be called")

    monkeypatch.setattr(weather, "_fetch_open_meteo", fake_om)
    monkeypatch.setattr(weather, "_fetch_wttr", fake_wttr)
    fc, err = asyncio.run(weather.fetch_forecast("Syracuse, NY", "f"))
    assert fc is not None and err == "" and used == ["om"]   # wttr not tried


def test_fetch_falls_back_to_wttr(monkeypatch):
    import asyncio
    used = []

    async def fake_om(client, loc, units):
        used.append("om")
        return (None, "weather service returned HTTP 429")

    async def fake_wttr(client, loc, units):
        used.append("wttr")
        return ({"location": loc, "units": units, "current": {"temp": "68"}, "days": []}, "")

    monkeypatch.setattr(weather, "_fetch_open_meteo", fake_om)
    monkeypatch.setattr(weather, "_fetch_wttr", fake_wttr)
    fc, err = asyncio.run(weather.fetch_forecast("Syracuse, NY", "f"))
    assert fc is not None and used == ["om", "wttr"]         # tried OM, then wttr


def test_fetch_reports_error_when_both_fail(monkeypatch):
    import asyncio

    async def fail(client, loc, units):
        return (None, "could not reach the weather service (ConnectError)")

    monkeypatch.setattr(weather, "_fetch_open_meteo", fail)
    monkeypatch.setattr(weather, "_fetch_wttr", fail)
    fc, err = asyncio.run(weather.fetch_forecast("Syracuse, NY", "f"))
    assert fc is None and "could not reach the weather service" in err


def test_weather_page_calls_the_correct_data_path(tmp_path, monkeypatch):
    """The data endpoint lives under /ui, and the page's <base href> is the app
    root, so the page MUST fetch 'ui/weather/data' (not 'weather/data', which
    would 404 and show 'unavailable' forever). Regression guard for that bug."""
    import os
    from unittest.mock import patch
    cwd = os.getcwd()
    os.chdir(SERVICE)
    try:
        from app.config import settings
        monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
        monkeypatch.setattr(settings, "auth_required", False, raising=False)
        from fastapi.testclient import TestClient
        from app.main import app
        with patch.object(type(settings), "is_configured", lambda self: True):
            html = TestClient(app).get("/ui/weather").text
        assert "fetch('ui/weather/data'" in html
        assert "fetch('weather/data'" not in html
    finally:
        os.chdir(cwd)


# --- forecast TTL cache (FoodAssistant-17tb) --------------------------------

def test_cache_key_normalizes_location_and_units():
    assert weather.cache_key("  Syracuse, NY ", "F") == ("syracuse, ny", "f")
    assert weather.cache_key("Syracuse, NY", "c") == ("syracuse, ny", "c")
    # Anything that is not celsius collapses to fahrenheit, matching the
    # endpoint's own units handling.
    assert weather.cache_key("", "kelvin") == ("", "f")
    assert weather.cache_key(None, None) == ("", "f")


def test_forecast_cache_hit_and_expiry():
    c = weather.ForecastCache(ok_ttl=600.0, err_ttl=60.0)
    key = weather.cache_key("Syracuse", "f")
    fc = {"current": {"temp": "70"}, "days": []}
    c.put(key, fc, "", now=1000.0)
    # Warm inside the window, gone at/after the edge.
    assert c.get(key, now=1599.9) == (fc, "")
    assert c.get(key, now=1600.0) is None
    # A repeated get after expiry stays a miss (the entry was dropped).
    assert c.get(key, now=1000.0) is None


def test_forecast_cache_failures_expire_sooner():
    c = weather.ForecastCache(ok_ttl=600.0, err_ttl=60.0)
    key = weather.cache_key("Nowhereville", "f")
    c.put(key, None, "could not find that location", now=0.0)
    assert c.get(key, now=59.9) == (None, "could not find that location")
    assert c.get(key, now=60.0) is None


def test_forecast_cache_put_prunes_expired_entries():
    c = weather.ForecastCache(ok_ttl=100.0, err_ttl=10.0)
    c.put(("a", "f"), {"days": []}, "", now=0.0)
    c.put(("b", "f"), None, "boom", now=0.0)
    # Writing a fresh entry after both expired drops the stale ones.
    c.put(("c", "f"), {"days": []}, "", now=500.0)
    assert set(c._entries) == {("c", "f")}


def test_fetch_forecast_cached_shares_upstream_calls(monkeypatch):
    import asyncio
    calls = []

    async def fake_fetch(location="", units="f"):
        calls.append((location, units))
        return {"current": {"temp": "70"}, "days": []}, ""

    monkeypatch.setattr(weather, "fetch_forecast", fake_fetch)
    cache = weather.ForecastCache()

    async def scenario():
        # Three tiles asking for the same place: one upstream call.
        for _ in range(3):
            fc, err = await weather.fetch_forecast_cached("Syracuse, NY", "f", cache=cache)
            assert fc is not None and err == ""
        # A different location is its own entry.
        await weather.fetch_forecast_cached("Rome, NY", "f", cache=cache)

    asyncio.run(scenario())
    assert calls == [("Syracuse, NY", "f"), ("Rome, NY", "f")]


def test_fetch_forecast_cached_caches_failures_with_error(monkeypatch):
    import asyncio
    calls = []

    async def fake_fetch(location="", units="f"):
        calls.append(location)
        return None, "could not find that location"

    monkeypatch.setattr(weather, "fetch_forecast", fake_fetch)
    cache = weather.ForecastCache()

    async def scenario():
        fc1, err1 = await weather.fetch_forecast_cached("Xyzzy", "f", cache=cache)
        fc2, err2 = await weather.fetch_forecast_cached("Xyzzy", "f", cache=cache)
        # The cached failure keeps its error string for the caller to surface.
        assert fc1 is None and fc2 is None
        assert err1 == err2 == "could not find that location"

    asyncio.run(scenario())
    assert calls == ["Xyzzy"]
