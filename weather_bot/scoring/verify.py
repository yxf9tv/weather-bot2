"""Thermometer check: compare our truth feeds against what each venue actually settled."""

from __future__ import annotations

import datetime as dt
import json

import httpx

from ..config import Settings
from ..markets.rules import parse_bin_label
from ..markets.station_lookup import load_cache
from ..markets.stations import CLI_TO_ICAO, KALSHI_SERIES_STATION, POLYMARKET_CITY_STATION, STATIONS
from ..weather import truth as T


async def verify_date(settings: Settings, date: dt.date) -> list[str]:
    lines: list[str] = []
    mismatches = 0
    cache = load_cache(settings.stations_cache)
    intl = {row["city"]: icao for icao, row in cache.items() if row.get("city")}
    async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
        lines.append(f"=== POLYMARKET {date}: venue result vs rebuilt hourly max")
        for city, icao in list(POLYMARKET_CITY_STATION.items()) + sorted(intl.items()):
            slug = f"highest-temperature-in-{city}-on-{date:%B}-{date.day}-{date.year}".lower()
            r = await c.get(f"{settings.gamma_host}/events", params={"slug": slug})
            ev = r.json() if r.status_code == 200 else []
            if not ev:
                lines.append(f"  {icao:<5} no event")
                continue
            win = None
            for m in ev[0]["markets"]:
                prices = json.loads(m.get("outcomePrices") or "[]")
                if prices and float(prices[0]) > 0.9:
                    win = m["groupItemTitle"]
            st = STATIONS[icao]
            t = await T.polymarket_truth(c, icao, date, st.tz, st.unit, hourly_filter=(st.unit == "F"))
            b = parse_bin_label(win, "x") if win else None
            ok = b is not None and t.value is not None and b.contains(t.value)
            mismatches += 0 if ok or win is None else 1
            lines.append(f"  {icao:<5} {city:<14} venue {str(win):<16} ours {t.value}°{st.unit} ({t.status}, n={t.detail.get('n_obs')})  "
                         f"{'OK' if ok else ('unresolved' if win is None else 'MISMATCH')}")
        lines.append(f"=== KALSHI {date}: settled result vs The Weather Company")
        twc = await T.twc_daily_max(c, date)
        for series, cli in KALSHI_SERIES_STATION.items():
            icao = CLI_TO_ICAO[cli]
            r = await c.get(f"{settings.kalshi_host}/markets",
                            params={"series_ticker": series, "status": "settled", "limit": 60})
            suffix = f"{date:%y%b%d}".upper()
            ms = [m for m in r.json().get("markets", []) if m["event_ticker"].endswith(suffix)]
            win = [m.get("yes_sub_title") for m in ms if m.get("result") == "yes"]
            t = twc.get(icao)
            b = parse_bin_label(win[0], "x") if win else None
            ok = b is not None and t is not None and t.value is not None and b.contains(t.value)
            mismatches += 0 if ok or not ms else 1
            lines.append(f"  {icao:<5} venue {str(win[0] if win else None):<16} TWC {t.value if t else None} "
                         f"({t.status if t else '-'})  {'OK' if ok else ('n/a' if not ms else 'MISMATCH')}")
    lines.append(f"mismatches: {mismatches}")
    return lines
