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
         "maxtempC": "27", "mintempC": "16", "hourly": [{"weatherCode": "113"}]},
    ],
}


def test_parse_wttr_forecast():
    fc = weather.parse_forecast(_WTTR, "f")
    assert fc["current"]["temp"] == "72" and fc["current"]["desc"] == "Partly cloudy"
    assert fc["days"][0]["hi"] == "80" and fc["days"][0]["desc"] == "Sunny"


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
