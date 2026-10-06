"""One trading cycle, shared by `scan --once` and `run`."""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass

from .config import Settings
from .execution.quoter import Quoter
from .execution.risk import KillSwitch, allows, snapshot
from .markets.model import WeatherMarket
from .storage.db import Database
from .strategy.edge import price_legs
from .strategy.scanner import Opportunity, evaluate_market
from .venues.base import Venue
from .weather.service import ForecastService


@dataclass
class App:
    settings: Settings
    db: Database
    venues: dict[str, Venue]
    svc: ForecastService
    quoter: Quoter
    kill: KillSwitch
    dry: bool
    venue_disabled_until: dict[str, dt.datetime] = None  # type: ignore[assignment]
    venue_abort_streak: dict[str, int] = None  # type: ignore[assignment]
    _markets: list[tuple[WeatherMarket, Venue]] | None = None
    _markets_at: dt.datetime | None = None

    @classmethod
    def build(cls, settings: Settings, *, dry: bool) -> "App":
        from .venues.kalshi import KalshiVenue
        from .venues.polymarket import PolymarketVenue

        db = Database(settings.db_path)
        venues: dict[str, Venue] = {"kalshi": KalshiVenue(settings), "polymarket": PolymarketVenue(settings)}
        live = settings.live_trading and not dry
        return cls(settings, db, venues, ForecastService(settings, db), Quoter(venues, db, settings, live=live),
                   KillSwitch(settings, db), dry)

    async def discover(self, now: dt.datetime, max_age_min: int = 15) -> list[tuple[WeatherMarket, Venue]]:
        if self._markets is not None and self._markets_at and now - self._markets_at < dt.timedelta(minutes=max_age_min):
            return self._markets
        results = await asyncio.gather(*(v.discover() for v in self.venues.values()), return_exceptions=True)
        markets: list[tuple[WeatherMarket, Venue]] = []
        for venue, res in zip(self.venues.values(), results):
            if isinstance(res, Exception):
                self.kill.record_error(f"discover:{venue.name}", res)
                continue
            for m in res:
                self.db.add_market_snapshot(m, m.hours_to_target(now))
                markets.append((m, venue))
        if markets:
            self.kill.record_ok()
        self._markets, self._markets_at = markets, now
        return markets

    async def cycle(self, now: dt.datetime | None = None) -> list[Opportunity]:
        from .markets.stations import STATIONS

        now = now or dt.datetime.now(dt.timezone.utc)
        markets = await self.discover(now)
        stations = {m.station_icao: STATIONS[m.station_icao] for m, _ in markets if m.station_icao in STATIONS}
        try:
            await self.svc.refresh_all(list(stations.values()))
        except Exception as exc:
            self.kill.record_error("forecasts", exc)
        opps: list[Opportunity] = []
        fair_bids: dict[str, dict[str, float]] = {}
        for m, venue in sorted(markets, key=lambda mv: (mv[0].target_date, mv[0].venue, mv[0].station_icao)):
            opp = await evaluate_market(m, venue, self.svc, self.db, self.settings, now)
            opps.append(opp)
            if opp.dists and opp.dists.nbm and m.rules.confident:
                probs = opp.dists.nbm.bin_probabilities(m.bins)
                legs = price_legs(m.sorted_bins(), probs, {}, min_net_edge=self.settings.min_net_edge, tick=venue.tick)
                fair_bids[m.key] = {l.bin.instrument_id: l.bid_price for l in legs}
        await self.quoter.refresh(now, fair_bids)
        await self._place_new(opps, now)
        return opps

    async def _place_new(self, opps: list[Opportunity], now: dt.datetime) -> None:
        tripped = self.kill.is_tripped()
        quotable = [o for o in opps if o.decision == "quote" and o.best is not None]
        if tripped:
            for o in quotable:
                self.db.add_event("info", "skip", f"{o.market.key}: kill switch ({tripped})")
            return
        quotable.sort(key=lambda o: -(o.best.prob * o.best.net_edge_maker))
        self.venue_disabled_until = self.venue_disabled_until or {}
        self.venue_abort_streak = self.venue_abort_streak or {}
        for o in quotable:
            until = self.venue_disabled_until.get(o.market.venue)
            if until and now < until:
                self.db.add_event("info", "skip", f"{o.market.key}: venue {o.market.venue} disabled until {until:%H:%MZ}")
                continue
            snap = snapshot(self.db, now)
            why = allows(o.best.cost, o.market.key, o.market.venue, snap, self.settings, now)
            if why:
                self.db.add_event("info", "skip", f"{o.market.key}: {why}")
                continue
            basket_id = await self.quoter.place_basket(o.venue, o.market.key, o.best, o.opportunity_id, now)
            if basket_id is not None:
                row = self.db.conn.execute("SELECT status FROM baskets WHERE id=?", (basket_id,)).fetchone()
                venue = o.market.venue
                if row and row["status"] == "aborted":
                    streak = self.venue_abort_streak.get(venue, 0) + 1
                    self.venue_abort_streak[venue] = streak
                    if streak >= 3:
                        self.venue_disabled_until[venue] = now + dt.timedelta(hours=6)
                        self.db.add_event("critical", "venue", f"{venue}: {streak} consecutive aborted baskets; "
                                                                f"no new quotes on this venue for 6h")
                else:
                    self.venue_abort_streak[venue] = 0

    def close(self) -> None:
        self.db.close()
