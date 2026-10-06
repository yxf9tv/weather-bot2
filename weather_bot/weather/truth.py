"""Settlement truth.

Kalshi  : The Weather Company portal relays the NWS CLI daily max. `weather.com/kalshi/api/climate/primary`
          gives the literal settlement value with a status; IEM's CLI archive is the independent cross-check.
Polymarket: max of the hourly "Temp" column on weather.gov/wrh/timeseries = Synoptic obs rounded to whole °F,
          only observations at minutes :51–:59 or :00–:04, local clock day. Rebuilt from METARs (IEM archive).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import httpx

TWC_URL = "https://weather.com/kalshi/api/climate/primary"
IEM_CLI_URL = "https://mesonet.agron.iastate.edu/json/cli.py"
IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
_UA = {"User-Agent": "Mozilla/5.0 (weather-bot-2 research; contact via NWS_USER_AGENT)"}


@dataclass(frozen=True)
class Truth:
    station: str
    target_date: dt.date
    quantity: str
    value: int | None
    status: str  # official | preliminary | revised | no_report | derived | missing
    source: str
    detail: dict


async def twc_daily_max(client: httpx.AsyncClient, date: dt.date) -> dict[str, Truth]:
    """All 43 TWC stations for one date, keyed by ICAO."""
    r = await client.get(TWC_URL, params={"date": date.isoformat()}, headers=_UA)
    r.raise_for_status()
    out: dict[str, Truth] = {}
    for row in r.json().get("results", []):
        st = row.get("station") or {}
        data = row.get("data") or {}
        icao = (st.get("icao") or "").upper()
        if not icao:
            continue
        v = data.get("maxTemp")
        out[icao] = Truth(icao, date, "cli_max", int(v) if v is not None else None, row.get("status") or "missing",
                          "twc", {"cli_id": st.get("cliId"), "issue_time": data.get("issueTime")})
    return out


async def iem_cli_daily_max(client: httpx.AsyncClient, station: str, date: dt.date) -> Truth:
    r = await client.get(IEM_CLI_URL, params={"station": station, "year": date.year}, headers=_UA)
    r.raise_for_status()
    for row in r.json().get("results", []):
        if row.get("valid") == date.isoformat():
            v = row.get("high")
            return Truth(station, date, "cli_max", int(v) if isinstance(v, (int, float)) else None,
                         "official" if isinstance(v, (int, float)) else "missing", "iem_cli",
                         {"product": row.get("product"), "high_time": row.get("high_time")})
    return Truth(station, date, "cli_max", None, "missing", "iem_cli", {})


def hourly_max_from_obs(obs: list[tuple[dt.datetime, float]], date: dt.date, tz: str,
                        hourly_filter: bool = True) -> tuple[int | None, int]:
    """weather.gov page rules: local clock day; with hourly_filter keep only obs at minute 51–59 or 0–4
    (the "Show Hourly Data" view used by US markets); otherwise every observation counts. Round each to a
    whole degree, take the max. Returns (max, n_obs_used)."""
    zone = ZoneInfo(tz)
    vals = []
    for ts, temp in obs:
        local = ts.astimezone(zone)
        if local.date() != date:
            continue
        if hourly_filter and not (local.minute >= 51 or local.minute <= 4):
            continue
        vals.append(int(round(temp)))
    return (max(vals) if vals else None), len(vals)


async def iem_metar_obs(client: httpx.AsyncClient, station: str, date: dt.date, tz: str,
                        unit: str = "F") -> list[tuple[dt.datetime, float]]:
    """Routine + special METAR temps (°F or °C) covering the local day, from IEM (UTC window with a day of slack)."""
    start = dt.datetime.combine(date, dt.time(0, 0), tzinfo=ZoneInfo(tz)).astimezone(dt.timezone.utc)
    end = start + dt.timedelta(hours=30) + dt.timedelta(days=1)  # IEM's day2 is an exclusive 00:00Z boundary
    params = {"station": station[1:] if (station.startswith("K") and len(station) == 4 and unit == "F") else station,
              "data": "tmpc" if unit == "C" else "tmpf",
              "year1": start.year, "month1": start.month, "day1": start.day,
              "year2": end.year, "month2": end.month, "day2": end.day,
              "tz": "UTC", "format": "onlycomma", "latlon": "no", "report_type": "3,4"}
    r = await client.get(IEM_ASOS_URL, params=params, headers=_UA)
    r.raise_for_status()
    out = []
    for line in r.text.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 3 or parts[2] in ("M", ""):
            continue
        ts = dt.datetime.strptime(parts[1], "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
        out.append((ts, float(parts[2])))
    return out


async def polymarket_truth(client: httpx.AsyncClient, station: str, date: dt.date, tz: str, unit: str = "F",
                           hourly_filter: bool = True) -> Truth:
    obs = await iem_metar_obs(client, station, date, tz, unit)
    value, n = hourly_max_from_obs(obs, date, tz, hourly_filter)
    status = "derived" if value is not None and n >= 18 else ("partial" if value is not None else "missing")
    return Truth(station, date, "hourly_max", value, status, "iem_metar_hourly", {"n_obs": n, "unit": unit})
