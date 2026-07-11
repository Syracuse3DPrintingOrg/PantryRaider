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
import time
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


def icon_for(desc: str) -> str:
    """Map a condition description to a Bootstrap Icons glyph (no 'bi-' prefix),
    so the page can show a weather icon (FoodAssistant-q9du). Works for both the
    Open-Meteo and wttr.in descriptions since they share vocabulary. Pure."""
    d = (desc or "").lower()
    if "thunder" in d:
        return "cloud-lightning-rain"
    if any(w in d for w in ("snow", "sleet", "blizzard", "ice", "grains")):
        return "cloud-snow"
    if "heavy" in d and ("rain" in d or "shower" in d):
        return "cloud-rain-heavy"
    if any(w in d for w in ("rain", "shower", "drizzle")):
        return "cloud-rain"
    if "fog" in d or "mist" in d:
        return "cloud-fog2"
    if "partly" in d:
        return "cloud-sun"
    if "clear" in d or "sunny" in d:
        return "sun"
    if "cloud" in d or "overcast" in d:
        return "clouds"
    return "cloud"


# Candidate food-and-kitchen lines per weather bucket (FoodAssistant-fytk).
# Playful, gently double-meaning, never crude. Index 0 of each bucket is the
# canonical line ``food_weather_note`` returns; ``forecast_insight`` rotates
# through the whole list day by day so the kitchen sees some variety without
# ever needing a network call or a random seed. Keep every line em-dash free.
_INSIGHT_LINES: dict[str, list[str]] = {
    "stormy": [
        "Thunder in the forecast: bring the grill session indoors and let the oven do the work.",
        "Storms rolling in. A good excuse to slow-braise something and let the thunder set the mood.",
        "Lightning out, low heat in. This is a stay-put-and-simmer kind of day.",
    ],
    "snowy": [
        "Snow day. Time for cocoa and something simmering on the stove.",
        "Snow is falling, so get a pot going and let the kitchen fog up the windows.",
        "White stuff outside, warm stuff on the stove. Roast something and take your time.",
    ],
    "hot": [
        "Hot enough to fry an egg on the sidewalk. Maybe skip the stove today.",
        "Real scorcher out there. Let the grill stay cold and let the fridge do the cooking.",
        "Too hot to fire up the oven. This is a cold-plate, tall-glass afternoon.",
        "The sidewalk is basically a griddle today. Keep it light and keep it chilled.",
    ],
    "rain_warm": [
        "Fire up the grill, but you might get wet.",
        "Warm enough to grill, wet enough to regret it. Keep the umbrella close.",
        "You can still work the grill, you will just be seasoning it with rain.",
    ],
    "rain": [
        "A little damp out there. Good day to keep something warm on the stove.",
        "Grey and drizzly. The stove is the coziest spot in the house right now.",
        "Rain is out, so let something low and slow steam up the kitchen.",
    ],
    "freezing": [
        "Soup weather, no question.",
        "Cold enough that the soup pot is practically calling your name.",
        "Bundle up, or just stand near the stew. Soup weather, plain and simple.",
    ],
    "windy": [
        "Windy enough to blow the napkins off the table. Weigh them down.",
        "Gusty out there. Anything lighter than a cast-iron pan might take flight.",
        "Hold onto your napkins. It is a weigh-down-the-tablecloth kind of day.",
    ],
    "pleasant": [
        "Kitchen weather is pretty good today too.",
        "Gorgeous out. A fine day to prep on the porch and let the kitchen breathe.",
        "Easy going outside, which makes it a good day to cook with the windows open.",
    ],
}


