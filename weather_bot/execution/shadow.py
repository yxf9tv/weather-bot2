"""Paper version of the resting-bid strategy, kept running while live trading is taker-only.

For every market the scanner would have quoted, a shadow basket rests the same bids it would have placed. A leg
counts as filled when the best ask falls to or below its bid (a seller came down to us). Unfilled shadows re-quote
after BID_TTL_MIN like the real quoter did; once any leg fills the basket is frozen and held to settlement. This
ignores queue position, so it slightly flatters fill rates; it captures adverse selection, which is what matters.

Statuses: open -> complete (every leg filled) | closed (horizon ended, some or no fills) | expired (no fill, market no
longer quotable).
"""

from __future__ import annotations

import datetime as dt
import json

from ..config import Settings
from ..storage.db import Database


def _now_iso(now: dt.datetime) -> str:
    return now.isoformat(timespec="seconds")


def _asks(opp) -> dict[str, float | None]:
    out: dict[str, float | None] = {b.instrument_id: b.yes_ask for b in opp.market.bins}
    for iid, book in (opp.books or {}).items():
        out[iid] = book.best_ask
    return out


def _legs_from(ev) -> list[dict]:
    return [{"iid": l.bin.instrument_id, "label": l.bin.label, "bid": round(l.bid_price, 4), "prob": round(l.prob, 4),
             "filled_ts": None} for l in ev.legs]


class ShadowBids:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    def update(self, opps, now: dt.datetime) -> None:
        seen = set()
        for o in opps:
            seen.add(o.market.key)
            row = self.db.conn.execute("SELECT * FROM shadow_baskets WHERE market_key=? AND status='open'",
                                       (o.market.key,)).fetchone()
            if row is None:
                if o.decision == "quote" and o.best is not None:
                    self._open(o, now)
                continue
            self._advance(row, o, now)
        for row in self.db.conn.execute("SELECT id, market_key FROM shadow_baskets WHERE status='open'").fetchall():
            if row["market_key"] not in seen:  # market closed / delisted
                self._set(row["id"], "closed", now, notes="market no longer listed")

    def _open(self, o, now: dt.datetime) -> None:
        self.db.conn.execute(
            "INSERT INTO shadow_baskets(created_ts, updated_ts, venue, market_key, label, prob, qty, legs, status)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (_now_iso(now), _now_iso(now), o.market.venue, o.market.key, o.best.label, round(o.best.prob, 4),
             o.best.qty, json.dumps(_legs_from(o.best)), "open"))

    def _advance(self, row, o, now: dt.datetime) -> None:
        legs = json.loads(row["legs"])
        asks = _asks(o)
        for leg in legs:
            ask = asks.get(leg["iid"])
            if leg["filled_ts"] is None and ask is not None and ask <= leg["bid"] + 1e-9:
                leg["filled_ts"] = _now_iso(now)
        any_fill = any(l["filled_ts"] for l in legs)
        if all(l["filled_ts"] for l in legs):
            self._set(row["id"], "complete", now, legs=legs)
            return
        if o.hours < self.settings.min_hours_to_target:
            self._set(row["id"], "closed", now, legs=legs, notes="horizon ended")
            return
        age_min = (now - dt.datetime.fromisoformat(row["updated_ts"])).total_seconds() / 60
        if not any_fill and age_min >= self.settings.bid_ttl_min:
            if o.decision == "quote" and o.best is not None:  # re-quote, as the live quoter did
                self.db.conn.execute("UPDATE shadow_baskets SET legs=?, label=?, prob=?, qty=?, updated_ts=? WHERE id=?",
                                     (json.dumps(_legs_from(o.best)), o.best.label, round(o.best.prob, 4), o.best.qty,
                                      _now_iso(now), row["id"]))
            else:
                self._set(row["id"], "expired", now, legs=legs, notes=o.reason)
            return
        if any_fill:
            self.db.conn.execute("UPDATE shadow_baskets SET legs=? WHERE id=?", (json.dumps(legs), row["id"]))

    def _set(self, sid: int, status: str, now: dt.datetime, *, legs: list | None = None, notes: str | None = None) -> None:
        self.db.conn.execute(
            "UPDATE shadow_baskets SET status=?, updated_ts=?, legs=COALESCE(?, legs), notes=COALESCE(?, notes) WHERE id=?",
            (status, _now_iso(now), json.dumps(legs) if legs is not None else None, notes, sid))


def shadow_report(db: Database) -> str:
    """PnL of the paper bid strategy on settled markets: what resting bids would have earned."""
    sett = {r["market_key"]: r["winning_bin"] for r in db.conn.execute(
        "SELECT market_key, winning_bin FROM settlements WHERE winning_bin IS NOT NULL")}
    rows = db.conn.execute("SELECT * FROM shadow_baskets").fetchall()
    stats: dict[str, list[float]] = {}
    for r in rows:
        if r["market_key"] not in sett:
            continue
        legs = json.loads(r["legs"])
        filled = [l for l in legs if l["filled_ts"]]
        kind = "all legs filled" if len(filled) == len(legs) else ("some legs filled" if filled else "no fill")
        pnl = sum(r["qty"] * ((1.0 if l["label"].strip() == sett[r["market_key"]].strip() else 0.0) - l["bid"])
                  for l in filled)
        s = stats.setdefault(kind, [0, 0.0, 0.0])
        s[0] += 1
        s[1] += pnl
        s[2] += sum(r["qty"] * l["bid"] for l in filled)
    open_n = sum(1 for r in rows if r["market_key"] not in sett)
    lines = ["shadow bid model (paper, settled markets):"]
    for kind in ("all legs filled", "some legs filled", "no fill"):
        n, pnl, cost = stats.get(kind, [0, 0.0, 0.0])
        lines.append(f"  {kind:<17} n={n:4d} cost ${cost:7.2f} pnl ${pnl:+7.2f}")
    tot_n = sum(v[0] for v in stats.values()); tot = sum(v[1] for v in stats.values())
    lines.append(f"  total             n={tot_n:4d}               pnl ${tot:+7.2f}   (unsettled shadows: {open_n})")
    return "\n".join(lines)
