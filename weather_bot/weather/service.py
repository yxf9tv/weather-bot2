"""Fetch and cache forecasts for all stations; build per-(station, date, quantity) distributions."""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass

import httpx

from ..config import Settings
from ..markets.stations import STATIONS, Station
from ..storage.db import Database
from . import nws as nws_mod
from .distribution import Distribution, from_members, from_percentiles
from .nbm import MaxTPercentiles, fetch_latest_nbp, parse_nbp
from .openmeteo import EnsembleDay, fetch_ensemble

OPENMETEO_BACKOFF_MIN = 15


@dataclass(frozen=True)
class StationForecast:
    station: str
    target_date: dt.date
    nbm: MaxTPercentiles | None
    ensemble: EnsembleDay | None
    nws_max: float | None
    fetched_at: dt.datetime

    def nbm_age_h(self, now: dt.datetime) -> float | None:
        return (now - self.nbm.cycle).total_seconds() / 3600 if self.nbm else None


@dataclass(frozen=True)
class Distributions:
    nbm: Distribution | None
    openmeteo: Distribution | None

    @property
    def primary(self) -> Distribution | None:
        return self.nbm or self.openmeteo


class ForecastService:
    def __init__(self, settings: Settings, db: Database | None, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.db = db
        self.client = client or httpx.AsyncClient(timeout=90.0, follow_redirects=True)
        self._nbp: dict[str, dict[dt.date, MaxTPercentiles]] = {}
        self._nbp_cycle: dt.datetime | None = None
        self._ensemble: dict[str, dict[dt.date, EnsembleDay]] = {}
        self._ensemble_at: dict[str, dt.datetime] = {}
        self._nws: dict[str, dict[dt.date, float]] = {}
        self._nws_at: dict[str, dt.datetime] = {}
        self._grid_urls: dict[str, str] = {}
        from ..scoring.calibrate import load as _load_cal

        self.calibration: dict[str, dict] = _load_cal(settings.db_path.parent / "calibration.json")

    # ---------- refresh ----------
    async def refresh_nbm(self, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now(dt.timezone.utc)
        skip = {self._nbp_cycle} if self._nbp_cycle else None
        res = await fetch_latest_nbp(self.client, now, skip_cycles=skip)
        if res is None:
            return False
        cycle, text = res
        parsed = parse_nbp(text, set(STATIONS))
        self._nbp = {st: {r.target_date: r for r in rows} for st, rows in parsed.items()}
        self._nbp_cycle = cycle
        if self.db:
            for st, rows in parsed.items():
                for r in rows:
                    self.db.add_forecast("nbm", st, r.target_date, cycle.isoformat(), r.as_dict())
        return True

    async def refresh_ensemble(self, stations: list[Station], now: dt.datetime | None = None) -> None:
        now = now or dt.datetime.now(dt.timezone.utc)
        due = [s for s in stations if now - self._ensemble_at.get(s.icao, dt.datetime.min.replace(tzinfo=dt.timezone.utc))
               >= dt.timedelta(minutes=self.settings.openmeteo_refresh_min)]
        sem = asyncio.Semaphore(4)

        async def one(s: Station):
            async with sem:
                try:
                    days = await fetch_ensemble(self.client, s.icao, s.lat, s.lon, s.tz, self.settings.openmeteo_api_key,
                                                unit=s.unit)
                except httpx.HTTPError as exc:
                    if self.db:
                        self.db.add_event("warn", "openmeteo", f"{s.icao}: {exc!r}")
                    # Back off: retry this station in OPENMETEO_BACKOFF_MIN, not next cycle. Re-requesting every
                    # failed station every minute is what turned one 429 into a permanent rate limit.
                    self._ensemble_at[s.icao] = (now - dt.timedelta(minutes=self.settings.openmeteo_refresh_min)
                                                 + dt.timedelta(minutes=OPENMETEO_BACKOFF_MIN))
                    return
                self._ensemble[s.icao] = {d.target_date: d for d in days}
                self._ensemble_at[s.icao] = now
                if self.db:
                    for d in days:
                        self.db.add_forecast("openmeteo", s.icao, d.target_date, None, d.as_dict())

        await asyncio.gather(*(one(s) for s in due))

    async def refresh_nws(self, stations: list[Station], now: dt.datetime | None = None) -> None:
        now = now or dt.datetime.now(dt.timezone.utc)
        ua = self.settings.nws_user_agent
        due = [s for s in stations if s.icao.startswith("K") and s.unit == "F"
               and now - self._nws_at.get(s.icao, dt.datetime.min.replace(tzinfo=dt.timezone.utc)) >= dt.timedelta(minutes=30)]
        sem = asyncio.Semaphore(3)

        async def one(s: Station):
            async with sem:
                try:
                    url = self._grid_urls.get(s.icao) or await nws_mod.fetch_grid_url(self.client, s.lat, s.lon, ua)
                    if not url:
                        return
                    self._grid_urls[s.icao] = url
                    temps = await nws_mod.fetch_max_temps(self.client, url, s.tz, ua)
                except httpx.HTTPError as exc:
                    if self.db:
                        self.db.add_event("warn", "nws", f"{s.icao}: {exc!r}")
                    return
                self._nws[s.icao] = temps
                self._nws_at[s.icao] = now
                if self.db:
                    for d, t in temps.items():
                        self.db.add_forecast("nws", s.icao, d, None, {"max_f": t})

        await asyncio.gather(*(one(s) for s in due))

    async def refresh_all(self, stations: list[Station] | None = None) -> None:
        stations = stations or list(STATIONS.values())
        await asyncio.gather(self.refresh_nbm(), self.refresh_ensemble(stations), self.refresh_nws(stations))

    # ---------- access ----------
    def forecast(self, station: str, target_date: dt.date) -> StationForecast:
        return StationForecast(
            station, target_date,
            self._nbp.get(station, {}).get(target_date),
            self._ensemble.get(station, {}).get(target_date),
            self._nws.get(station, {}).get(target_date),
            dt.datetime.now(dt.timezone.utc),
        )

    def distributions(self, fc: StationForecast, quantity: str, unit: str = "F") -> Distributions:
        s = self.settings
        om = None
        if fc.ensemble and fc.ensemble.all_members():
            if unit == "C":
                # International truth counts every observation, so no hourly-max discount; apply measured model bias.
                om = from_members(fc.ensemble.all_members(), bias_f=s.intl_ensemble_bias_c, inflation=1.4,
                                  extra_sigma_f=s.openmeteo_extra_sigma_c)
            else:
                om_offset = s.hourly_max_delta_f if quantity == "hourly_max" else 0.0
                om = from_members(fc.ensemble.all_members(), bias_f=om_offset, inflation=1.4,
                                  extra_sigma_f=s.openmeteo_extra_sigma_f)
        if unit == "C" or fc.nbm is None:
            return Distributions(None, om)
        offset = s.cli_window_delta_f if quantity == "cli_max" else s.hourly_max_delta_f
        inflation = s.nbm_sigma_inflation
        cal = self.calibration.get(f"{fc.station}:{quantity}")
        if cal:
            offset, inflation = float(cal["offset_f"]), float(cal["inflation"])
        nbm = from_percentiles(fc.nbm, offset_f=offset, inflation=inflation)
        return Distributions(nbm, om)

    @property
    def nbm_cycle(self) -> dt.datetime | None:
        return self._nbp_cycle
