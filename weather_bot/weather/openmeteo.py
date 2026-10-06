"""Open-Meteo Ensemble API: individual members' daily max temperature at a station point."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

import httpx

FREE_HOST = "https://ensemble-api.open-meteo.com/v1/ensemble"
PAID_HOST = "https://customer-ensemble-api.open-meteo.com/v1/ensemble"
DEFAULT_MODELS = ("gfs_seamless", "ecmwf_ifs025", "icon_seamless")
_MEMBER_RE = re.compile(r"^temperature_2m_max(?P<rest>.*)$")
_MEMBER_TAG = re.compile(r"_?member\d+_?")


@dataclass(frozen=True)
class EnsembleDay:
    station: str
    target_date: dt.date
    fetched_at: dt.datetime
    members: dict[str, list[float]]  # model -> member values in `unit`, control included
    unit: str = "F"

    def all_members(self) -> list[float]:
        return [v for vals in self.members.values() for v in vals]

    def model_means(self) -> dict[str, float]:
        return {m: sum(v) / len(v) for m, v in self.members.items() if v}

    def model_spread(self) -> float:
        means = list(self.model_means().values())
        return (max(means) - min(means)) if len(means) >= 2 else 0.0

    def as_dict(self) -> dict:
        return {"station": self.station, "target_date": self.target_date.isoformat(), "unit": self.unit,
                "fetched_at": self.fetched_at.isoformat(), "members": self.members}


async def fetch_ensemble(client: httpx.AsyncClient, station: str, lat: float, lon: float, tz: str,
                         api_key: str | None = None, models=DEFAULT_MODELS, forecast_days: int = 4,
                         unit: str = "F") -> list[EnsembleDay]:
    params = {"latitude": lat, "longitude": lon, "daily": "temperature_2m_max",
              "temperature_unit": "celsius" if unit == "C" else "fahrenheit",
              "timezone": tz, "forecast_days": forecast_days, "models": ",".join(models)}
    host = FREE_HOST
    if api_key:
        host = PAID_HOST
        params["apikey"] = api_key
    r = await client.get(host, params=params)
    r.raise_for_status()
    return parse_ensemble(station, r.json(), unit)


def parse_ensemble(station: str, payload: dict, unit: str = "F") -> list[EnsembleDay]:
    daily = payload.get("daily") or {}
    dates = [dt.date.fromisoformat(d) for d in daily.get("time", [])]
    fetched = dt.datetime.now(dt.timezone.utc)
    per_day: list[dict[str, list[float]]] = [dict() for _ in dates]
    for key, series in daily.items():
        m = _MEMBER_RE.match(key)
        if not m or key == "time":
            continue
        model = _MEMBER_TAG.sub("_", m["rest"]).strip("_") or "default"
        for idx, v in enumerate(series):
            if v is not None and idx < len(per_day):
                per_day[idx].setdefault(model, []).append(float(v))
    return [EnsembleDay(station, d, fetched, members, unit) for d, members in zip(dates, per_day) if members]