def _insight_bucket(temp, unit: str = "F", desc: str = "", wind=None) -> str:
    """Classify current conditions into an insight bucket key, or "" when
    nothing stands out. Pure; ``temp`` is read in Fahrenheit internally,
    converting from Celsius when ``unit`` is "C"."""
    try:
        t = float(temp)
    except (TypeError, ValueError):
        t = None
    if t is not None and str(unit).strip().upper() == "C":
        t = t * 9 / 5 + 32
    try:
        w = float(wind)
    except (TypeError, ValueError):
        w = None

    d = (desc or "").lower()
    is_stormy = "thunder" in d
    is_snowy = any(word in d for word in ("snow", "sleet", "blizzard"))
    is_rainy = any(word in d for word in ("rain", "shower", "drizzle")) and not is_snowy

    if is_stormy:
        return "stormy"
    if is_snowy:
        return "snowy"
    if t is not None and t >= 90:
        return "hot"
    if is_rainy and t is not None and t >= 60:
        return "rain_warm"
    if is_rainy:
        return "rain"
    if t is not None and t <= 35:
        return "freezing"
    if w is not None and w >= 20:
        return "windy"
    if t is not None and 65 <= t <= 80:
        return "pleasant"
    return ""


def _insight_candidates(temp, unit: str = "F", desc: str = "", wind=None) -> list[str]:
    """The candidate lines for the current conditions, or [] when none fit. Pure."""
    return _INSIGHT_LINES.get(_insight_bucket(temp, unit, desc, wind), [])


def food_weather_note(temp, unit: str = "F", desc: str = "", wind=None) -> str:
    """A short, food-themed note for the current conditions (FoodAssistant-fytk).

    Playful, gentle double meaning, one line: a hot day fries an egg on the
    sidewalk, rain while it's warm enough to grill gets you wet at the grill,
    a cold snap is soup weather, snow calls for cocoa and something simmering,
    a gusty day rattles the napkins. Falls back to a plain pleasant note when
    nothing stands out. Pure and deterministic; returns the canonical (first)
    line for the matched bucket, or "" when nothing stands out."""
    candidates = _insight_candidates(temp, unit, desc, wind)
    return candidates[0] if candidates else ""


def forecast_insight(current: dict, day: int | None = None) -> str:
    """A playful, food-or-kitchen-themed line for a parsed ``current`` block.

    Reads the render-ready ``current`` dict (``temp``, ``unit``, ``desc``,
    ``wind``) that ``parse_open_meteo`` / ``parse_forecast`` produce, picks the
    matching bucket, and rotates through that bucket's candidate lines by
    day-of-year so the line is stable within a day but shifts day to day. Pass
    ``day`` (1-366) to pin the rotation in tests. Returns "" when conditions do
    not fit a bucket or ``current`` is not a dict. Pure and deterministic."""
    if not isinstance(current, dict):
        return ""
    candidates = _insight_candidates(
        current.get("temp"), current.get("unit", "F"),
        current.get("desc", ""), current.get("wind"),
    )
    if not candidates:
        return ""
    if day is None:
        from datetime import date
        day = date.today().timetuple().tm_yday
    return candidates[int(day) % len(candidates)]


def _round(value) -> str:
    """Render a number as a clean integer string, or '?' when missing."""
    try:
        return str(round(float(value)))
    except (TypeError, ValueError):
        return "?"


def _opt_round(value):
    """Like ``_round`` but returns None when missing, so the template can hide
    a stat the source did not provide rather than showing '?'. Pure."""
    try:
        return str(round(float(value)))
    except (TypeError, ValueError):
        return None


def _clock(value) -> str | None:
    """Pull 'HH:MM' out of an ISO timestamp like '2026-06-30T05:31', or None.
    Pure, no timezone math (Open-Meteo already returns local times)."""
    text = str(value or "").strip()
    if "T" in text:
        text = text.split("T", 1)[1]
    m = re.match(r"^(\d{1,2}:\d{2})", text)
    return m.group(1) if m else None


def format_hour_label(hhmm: str, clock_format: str) -> str:
    """Re-read an 'HH:MM' label per the clock_format setting. Pure.

    "12" turns 15:00 into 3 PM (an on-the-hour label drops :00 to keep the
    hourly strip compact) and 05:31 into 5:31 AM; "auto" and "24" keep the
    24-hour label unchanged, as does anything that is not HH:MM."""
    if clock_format != "12":
        return hhmm
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(hhmm or ""))
    if not m:
        return hhmm
    h, minute = int(m.group(1)), int(m.group(2))
    if h > 23 or minute > 59:
        return hhmm
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return (f"{h12} {suffix}" if minute == 0 else f"{h12}:{minute:02d} {suffix}")


