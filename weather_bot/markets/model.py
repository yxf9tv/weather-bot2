"""Normalized market objects shared by every venue."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from typing import Literal

Quantity = Literal["cli_max", "hourly_max"]


@dataclass(frozen=True)
class Bin:
    """One temperature bracket. lo/hi are inclusive whole degrees; None = open tail."""

    label: str
    lo: int | None
    hi: int | None
    instrument_id: str  # Kalshi market ticker or Polymarket YES token id
    yes_bid: float | None = None
    yes_ask: float | None = None
    ask_size: float | None = None
    bid_size: float | None = None

    def contains(self, temp_f: int) -> bool:
        if self.lo is not None and temp_f < self.lo:
            return False
        if self.hi is not None and temp_f > self.hi:
            return False
        return True

    def with_quote(self, *, yes_bid, yes_ask, ask_size=None, bid_size=None) -> "Bin":
        return replace(self, yes_bid=yes_bid, yes_ask=yes_ask, ask_size=ask_size, bid_size=bid_size)


@dataclass(frozen=True)
class RulesParse:
    """What the venue's rules text says. confident=False means DO NOT TRADE."""

    station_icao: str | None
    station_text: str | None
    target_date: dt.date | None
    unit: str | None
    quantity: Quantity | None
    source: str | None
    confident: bool
    problems: tuple[str, ...] = ()
    hourly_filter: bool = True  # Polymarket US: only obs at :51–:59/:00–:04 count; intl: every observation


@dataclass(frozen=True)
class WeatherMarket:
    venue: str
    event_id: str
    city: str
    station_icao: str
    target_date: dt.date
    timezone: str
    quantity: Quantity
    unit: str
    rules: RulesParse
    bins: tuple[Bin, ...]
    opens_at: dt.datetime | None = None
    closes_at: dt.datetime | None = None
    raw_rules_text: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.station_icao}:{self.target_date.isoformat()}:{self.quantity}"

    def hours_to_target(self, now: dt.datetime) -> float:
        from zoneinfo import ZoneInfo

        start = dt.datetime.combine(self.target_date, dt.time(0, 0), tzinfo=ZoneInfo(self.timezone))
        return (start - now.astimezone(ZoneInfo(self.timezone))).total_seconds() / 3600.0

    def sum_of_asks(self) -> float | None:
        asks = [b.yes_ask for b in self.bins if b.yes_ask is not None]
        return sum(asks) if len(asks) == len(self.bins) and asks else None

    def with_bins(self, bins: tuple[Bin, ...]) -> "WeatherMarket":
        return replace(self, bins=bins)

    def sorted_bins(self) -> tuple[Bin, ...]:
        def sort_key(b: Bin):
            if b.lo is None:
                return (-10_000, 0)
            return (b.lo, b.hi if b.hi is not None else 10_000)

        return tuple(sorted(self.bins, key=sort_key))

    def bins_are_contiguous(self) -> bool:
        bins = self.sorted_bins()
        if len(bins) < 2 or bins[0].lo is not None or bins[-1].hi is not None:
            return False
        for a, b in zip(bins, bins[1:]):
            if a.hi is None or b.lo is None or b.lo != a.hi + 1:
                return False
        return True
