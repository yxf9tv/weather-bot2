"""One trading cycle, shared by `scan --once` and `run`."""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass

from .config import Settings
from .execution.quoter import Quoter
from .execution.risk import KillSwitch, allows, snapshot
from .execution.shadow import ShadowBids
from .strategy.filters import live_gate, taker_cost_per_unit
from .markets.model import WeatherMarket
from .storage.db import Database
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
    score_hour_utc: int = 14  # 10am ET: Weather Company 'official' values and West-coast hourly obs are all in
    venue_disabled_until: dict[str, dt.datetime] = None  # type: ignore[assignment]
    _last_skip: dict[str, str] = None  # type: ignore[assignment]
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
            if opp.dists and opp.dists.primary and m.rules.confident:
                # Model fair value per leg (uncapped). A resting bid above this is no longer +EV -> re-quote.
                probs = opp.dists.primary.bin_probabilities(m.bins)
                fair_bids[m.key] = {b.instrument_id: probs[b.label] for b in m.bins}
        blocked = {o.market.key for o in opps if o.decision == "skip" and o.reason and "disagree" in o.reason}
        await self.quoter.refresh(now, fair_bids, blocked)
        try:
            ShadowBids(self.db, self.settings).update(opps, now)
        except Exception as exc:  # paper bookkeeping must never stop trading
            self.db.add_event("error", "shadow", repr(exc))
        await self._place_new(opps, now)
        await self.maybe_score(now)
        return opps

    def score_due(self, now: dt.datetime) -> bool:
        """Once per UTC day, at or after score_hour_utc. Also catches up if the loop was down at that hour."""
        last = self.db.get("last_score_date")
        return now.hour >= self.score_hour_utc and last != now.date().isoformat()

    async def maybe_score(self, now: dt.datetime) -> None:
        if not self.score_due(now):
            return
        import httpx

        from .scoring.settle import calibration_report, score_pending

        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                n = await score_pending(self.db, client, now)
        except Exception as exc:
            self.db.add_event("error", "score", f"daily scoring failed: {exc!r}")
            return
        self.db.set("last_score_date", now.date().isoformat())
        report = calibration_report(self.db)
        self.db.add_event("info", "score", f"daily scoring: {n} market(s) settled", {"report": report})
        print(f"[score] {n} market(s) settled\n{report}")

    async def _place_new(self, opps: list[Opportunity], now: dt.datetime) -> None:
        tripped = self.kill.is_tripped()
        self._last_skip = self._last_skip or {}
        taker_mode = self.settings.execution_mode == "taker"
        if taker_mode:
            quotable = []
            for o in opps:
                if o.best_taker is None:
                    continue
                why = live_gate(o.hours, o.market_gap, o.market.unit, self.settings)
                if why:
                    if self._last_skip.get(o.market.key) != why.split(" ")[0] + why.split(" ")[-1]:
                        self.db.add_event("info", "skip", f"{o.market.key}: {why}")
                        self._last_skip[o.market.key] = why.split(" ")[0] + why.split(" ")[-1]
                    continue
                quotable.append(o)
        else:
            quotable = [o for o in opps if o.decision == "quote" and o.best is not None]
        if tripped:
            for o in quotable:
                self.db.add_event("info", "skip", f"{o.market.key}: kill switch ({tripped})")
            return
        def ev_of(o):
            return o.best_taker if taker_mode else o.best

        def cost_of(o):
            ev = ev_of(o)
            return ev.qty * (taker_cost_per_unit(ev) or 0.0) if taker_mode else ev.cost

        quotable.sort(key=lambda o: -(ev_of(o).prob * ((ev_of(o).net_edge_taker or 0.0) if taker_mode
                                                        else ev_of(o).net_edge_maker)))
        # Free balance per venue, read once per cycle; a basket must fit in what is actually available.
        free: dict[str, float] = {}
        for name, venue in self.venues.items():
            try:
                free[name] = await venue.balance()
            except Exception as exc:
                self.db.add_event("warn", "balance", f"{name}: {exc!r}")
        self.venue_disabled_until = self.venue_disabled_until or {}
        self.venue_abort_streak = self.venue_abort_streak or {}
        for o in quotable:
            until = self.venue_disabled_until.get(o.market.venue)
            if until and now < until:
                self.db.add_event("info", "skip", f"{o.market.key}: venue {o.market.venue} disabled until {until:%H:%MZ}")
                continue
            snap = snapshot(self.db, now)
            cost = cost_of(o)
            why = allows(cost, o.market.key, o.market.venue, snap, self.settings, now)
            bal = free.get(o.market.venue)
            if why is None and bal is not None and cost > bal - self.settings.balance_buffer:
                why = f"insufficient free balance (${bal:.2f} on {o.market.venue})"
            if why is None and bal is not None:
                free[o.market.venue] = bal - cost
            if why:
                short = why.split(" (")[0]
                if self._last_skip.get(o.market.key) != short:  # one event per change of reason, not per minute
                    self.db.add_event("info", "skip", f"{o.market.key}: {why}")
                    self._last_skip[o.market.key] = short
                continue
            self._last_skip.pop(o.market.key, None)
            basket_id = await self.quoter.place_basket(o.venue, o.market.key, ev_of(o), o.opportunity_id, now,
                                                       taker_only=taker_mode)
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
