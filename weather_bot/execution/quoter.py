"""Resting-bid execution: post one GTC post-only bid per leg, reconcile fills, re-quote or abandon.

Basket statuses: resting -> (filled legs) partial -> complete
                 resting --TTL--> expired            (no fills; scanner may re-quote next cycle)
                 partial --timeout--> completing (lift remaining asks if still +EV) -> complete
                                   -> unwinding (sell filled legs) -> unwound | partial (held)
                 any -> aborted (a leg was rejected at placement)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from dataclasses import dataclass

from ..config import Settings
from ..storage.db import Database
from ..strategy.edge import BasketEval, Leg
from ..venues.base import OrderResult, Venue
from .risk import legs_json


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class LegState:
    order_row_id: int
    instrument_id: str
    label: str
    prob: float
    venue_order_id: str
    bid: float
    qty: float
    status: str
    filled_qty: float
    avg_fill_price: float | None


class Quoter:
    def __init__(self, venues: dict[str, Venue], db: Database, settings: Settings, *, live: bool):
        self.venues = venues
        self.db = db
        self.settings = settings
        self.live = live

    # ---------- placement ----------
    async def place_basket(self, venue: Venue, market_key: str, ev: BasketEval, opportunity_id: int | None,
                           now: dt.datetime | None = None) -> int | None:
        """Post all legs concurrently. Returns basket id, or None when not live / rejected."""
        created = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
        if not self.live:
            self.db.add_event("info", "dry-quote", f"{venue.name} {market_key} {ev.label} qty {ev.qty:g} "
                                                   f"Σbid {ev.sum_bids:.3f} edge {ev.net_edge_maker:.3f}")
            return None
        cur = self.db.conn.execute(
            "INSERT INTO baskets(created_ts, venue, market_key, opportunity_id, status, legs, intended_cost, filled_cost,"
            " qty, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (created, venue.name, market_key, opportunity_id, "placing", legs_json(ev.legs), ev.cost, 0.0, ev.qty,
             created))
        basket_id = int(cur.lastrowid)
        results = await asyncio.gather(*(self._place_leg(venue, basket_id, leg, ev.qty) for leg in ev.legs),
                                       return_exceptions=True)
        rejected = [r for r in results if isinstance(r, BaseException) or r.status in ("rejected", "unknown")]
        if rejected:
            await self._cancel_open_legs(venue, basket_id)
            self._set_status(basket_id, "aborted", notes=f"{len(rejected)} leg(s) rejected")
            self.db.add_event("warn", "basket", f"basket {basket_id} aborted: {rejected[0]!r}")
            return basket_id
        self._set_status(basket_id, "resting")
        self.db.add_event("info", "basket", f"basket {basket_id} resting on {venue.name} {market_key} {ev.label}",
                          {"qty": ev.qty, "sum_bids": ev.sum_bids, "edge": ev.net_edge_maker})
        return basket_id

    async def _place_leg(self, venue: Venue, basket_id: int, leg: Leg, qty: float, *, side: str = "buy",
                         price: float | None = None, ioc: bool = False) -> OrderResult:
        price = leg.bid_price if price is None else price
        client_id = str(uuid.uuid4())
        cur = self.db.conn.execute(
            "INSERT INTO orders(ts, venue, basket_id, instrument_id, client_id, venue_order_id, side, price, qty,"
            " post_only, status, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (_now_iso(), venue.name, basket_id, leg.bin.instrument_id, client_id, None, side, price, qty,
             int(not ioc), "sending", _now_iso()))
        row_id = int(cur.lastrowid)
        res = await venue.place_limit(leg.bin.instrument_id, price, qty, side=side, post_only=not ioc, ioc=ioc,
                                      client_id=client_id)
        self._update_order(row_id, res)
        return res

    # ---------- reconciliation ----------
    async def refresh(self, now: dt.datetime, fair_bids: dict[str, dict[str, float]] | None = None) -> None:
        """fair_bids: market_key -> {instrument_id: current fair-minus-edge bid}. Used to re-quote on model moves."""
        rows = self.db.conn.execute(
            "SELECT * FROM baskets WHERE status IN ('resting','partial','completing','unwinding')").fetchall()
        for b in rows:
            venue = self.venues.get(b["venue"])
            if venue is None:
                continue
            try:
                await self._refresh_basket(venue, b, now, (fair_bids or {}).get(b["market_key"]))
            except Exception as exc:
                self.db.add_event("error", "refresh", f"basket {b['id']}: {exc!r}")

    async def _refresh_basket(self, venue: Venue, b, now: dt.datetime, fair: dict[str, float] | None) -> None:
        legs = await self._sync_legs(venue, b["id"])
        buys = [l for l in legs if l.status != "cancelled"]
        filled_cost = sum(l.filled_qty * (l.avg_fill_price or l.bid) for l in self._buy_legs(b["id"]))
        n_full = sum(1 for l in buys if l.status == "filled")
        n_any = sum(1 for l in buys if l.filled_qty > 0)
        age_min = (now - dt.datetime.fromisoformat(b["created_ts"])).total_seconds() / 60
        status = b["status"]
        if status in ("resting", "partial"):
            if buys and n_full == len(buys):
                self._set_status(b["id"], "complete", filled_cost=filled_cost)
                self.db.add_event("info", "basket", f"basket {b['id']} complete, cost ${filled_cost:.2f}")
                return
            if n_any == 0:
                moved = fair is not None and any(l.bid > fair.get(l.instrument_id, l.bid) + venue.tick / 2
                                                 for l in buys if l.status == "resting")
                if age_min >= self.settings.bid_ttl_min or moved:
                    await self._cancel_open_legs(venue, b["id"])
                    self._set_status(b["id"], "expired", notes="ttl" if not moved else "model moved")
                return
            self._set_status(b["id"], "partial", filled_cost=filled_cost)
            if age_min >= self.settings.basket_complete_timeout_min:
                await self._resolve_partial(venue, b, legs, filled_cost)
        elif status == "unwinding":
            sells = [l for l in legs if l.status != "cancelled" and self._order_side(l.order_row_id) == "sell"]
            if sells and all(l.status == "filled" for l in sells):
                self._set_status(b["id"], "unwound", filled_cost=filled_cost)
            elif age_min >= self.settings.basket_complete_timeout_min + self.settings.unwind_ttl_min:
                await self._cancel_open_legs(venue, b["id"])
                self._set_status(b["id"], "partial", notes="held after unwind attempt")

    async def _resolve_partial(self, venue: Venue, b, legs: list[LegState], filled_cost: float) -> None:
        """After the completion timeout: lift remaining asks if still +EV, else try to unwind, else hold."""
        remaining = [l for l in legs if l.status in ("resting", "partial")]
        books = {}
        for l in remaining:
            try:
                books[l.instrument_id] = await venue.orderbook(l.instrument_id)
            except Exception:
                pass
        qty = float(b["qty"])
        prob = sum(l.prob for l in legs if l.status != "cancelled")
        asks = [books[l.instrument_id].best_ask for l in remaining if l.instrument_id in books]
        if remaining and len(asks) == len(remaining) and all(a is not None for a in asks):
            cost_complete = filled_cost + sum(a * (qty - l.filled_qty) for a, l in zip(asks, remaining))
            fees = sum(venue.taker_fee(a, qty - l.filled_qty) for a, l in zip(asks, remaining))
            net = prob - cost_complete / qty - fees / qty
            if net >= self.settings.min_net_edge:
                await self._cancel_open_legs(venue, b["id"])
                for a, l in zip(asks, remaining):
                    leg = Leg(_bin_stub(l), l.prob, None, a, None, a)
                    await self._place_leg(venue, b["id"], leg, qty - l.filled_qty, price=a, ioc=True)
                self._set_status(b["id"], "completing", notes=f"lifted remaining at net {net:.3f}")
                return
        # Unwind filled legs with a resting ask one tick above cost.
        await self._cancel_open_legs(venue, b["id"])
        filled = [l for l in legs if l.filled_qty > 0]
        for l in filled:
            paid = l.avg_fill_price or l.bid
            leg = Leg(_bin_stub(l), l.prob, None, None, None, paid)
            await self._place_leg(venue, b["id"], leg, l.filled_qty, side="sell", price=round(paid + venue.tick, 4))
        self._set_status(b["id"], "unwinding", notes="remaining legs not +EV; unwinding fills")

    # ---------- helpers ----------
    async def _sync_legs(self, venue: Venue, basket_id: int) -> list[LegState]:
        rows = self.db.conn.execute("SELECT * FROM orders WHERE basket_id=? ORDER BY id", (basket_id,)).fetchall()
        legs_meta = {l["instrument_id"]: l for l in json.loads(
            self.db.conn.execute("SELECT legs FROM baskets WHERE id=?", (basket_id,)).fetchone()["legs"])}
        out: list[LegState] = []
        for r in rows:
            status, filled, avg = r["status"], float(r["filled_qty"] or 0), r["avg_fill_price"]
            if status in ("resting", "partial", "sending") and r["venue_order_id"]:
                res = await venue.order_status(r["venue_order_id"])
                if res.status != "unknown":
                    self._update_order(r["id"], res)
                    status, filled, avg = res.status, res.filled_qty, res.avg_fill_price
            meta = legs_meta.get(r["instrument_id"], {})
            out.append(LegState(r["id"], r["instrument_id"], meta.get("label", "?"), float(meta.get("prob", 0.0)),
                                r["venue_order_id"] or "", float(r["price"]), float(r["qty"]), status, filled, avg))
        return out

    def _buy_legs(self, basket_id: int) -> list[LegState]:
        rows = self.db.conn.execute("SELECT * FROM orders WHERE basket_id=? AND side='buy'", (basket_id,)).fetchall()
        return [LegState(r["id"], r["instrument_id"], "", 0.0, r["venue_order_id"] or "", float(r["price"]),
                         float(r["qty"]), r["status"], float(r["filled_qty"] or 0), r["avg_fill_price"]) for r in rows]

    def _order_side(self, order_row_id: int) -> str:
        return self.db.conn.execute("SELECT side FROM orders WHERE id=?", (order_row_id,)).fetchone()["side"]

    async def _cancel_open_legs(self, venue: Venue, basket_id: int) -> None:
        rows = self.db.conn.execute("SELECT id, venue_order_id FROM orders WHERE basket_id=? AND status IN "
                                    "('resting','partial','sending') AND venue_order_id IS NOT NULL", (basket_id,)).fetchall()
        for r in rows:
            try:
                await venue.cancel(r["venue_order_id"])
                res = await venue.order_status(r["venue_order_id"])
                if res.status == "unknown":
                    res = OrderResult(r["venue_order_id"], "cancelled", 0.0, None, {})
                self._update_order(r["id"], res)
            except Exception as exc:
                self.db.add_event("error", "cancel", f"order {r['venue_order_id']}: {exc!r}")

    def _update_order(self, row_id: int, res: OrderResult) -> None:
        self.db.conn.execute(
            "UPDATE orders SET venue_order_id=COALESCE(NULLIF(?,''), venue_order_id), status=?, filled_qty=?, "
            "avg_fill_price=?, raw=?, updated_ts=? WHERE id=?",
            (res.order_id, res.status, res.filled_qty, res.avg_fill_price, json.dumps(res.raw, default=str),
             _now_iso(), row_id))

    def _set_status(self, basket_id: int, status: str, *, filled_cost: float | None = None, notes: str | None = None) -> None:
        self.db.conn.execute(
            "UPDATE baskets SET status=?, filled_cost=COALESCE(?, filled_cost), notes=COALESCE(?, notes), updated_ts=? "
            "WHERE id=?", (status, filled_cost, notes, _now_iso(), basket_id))


def _bin_stub(l: LegState):
    from ..markets.model import Bin

    return Bin(l.label, None, None, l.instrument_id)