def apply_clock_format(forecast: dict, clock_format: str) -> dict:
    """A copy of a parsed forecast with every time-of-day label (the hourly
    strip, sunrise, sunset) re-read per clock_format. Pure and non-mutating:
    the input may live in the shared forecast cache, so nothing is edited in
    place. "auto" and "24" return the input untouched."""
    if clock_format != "12" or not isinstance(forecast, dict):
        return forecast
    out = dict(forecast)
    days = []
    for day in forecast.get("days") or []:
        if not isinstance(day, dict):
            days.append(day)
            continue
        d = dict(day)
        for key in ("sunrise", "sunset"):
            if d.get(key):
                d[key] = format_hour_label(d[key], clock_format)
        hours = []
        for h in d.get("hourly") or []:
            if isinstance(h, dict) and h.get("time"):
                h = dict(h)
                h["time"] = format_hour_label(h["time"], clock_format)
            hours.append(h)
        d["hourly"] = hours
        days.append(d)
    out["days"] = days
    return out


def _parse_om_hours(hourly: dict, date: str) -> list[dict]:
    """Build a day's hourly strip from an Open-Meteo ``hourly`` block, keeping
    only the rows whose timestamp falls on ``date``. Pure."""
    if not isinstance(hourly, dict):
        return []
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    probs = hourly.get("precipitation_probability") or []
    codes = hourly.get("weather_code") or []
    out: list[dict] = []
    for i, stamp in enumerate(times):
        stamp = str(stamp)
        if date and not stamp.startswith(date):
            continue
        desc = _wmo_desc(codes[i]) if i < len(codes) else ""
        out.append({
            "time": _clock(stamp) or "",
            "temp": _round(temps[i]) if i < len(temps) else "?",
            "precip": _opt_round(probs[i]) if i < len(probs) else None,
            "icon": icon_for(desc),
            "desc": desc,
        })
    return out


