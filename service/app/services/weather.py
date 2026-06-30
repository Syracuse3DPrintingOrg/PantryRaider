"""Server-side weather forecast (FoodAssistant-afqd, -wx2 reliability).

Primary source is Open-Meteo: a free, no-key, very reliable JSON API. The
location name is geocoded to lat/lon with Open-Meteo's geocoder, then the
forecast is fetched. wttr.in (the source the Stream Deck widget uses) is kept as
a fallback, because it is frequently rate-limited and returns error pages, which
is the likely reason the weather screen showed "unavailable".

All parse steps are pure functions so they are unit-testable without a network.
The public ``fetch_forecast`` returns ``(forecast, error)`` so the caller can
show why it failed instead of a bare "unavailable".
"""
from __future__ import annotations

import re
from typing import Any

# --- Open-Meteo (primary) -------------------------------------------------

# WMO weather codes -> short description (Open-Meteo's ``weather_code``).
_WMO = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm + hail", 99: "Severe thunderstorm",
}


def _wmo_desc(code) -> str:
    try:
        return _WMO.get(int(code), "")
    except (TypeError, ValueError):
        return ""


def _round(value) -> str:
    """Render a number as a clean integer string, or '?' when missing."""
    try:
        return str(round(float(value)))
    except (TypeError, ValueError):
        return "?"


def _is_lat_lon(text: str) -> tuple[float, float] | None:
    """Parse a bare 'lat,lon' string, or None. Lets a user skip geocoding."""
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", text or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def parse_open_meteo(data: Any, units: str = "f", location: str = "") -> dict | None:
    """Parse an Open-Meteo forecast payload into the render-ready shape, or None.

    Shape: ``{location, units, current: {...}, days: [{...}]}``. Pure.
    """
    if not isinstance(data, dict):
        return None
    units = "c" if str(units).lower() == "c" else "f"
    u = "F" if units == "f" else "C"
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    if not isinstance(cur, dict) or "temperature_2m" not in cur:
        return None
    current = {
        "temp": _round(cur.get("temperature_2m")),
        "feels": _round(cur.get("apparent_temperature", cur.get("temperature_2m"))),
        "humidity": _round(cur.get("relative_humidity_2m")),
        "wind": _round(cur.get("wind_speed_10m")),
        "wind_unit": "mph" if units == "f" else "km/h",
        "desc": _wmo_desc(cur.get("weather_code")),
        "unit": u,
    }
    times = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    codes = daily.get("weather_code") or []
    tags = ("Today", "Tomorrow")
    days: list[dict] = []
    for i, date in enumerate(times):
        days.append({
            "label": tags[i] if i < len(tags) else str(date),
            "date": str(date),
            "hi": _round(highs[i]) if i < len(highs) else "?",
            "lo": _round(lows[i]) if i < len(lows) else "?",
            "desc": _wmo_desc(codes[i]) if i < len(codes) else "",
            "unit": u,
        })
    return {"location": location, "units": units, "current": current, "days": days}


async def _geocode(client, name: str) -> tuple[float, float] | None:
    """Resolve a place name to (lat, lon) via Open-Meteo's geocoder, or None.

    A trailing region (", NY") is used to prefer the right match among results
    rather than being sent as part of the city name, which the geocoder dislikes.
    """
    coords = _is_lat_lon(name)
    if coords:
        return coords
    raw = (name or "").strip()
    if not raw:
        return None
    city = raw.split(",", 1)[0].strip()
    region = raw.split(",", 1)[1].strip().lower() if "," in raw else ""
    try:
        r = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 5, "language": "en", "format": "json"},
        )
        results = (r.json() or {}).get("results") or [] if r.status_code == 200 else []
    except Exception:  # noqa: BLE001
        return None
    if not results:
        return None
    if region:
        for res in results:
            hay = " ".join(str(res.get(k, "")) for k in ("admin1", "admin1_id", "country", "country_code")).lower()
            if region in hay or region.replace(" ", "") in hay.replace(" ", ""):
                return res.get("latitude"), res.get("longitude")
    first = results[0]
    return first.get("latitude"), first.get("longitude")


async def _fetch_open_meteo(client, location: str, units: str) -> tuple[dict | None, str]:
    coords = await _geocode(client, location) if location.strip() else None
    if location.strip() and not coords:
        return None, "could not find that location"
    params = {
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code",
        "timezone": "auto",
        "forecast_days": 4,
        "temperature_unit": "fahrenheit" if str(units).lower() != "c" else "celsius",
        "wind_speed_unit": "mph" if str(units).lower() != "c" else "kmh",
    }
    if coords:
        params["latitude"], params["longitude"] = coords[0], coords[1]
    try:
        r = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
    except Exception as e:  # noqa: BLE001
        return None, f"could not reach the weather service ({e.__class__.__name__})"
    if r.status_code != 200:
        return None, f"weather service returned HTTP {r.status_code}"
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return None, "weather service did not return forecast data"
    parsed = parse_open_meteo(data, units, location)
    if parsed is None:
        return None, "could not parse the forecast"
    return parsed, ""


