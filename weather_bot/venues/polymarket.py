"""Polymarket (international CLOB V2) venue: Gamma discovery, CLOB books, py-clob-client-v2 orders."""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from dataclasses import replace

import httpx

from ..config import Settings
from ..markets.model import Bin, WeatherMarket
from ..markets.rules import parse_bin_label, parse_polymarket_rules
from ..markets.stations import POLYMARKET_CITY_STATION, STATIONS
from .base import BookLevel, OrderBook, OrderResult

WEATHER_TAG_ID = 84
_SLUG_RE = re.compile(r"^highest-temperature-in-(?P<city>[a-z-]+?)-on-[a-z]+-\d{1,2}-\d{4}$")
_UA = {"User-Agent": "Mozilla/5.0 (weather-bot-2)", "Accept": "application/json"}


def polymarket_taker_fee(price: float, qty: float, rate: float = 0.05) -> float:
    return round(rate * qty * price * (1.0 - price), 5)


def polymarket_min_qty(price: float, min_shares: float = 5.0, min_notional: float = 1.0) -> float:
    """Exchange floor: >= 5 shares and >= $1 notional for a marketable BUY. Whole shares."""
    if price <= 0:
        return min_shares
    return float(max(min_shares, math.ceil(min_notional / price)))


class PolymarketVenue:
    name = "polymarket"
    tick = 0.01

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.gamma = settings.gamma_host.rstrip("/")
        self.clob = settings.clob_host.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=20.0, headers=_UA)
        self._clob_client = None

    # ---------- discovery ----------
    async def discover(self) -> list[WeatherMarket]:
        events: list[dict] = []
        for offset in range(0, 1000, 100):
            r = await self.client.get(f"{self.gamma}/events", params={
                "tag_id": WEATHER_TAG_ID, "closed": "false", "limit": 100, "offset": offset})
            r.raise_for_status()
            page = r.json()
            if not page:
                break
            events.extend(page)
            if len(page) < 100:
                break
        out = []
        for ev in events:
            wm = self.build_market(ev)
            if wm is not None:
                out.append(wm)
        return out

    def build_market(self, ev: dict) -> WeatherMarket | None:
        slug = ev.get("slug", "")
        m = _SLUG_RE.match(slug)
        if not m or m["city"] not in POLYMARKET_CITY_STATION:
            return None
        rows = ev.get("markets") or []
        if not rows:
            return None
        first = rows[0]
        rules = parse_polymarket_rules(first.get("description", ""), first.get("resolutionSource"), slug)
        problems = list(rules.problems)
        expected = POLYMARKET_CITY_STATION[m["city"]]
        if rules.station_icao and rules.station_icao != expected:
            problems.append(f"city {m['city']} expected {expected}, rules say {rules.station_icao}")
        bins: list[Bin] = []
        for row in rows:
            try:
                tokens = json.loads(row.get("clobTokenIds") or "[]")
            except ValueError:
                tokens = []
            if not tokens:
                problems.append(f"no token ids for {row.get('groupItemTitle')}")
                continue
            b = parse_bin_label(row.get("groupItemTitle") or "", tokens[0])
            if b is None:
                problems.append(f"unparsed bin label {row.get('groupItemTitle')!r}")
                continue
            bins.append(b.with_quote(yes_bid=_f(row.get("bestBid")), yes_ask=_f(row.get("bestAsk"))))
        rules = replace(rules, confident=rules.confident and not problems, problems=tuple(problems))
        st = STATIONS.get(rules.station_icao or "")
        wm = WeatherMarket(
            venue="polymarket", event_id=slug, city=st.name if st else m["city"],
            station_icao=rules.station_icao or "?", target_date=rules.target_date or dt.date(1970, 1, 1),
            timezone=st.tz if st else "UTC", quantity="hourly_max", unit="F", rules=rules, bins=tuple(bins),
            opens_at=_ts(ev.get("startDate")), closes_at=_ts(ev.get("endDate")),
            raw_rules_text=first.get("description", ""),
            extra={"neg_risk": bool(ev.get("negRisk")), "condition_ids": [r.get("conditionId") for r in rows],
                   "min_size": _f(first.get("orderMinSize")) or 5.0,
                   "tick": _f(first.get("orderPriceMinTickSize")) or 0.01,
                   "fee_schedule": first.get("feeSchedule")},
        )
        if not wm.bins_are_contiguous():
            wm = replace(wm, rules=replace(wm.rules, confident=False,
                                           problems=wm.rules.problems + ("bins not contiguous/exhaustive",)))
        return wm

    # ---------- books ----------
    async def orderbook(self, instrument_id: str) -> OrderBook:
        r = await self.client.get(f"{self.clob}/book", params={"token_id": instrument_id})
        r.raise_for_status()
        d = r.json()
        bids = sorted(((float(x["price"]), float(x["size"])) for x in d.get("bids", [])), key=lambda x: -x[0])
        asks = sorted(((float(x["price"]), float(x["size"])) for x in d.get("asks", [])), key=lambda x: x[0])
        return OrderBook(instrument_id, tuple(BookLevel(p, s) for p, s in bids),
                         tuple(BookLevel(p, s) for p, s in asks), dt.datetime.now(dt.timezone.utc))

    # ---------- fees / sizing ----------
    def taker_fee(self, price: float, qty: float) -> float:
        return polymarket_taker_fee(price, qty)

    def maker_fee(self, price: float, qty: float) -> float:
        return 0.0

    def min_qty(self, price: float) -> float:
        return polymarket_min_qty(price)

    # ---------- trading ----------
    def _sdk(self):
        if self._clob_client is None:
            from py_clob_client_v2.client import ClobClient

            if not (self.settings.poly_pk and self.settings.poly_funder):
                raise RuntimeError("POLY_PK / POLY_FUNDER not set")
            c = ClobClient(self.clob, chain_id=137, key=self.settings.poly_pk,
                           signature_type=self.settings.poly_sig_type, funder=self.settings.poly_funder)
            c.set_api_creds(c.create_or_derive_api_key())
            self._clob_client = c
        return self._clob_client

    async def place_limit(self, instrument_id: str, price: float, qty: float, *, side: str = "buy",
                          post_only: bool = True, ioc: bool = False, client_id: str) -> OrderResult:
        import asyncio

        from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import BUY, SELL

        def _do():
            c = self._sdk()
            tick = c.get_tick_size(instrument_id)
            signed = c.create_order(OrderArgs(token_id=instrument_id, price=price, size=qty,
                                              side=BUY if side == "buy" else SELL),
                                    PartialCreateOrderOptions(tick_size=tick, neg_risk=True))
            if ioc:
                return c.post_order(signed, OrderType.FAK)
            return c.post_order(signed, OrderType.GTC, post_only=post_only) if post_only \
                else c.post_order(signed, OrderType.GTC)

        try:
            resp = await asyncio.to_thread(_do)
        except Exception as exc:  # SDK raises on HTTP errors
            return OrderResult("", "rejected", 0.0, None, {"error": str(exc)})
        oid = str(resp.get("orderID") or resp.get("orderId") or "")
        status = (resp.get("status") or "live").lower()
        st = {"matched": "filled", "live": "resting", "delayed": "resting"}.get(status, status)
        filled = float(resp.get("takingAmount") or 0) if st == "filled" else 0.0
        return OrderResult(oid, st, filled, None, resp)

    async def cancel(self, order_id: str) -> None:
        import asyncio

        await asyncio.to_thread(lambda: self._sdk().cancel(order_id))

    async def order_status(self, order_id: str) -> OrderResult:
        import asyncio

        try:
            o = await asyncio.to_thread(lambda: self._sdk().get_order(order_id))
        except Exception as exc:
            return OrderResult(order_id, "unknown", 0.0, None, {"error": str(exc)})
        matched = float(o.get("size_matched") or 0)
        size = float(o.get("original_size") or 0)
        vstatus = (o.get("status") or "").upper()
        if vstatus == "CANCELED":
            st = "cancelled"
        elif vstatus == "MATCHED" or (size and matched >= size - 1e-9):
            st = "filled"
        elif matched > 0:
            st = "partial"
        else:
            st = "resting"
        return OrderResult(order_id, st, matched, _f(o.get("price")), o)

    async def positions(self) -> list[dict]:
        r = await self.client.get("https://data-api.polymarket.com/positions",
                                  params={"user": self.settings.poly_funder})
        r.raise_for_status()
        return r.json()

    async def balance(self) -> float:
        import asyncio

        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        d = await asyncio.to_thread(lambda: self._sdk().get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)))
        return float(d.get("balance", 0)) / 1e6


def _f(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
