"""Resolve unknown ICAO stations (international Polymarket cities) to lat/lon/tz and register them.

Coordinates: aviationweather.gov station info. Time zone: Open-Meteo `timezone=auto`. Cached in
data/stations_cache.json so lookups happen once per station.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from .stations import STATIONS, Station

AWC_URL = "https://aviationweather.gov/api/data/stationinfo"
OM_URL = "https://api.open-meteo.com/v1/forecast"
_UA = {"User-Agent": "Mozilla/5.0 (weather-bot-2)"}


def load_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    for icao, row in data.items():
        if icao not in STATIONS:
            STATIONS[icao] = Station(icao, row["name"], row["lat"], row["lon"], row["tz"], None, row.get("unit", "C"))
    return data


def save_cache(path: Path, cache: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=1, sort_keys=True))


async def lookup_station(client: httpx.AsyncClient, icao: str, cache_path: Path, *, unit: str = "C",
                         city: str | None = None) -> Station | None:
    icao = icao.upper()
    if icao in STATIONS:
        return STATIONS[icao]
    cache = load_cache(cache_path)
    if icao in cache:
        return STATIONS.get(icao)
    try:
        r = await client.get(AWC_URL, params={"ids": icao, "format": "json"}, headers=_UA)
        r.raise_for_status()
        rows = r.json() or []
    except (httpx.HTTPError, ValueError):
        return None
    row = next((x for x in rows if (x.get("icaoId") or "").upper() == icao), None)
    if not row or row.get("lat") is None or row.get("lon") is None:
        return None
    lat, lon = float(row["lat"]), float(row["lon"])
    try:
        r = await client.get(OM_URL, params={"latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1,
                                             "daily": "temperature_2m_max"})
        r.raise_for_status()
        tz = r.json().get("timezone")
    except (httpx.HTTPError, ValueError):
        tz = None
    if not tz:
        return None
    st = Station(icao, row.get("site") or icao, lat, lon, tz, None, unit)
    STATIONS[icao] = st
    cache[icao] = {"name": st.name, "lat": lat, "lon": lon, "tz": tz, "unit": unit, "city": city,
                   "country": row.get("country")}
    save_cache(cache_path, cache)
    return st
