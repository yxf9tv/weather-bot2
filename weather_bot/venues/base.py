"""Common venue interface. Strategy code depends only on this."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal, Protocol

from ..markets.model import WeatherMarket

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class OrderBook:
    instrument_id: str
    bids: tuple[BookLevel, ...]  # YES bids, best first
    asks: tuple[BookLevel, ...]  # YES asks, best first
    fetched_at: dt.datetime

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    status: str  # resting | filled | partial | cancelled | rejected | unknown
    filled_qty: float
    avg_fill_price: float | None
    raw: dict

    @property
    def is_open(self) -> bool:
        return self.status in ("resting", "partial")


class Venue(Protocol):
    name: str
    tick: float

    async def discover(self) -> list[WeatherMarket]: ...
    async def orderbook(self, instrument_id: str) -> OrderBook: ...
    def taker_fee(self, price: float, qty: float) -> float: ...
    def maker_fee(self, price: float, qty: float) -> float: ...
    def min_qty(self, price: float) -> float: ...
    async def place_limit(self, instrument_id: str, price: float, qty: float, *, side: Side = "buy",
                          post_only: bool = True, ioc: bool = False, client_id: str) -> OrderResult: ...
    async def cancel(self, order_id: str) -> None: ...
    async def order_status(self, order_id: str) -> OrderResult: ...
    async def positions(self) -> list[dict]: ...
    async def balance(self) -> float: ...
