"""One evaluation pass: markets × forecasts × books → ranked opportunities, all logged."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from dataclasses import dataclass

from ..config import Settings
from ..markets.model import WeatherMarket
from ..markets.stations import STATIONS
from ..monitoring.console import market_block
from ..storage.db import Database
from ..venues.base import OrderBook, Venue
from ..weather.service import Distributions, ForecastService, StationForecast
from .baskets import contiguous_ranges
from .edge import BasketEval, evaluate
from .filters import basket_gate, forecast_gate, market_gate, market_gate_vs_model, market_implied_mean
from .sizing import basket_qty


@dataclass(frozen=True)
class Opportunity:
    market: WeatherMarket
    venue: Venue
    forecast: StationForecast | None
    dists: Distributions | None
    best: BasketEval | None
    decision: str  # quote | skip
    reason: str | None
    opportunity_id: int | None
    hours: float

    def console(self) -> str:
        return market_block(self.market, self.forecast, self.dists, self.best, self.decision, self.reason, self.hours)


async def fetch_books(venue: Venue, market: WeatherMarket, db: Database | None) -> dict[str, OrderBook]:
    sem = asyncio.Semaphore(6)

    async def one(instrument_id: str):
        async with sem:
            try:
                book = await venue.orderbook(instrument_id)
            except Exception as exc:  # network/venue error: leave the leg without a book
                if db:
                    db.add_event("warn", "book", f"{venue.name} {instrument_id}: {exc!r}")
                return instrument_id, None
            if db:
                db.add_book(venue.name, book)
            return instrument_id, book

    pairs = await asyncio.gather(*(one(b.instrument_id) for b in market.bins))
    return {k: v for k, v in pairs if v is not None}


def rank_key(ev: BasketEval) -> float:
    """Prefer high-probability ranges whose bids sit close to executable prices."""
    gap = ev.gap if ev.gap is not None else 1.0
    return ev.prob * ev.net_edge_maker / (1.0 + gap)


async def evaluate_market(market: WeatherMarket, venue: Venue, svc: ForecastService, db: Database | None,
                          settings: Settings, now: dt.datetime) -> Opportunity:
    hours = market.hours_to_target(now)
    reason = market_gate(market, now, settings)
    fc = dists = None
    best = None
    if reason is None:
        fc = svc.forecast(market.station_icao, market.target_date)
        dists = svc.distributions(fc, market.quantity, market.unit)
        reason = forecast_gate(fc, dists, now, settings, market.unit)
        if reason is None and dists.primary is not None:
            reason = market_gate_vs_model(market, dists.primary.mean, settings)
    if reason is None and dists and dists.primary:
        books = await fetch_books(venue, market, db)
        probs = dists.primary.bin_probabilities(market.bins)
        candidates: list[BasketEval] = []
        rejected: list[str] = []
        for rng in contiguous_ranges(market.sorted_bins(), settings.min_bins, settings.max_bins):
            trial = evaluate(rng, probs, books, venue, min_net_edge=settings.min_net_edge, qty=1.0)
            qty = basket_qty(venue, [l.bid_price for l in trial.legs], settings.max_basket_cost(venue.name))
            if qty is None:
                rejected.append(f"{trial.label}: size")
                continue
            ev = evaluate(rng, probs, books, venue, min_net_edge=settings.min_net_edge, qty=qty)
            why = basket_gate(ev, settings)
            if why:
                rejected.append(f"{ev.label}: {why}")
                continue
            candidates.append(ev)
        if candidates:
            best = max(candidates, key=rank_key)
        else:
            reason = "no basket passes: " + "; ".join(rejected[:4]) + (" …" if len(rejected) > 4 else "")
    decision = "quote" if best is not None and reason is None else "skip"
    opp_id = None
    if db:
        opp_id = _log(db, market, fc, dists, best, decision, reason, hours)
    return Opportunity(market, venue, fc, dists, best, decision, reason, opp_id, hours)


def _log(db: Database, m: WeatherMarket, fc, dists, best: BasketEval | None, decision: str, reason: str | None,
         hours: float) -> int:
    row = {
        "venue": m.venue, "market_key": m.key, "event_id": m.event_id, "station": m.station_icao,
        "target_date": m.target_date.isoformat(), "quantity": m.quantity, "hours_to_target": round(hours, 2),
        "bins": json.dumps([{"label": b.label, "bid": b.yes_bid, "ask": b.yes_ask} for b in m.sorted_bins()]),
        "prob_nbm": None, "prob_openmeteo": None, "decision": decision, "reason": reason,
        "dist_nbm": json.dumps(dists.nbm.as_dict()) if dists and dists.nbm else None,
        "dist_openmeteo": json.dumps(dists.openmeteo.as_dict()) if dists and dists.openmeteo else None,
        "nws_max": fc.nws_max if fc else None,
        "confidence": json.dumps({"nbm_sd": fc.nbm.sd if fc.nbm else None,
                                  "nbm_cycle": fc.nbm.cycle.isoformat() if fc.nbm else None,
                                  "model_spread": fc.ensemble.model_spread() if fc.ensemble else None,
                                  "market_mean": market_implied_mean(m), "unit": m.unit,
                                  "model_mean": round(dists.primary.mean, 2) if dists and dists.primary else None})
        if fc else None,
    }
    if best:
        row.update(best.as_row())
        lo, hi = best.legs[0].bin.lo, best.legs[-1].bin.hi
        row["prob_nbm"] = round(dists.nbm.prob_range(lo, hi), 4) if dists and dists.nbm else None
        row["prob_openmeteo"] = round(dists.openmeteo.prob_range(lo, hi), 4) if dists and dists.openmeteo else None
    return db.add_opportunity(row)


async def scan_all(venues: list[Venue], svc: ForecastService, db: Database | None, settings: Settings,
                   now: dt.datetime | None = None) -> list[Opportunity]:
    now = now or dt.datetime.now(dt.timezone.utc)
    discovered = await asyncio.gather(*(v.discover() for v in venues), return_exceptions=True)
    markets: list[tuple[WeatherMarket, Venue]] = []
    for venue, res in zip(venues, discovered):
        if isinstance(res, Exception):
            if db:
                db.add_event("error", "discover", f"{venue.name}: {res!r}")
            continue
        for m in res:
            if db:
                db.add_market_snapshot(m, m.hours_to_target(now))
            markets.append((m, venue))
    stations = [STATIONS[m.station_icao] for m, _ in markets if m.station_icao in STATIONS]
    await svc.refresh_all(list({s.icao: s for s in stations}.values()))
    opps = []
    for m, venue in sorted(markets, key=lambda mv: (mv[0].target_date, mv[0].venue, mv[0].station_icao)):
        opps.append(await evaluate_market(m, venue, svc, db, settings, now))
    return opps