# --- wttr.in (fallback) ---------------------------------------------------

_CONDITION = {
    113: "Sunny", 116: "Partly cloudy", 119: "Cloudy", 122: "Overcast",
    143: "Mist", 176: "Patchy rain", 179: "Patchy snow", 182: "Sleet",
    185: "Drizzle", 200: "Thundery", 227: "Blowing snow", 230: "Blizzard",
    248: "Fog", 263: "Drizzle", 266: "Drizzle", 293: "Light rain",
    296: "Light rain", 299: "Rain", 302: "Rain", 305: "Heavy rain",
    308: "Heavy rain", 323: "Light snow", 326: "Light snow", 329: "Snow",
    332: "Snow", 335: "Heavy snow", 338: "Heavy snow", 353: "Light showers",
    356: "Showers", 359: "Heavy showers", 368: "Snow showers", 386: "Thundery showers",
}


def _desc(cond: dict) -> str:
    try:
        code = int(cond.get("weatherCode", 0))
    except (TypeError, ValueError):
        code = 0
    if code in _CONDITION:
        return _CONDITION[code]
    try:
        return str(cond.get("weatherDesc", [{}])[0].get("value", "")).strip()
    except Exception:
        return ""


def parse_forecast(data: Any, units: str = "f") -> dict | None:
    """Parse a wttr.in j1 payload into the render-ready shape, or None. Pure."""
    if not isinstance(data, dict):
        return None
    units = "c" if str(units).lower() == "c" else "f"
    u = "F" if units == "f" else "C"
    cc = data.get("current_condition")
    if not cc or not isinstance(cc, list) or not isinstance(cc[0], dict):
        return None
    cond = cc[0]
    current = {
        "temp": cond.get("temp_F" if units == "f" else "temp_C", "?"),
        "feels": cond.get("FeelsLikeF" if units == "f" else "FeelsLikeC", "?"),
        "humidity": cond.get("humidity", "?"),
        "wind": cond.get("windspeedMiles" if units == "f" else "windspeedKmph", "?"),
        "wind_unit": "mph" if units == "f" else "km/h",
        "desc": _desc(cond),
        "unit": u,
    }
    tags = ("Today", "Tomorrow")
    days: list[dict] = []
    for i, day in enumerate(data.get("weather", []) or []):
        if not isinstance(day, dict):
            continue
        hourly = day.get("hourly") or []
        mid = hourly[len(hourly) // 2] if hourly else {}
        days.append({
            "label": tags[i] if i < len(tags) else str(day.get("date", "")),
            "date": str(day.get("date", "")),
            "hi": day.get("maxtempF" if units == "f" else "maxtempC", "?"),
            "lo": day.get("mintempF" if units == "f" else "mintempC", "?"),
            "desc": _desc(mid) if isinstance(mid, dict) else "",
            "unit": u,
        })
    if not days and not current.get("temp"):
        return None
    return {"units": units, "current": current, "days": days}


async def _fetch_wttr(client, location: str, units: str) -> tuple[dict | None, str]:
    loc = (location or "").strip().replace(" ", "+")
    url = f"https://wttr.in/{loc}?format=j1"
    try:
        r = await client.get(url, headers={"User-Agent": "foodassistant-weather/1.0"})
    except Exception as e:  # noqa: BLE001
        return None, f"could not reach the weather service ({e.__class__.__name__})"
    if r.status_code != 200:
        return None, f"weather service returned HTTP {r.status_code}"
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return None, "weather service did not return forecast data"
    parsed = parse_forecast(data, units)
    if parsed is None:
        return None, "could not parse the forecast"
    parsed["location"] = location
    return parsed, ""


# --- public ---------------------------------------------------------------

async def fetch_forecast(location: str = "", units: str = "f") -> tuple[dict | None, str]:
    """Fetch a forecast, preferring Open-Meteo and falling back to wttr.in.

    Returns ``(forecast, "")`` on success or ``(None, error)`` with the reason.
    A blank location geolocates from this server's egress IP (wttr.in path);
    Open-Meteo needs coordinates, so a blank location skips straight to wttr.in.
    """
    import httpx
    last_error = ""
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            if location.strip():
                forecast, err = await _fetch_open_meteo(client, location, units)
                if forecast is not None:
                    return forecast, ""
                last_error = err
            forecast, err = await _fetch_wttr(client, location, units)
            if forecast is not None:
                return forecast, ""
            last_error = err or last_error
    except Exception as e:  # noqa: BLE001
        return None, f"weather lookup failed ({e.__class__.__name__})"
    return None, last_error or "forecast unavailable"