def _is_lat_lon(text: str) -> tuple[float, float] | None:
    """Parse a bare 'lat,lon' string, or None. Lets a user skip geocoding."""
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", text or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def _day_label(i: int, date: str) -> str:
    """Forecast day label: Today, Tomorrow, then the weekday name (Tuesday,
    Wednesday, ...) rather than a bare date (Pantry Raider). Pure."""
    if i == 0:
        return "Today"
    if i == 1:
        return "Tomorrow"
    try:
        from datetime import datetime
        return datetime.strptime(str(date)[:10], "%Y-%m-%d").strftime("%A")
    except Exception:
        return str(date)


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
    cur_desc = _wmo_desc(cur.get("weather_code"))
    current = {
        "temp": _round(cur.get("temperature_2m")),
        "feels": _round(cur.get("apparent_temperature", cur.get("temperature_2m"))),
        "humidity": _round(cur.get("relative_humidity_2m")),
        "wind": _round(cur.get("wind_speed_10m")),
        "wind_unit": "mph" if units == "f" else "km/h",
        "desc": cur_desc,
        "icon": icon_for(cur_desc),
        "unit": u,
    }
    times = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    codes = daily.get("weather_code") or []
    precips = daily.get("precipitation_probability_max") or []
    winds = daily.get("wind_speed_10m_max") or []
    sunrises = daily.get("sunrise") or []
    sunsets = daily.get("sunset") or []
    hourly = data.get("hourly") or {}
    wind_unit = "mph" if units == "f" else "km/h"
    days: list[dict] = []
    for i, date in enumerate(times):
        day_desc = _wmo_desc(codes[i]) if i < len(codes) else ""
        days.append({
            "label": _day_label(i, date),
            "date": str(date),
            "hi": _round(highs[i]) if i < len(highs) else "?",
            "lo": _round(lows[i]) if i < len(lows) else "?",
            "desc": day_desc,
            "icon": icon_for(day_desc),
            "unit": u,
            "precip": _opt_round(precips[i]) if i < len(precips) else None,
            "wind": _opt_round(winds[i]) if i < len(winds) else None,
            "wind_unit": wind_unit,
            "sunrise": _clock(sunrises[i]) if i < len(sunrises) else None,
            "sunset": _clock(sunsets[i]) if i < len(sunsets) else None,
            "hourly": _parse_om_hours(hourly, str(date)),
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
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max,wind_speed_10m_max,sunrise,sunset",
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "timezone": "auto",
        "forecast_days": 4,
        "temperature_unit": "fahrenheit" if str(units).lower() != "c" else "celsius",
        "wind_speed_unit": "mph" if str(units).lower() != "c" else "kmh",
    }
    if coords:
        params["latitude"], params["longitude"] = coords[0], coords[1]
    try:
        from ..config import settings
        base = (getattr(settings, "weather_api_base", "") or "https://api.open-meteo.com").rstrip("/")
        r = await client.get(f"{base}/v1/forecast", params=params)
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


def _wttr_clock(value) -> str:
    """Turn a wttr.in hourly 'time' (e.g. '0', '300', '1500') into 'HH:MM'."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ""
    return f"{n // 100:02d}:{n % 100:02d}"


def _wttr_max_wind(hourly: list, units: str):
    """Largest hourly wind speed across a wttr.in day, or None. Pure."""
    key = "windspeedMiles" if units == "f" else "windspeedKmph"
    speeds = []
    for h in hourly or []:
        if isinstance(h, dict):
            try:
                speeds.append(float(h.get(key)))
            except (TypeError, ValueError):
                continue
    return max(speeds) if speeds else None


def _wttr_day_precip(hourly: list):
    """Highest chance-of-rain across a wttr.in day as a string, or None. Pure."""
    chances = []
    for h in hourly or []:
        if isinstance(h, dict):
            try:
                chances.append(int(h.get("chanceofrain")))
            except (TypeError, ValueError):
                continue
    return str(max(chances)) if chances else None


def _parse_wttr_hours(hourly: list, units: str) -> list[dict]:
    """Build the hourly strip from a wttr.in day's hourly list. Pure."""
    out: list[dict] = []
    for h in hourly or []:
        if not isinstance(h, dict):
            continue
        desc = _desc(h)
        precip = h.get("chanceofrain")
        out.append({
            "time": _wttr_clock(h.get("time")),
            "temp": h.get("tempF" if units == "f" else "tempC", "?"),
            "precip": str(precip) if precip not in (None, "") else None,
            "icon": icon_for(desc),
            "desc": desc,
        })
    return out


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
    cur_desc = _desc(cond)
    current = {
        "temp": cond.get("temp_F" if units == "f" else "temp_C", "?"),
        "feels": cond.get("FeelsLikeF" if units == "f" else "FeelsLikeC", "?"),
        "humidity": cond.get("humidity", "?"),
        "wind": cond.get("windspeedMiles" if units == "f" else "windspeedKmph", "?"),
        "wind_unit": "mph" if units == "f" else "km/h",
        "desc": cur_desc,
        "icon": icon_for(cur_desc),
        "unit": u,
    }
    days: list[dict] = []
    for i, day in enumerate(data.get("weather", []) or []):
        if not isinstance(day, dict):
            continue
        hourly = day.get("hourly") or []
        mid = hourly[len(hourly) // 2] if hourly else {}
        day_desc = _desc(mid) if isinstance(mid, dict) else ""
        astro = (day.get("astronomy") or [{}])[0] if isinstance(day.get("astronomy"), list) else {}
        days.append({
            "label": _day_label(i, day.get("date", "")),
            "date": str(day.get("date", "")),
            "hi": day.get("maxtempF" if units == "f" else "maxtempC", "?"),
            "lo": day.get("mintempF" if units == "f" else "mintempC", "?"),
            "desc": day_desc,
            "icon": icon_for(day_desc),
            "unit": u,
            "precip": _wttr_day_precip(hourly),
            "wind": _opt_round(_wttr_max_wind(hourly, units)),
            "wind_unit": "mph" if units == "f" else "km/h",
            "sunrise": astro.get("sunrise") if isinstance(astro, dict) else None,
            "sunset": astro.get("sunset") if isinstance(astro, dict) else None,
            "hourly": _parse_wttr_hours(hourly, units),
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


# --- TTL cache (FoodAssistant-17tb) -----------------------------------------
#
# The Stream Deck can carry several weather tiles (the shared widget plus
# per-key overrides), and the kiosk weather page fetches too. Without a cache
# every tile re-hits Open-Meteo (or the rate-limited wttr.in fallback) on each
# poll; with it, N tiles sharing a (location, units) pair cost one upstream
# call per TTL window. Failures are cached briefly so a dead upstream is not
# hammered, but recovery is quick.

CACHE_OK_TTL_SECS: float = 600.0    # successful forecasts stay warm 10 minutes
CACHE_ERR_TTL_SECS: float = 60.0    # failures retry after a minute


def cache_key(location: str, units: str) -> tuple[str, str]:
    """Normalize (location, units) so trivially-different spellings share an
    entry: whitespace-trimmed, case-folded location; units collapsed to f/c."""
    u = "c" if str(units or "").strip().lower() == "c" else "f"
    return ((location or "").strip().lower(), u)


class ForecastCache:
    """In-process TTL cache for ``fetch_forecast`` results.

    Pure: ``get``/``put`` take an explicit ``now`` so expiry is unit-testable
    without sleeping. Entries are ``(expires_at, forecast, error)``; expired
    entries are dropped lazily on ``get`` and pruned on ``put`` so the dict
    never grows past the set of recently-asked locations.
    """

    def __init__(self, ok_ttl: float = CACHE_OK_TTL_SECS,
                 err_ttl: float = CACHE_ERR_TTL_SECS) -> None:
        self.ok_ttl = ok_ttl
        self.err_ttl = err_ttl
        self._entries: dict[tuple[str, str], tuple[float, dict | None, str]] = {}

    def get(self, key: tuple[str, str], now: float) -> tuple[dict | None, str] | None:
        """Return the cached ``(forecast, error)`` or None when absent/expired."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, forecast, error = entry
        if now >= expires_at:
            self._entries.pop(key, None)
            return None
        return forecast, error

    def put(self, key: tuple[str, str], forecast: dict | None, error: str,
            now: float) -> None:
        ttl = self.ok_ttl if forecast is not None else self.err_ttl
        self._entries[key] = (now + ttl, forecast, error)
        # Prune anything else that has already expired, bounding memory.
        stale = [k for k, (exp, _f, _e) in self._entries.items()
                 if k != key and now >= exp]
        for k in stale:
            self._entries.pop(k, None)

    def clear(self) -> None:
        self._entries.clear()


_forecast_cache = ForecastCache()


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
        # An explicit tight connect timeout: a device with a blackholed IPv6
        # route otherwise waits the full read timeout per connection attempt
        # before anything falls back, which stacked up across tiles is exactly
        # the "everything is slow" satellite symptom (FoodAssistant-17tb).
        timeout = httpx.Timeout(12.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
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


async def fetch_forecast_cached(location: str = "", units: str = "f",
                                cache: ForecastCache | None = None) -> tuple[dict | None, str]:
    """``fetch_forecast`` behind the shared TTL cache.

    All /ui/weather/data callers (the kiosk weather page and every Stream Deck
    weather/forecast tile) go through here, so a deck with three tiles on the
    same location makes one upstream call per TTL window instead of three per
    poll. ``cache`` is injectable for tests; production uses the module cache.
    """
    c = cache if cache is not None else _forecast_cache
    key = cache_key(location, units)
    now = time.monotonic()
    hit = c.get(key, now)
    if hit is not None:
        return hit
    forecast, error = await fetch_forecast(location, units)
    c.put(key, forecast, error, now)
    return forecast, error
