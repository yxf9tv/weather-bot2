"""api.weather.gov gridpoint maxTemperature, bucketed by station local date."""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

import httpx

_DUR_RE = re.compile(r"P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?)?")


async def fetch_grid_url(client: httpx.AsyncClient, lat: float, lon: float, user_agent: str) -> str | None:
    r = await client.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
                         headers={"User-Agent": user_agent, "Accept": "application/geo+json"})
    if r.status_code != 200:
        return None
    return (r.json().get("properties") or {}).get("forecastGridData")


async def fetch_max_temps(client: httpx.AsyncClient, grid_url: str, tz: str, user_agent: str) -> dict[dt.date, float]:
    r = await client.get(grid_url, headers={"User-Agent": user_agent, "Accept": "application/geo+json"})
    if r.status_code != 200:
        return {}
    return parse_max_temps(r.json(), tz)


def parse_max_temps(payload: dict, tz: str) -> dict[dt.date, float]:
    """Return {local_date: max temp °F} using the period midpoint to pick the local date."""
    out: dict[dt.date, float] = {}
    values = ((payload.get("properties") or {}).get("maxTemperature") or {}).get("values") or []
    zone = ZoneInfo(tz)
    for v in values:
        if v.get("value") is None:
            continue
        start_s, _, dur = v["validTime"].partition("/")
        start = dt.datetime.fromisoformat(start_s.replace("Z", "+00:00"))
        m = _DUR_RE.fullmatch(dur or "PT0H")
        hours = (int(m["d"] or 0) * 24 + int(m["h"] or 0)) if m else 0
        mid = start + dt.timedelta(hours=hours / 2)
        local_date = mid.astimezone(zone).date()
        temp_f = float(v["value"]) * 9 / 5 + 32
        out[local_date] = max(out.get(local_date, -999.0), temp_f)
    return out
