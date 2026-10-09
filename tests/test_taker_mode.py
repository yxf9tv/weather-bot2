import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from weather_bot.config import Settings
from weather_bot.execution.quoter import Quoter
from weather_bot.execution.shadow import ShadowBids, shadow_report
from weather_bot.markets.model import Bin
from weather_bot.storage.db import Database
from weather_bot.strategy.edge import evaluate
from weather_bot.strategy.filters import live_gate, taker_cost_per_unit, taker_gate
from weather_bot.venues.base import BookLevel, OrderBook
from tests.test_quoter import BINS, FakeVenue, book

NOW = dt.datetime(2026, 10, 9, 12, tzinfo=dt.timezone.utc)
S = Settings(_env_file=None, min_net_edge=0.10, min_range_probability=0.70, live_max_ask_cost=0.85)


def ev_with(asks, probs=(0.40, 0.35, 0.20)):
    v = FakeVenue()
    v.books = {iid: book(iid, round(a - 0.05, 2), a) for iid, a in zip("bcd", asks)}
    p = {b.label: x for b, x in zip(BINS, probs)}
    return evaluate(BINS, p, v.books, v, min_net_edge=0.10, qty=1.0), v


@pytest.mark.unit
def test_taker_gate_requires_room_to_move():
    ok, _ = ev_with((0.25, 0.25, 0.15))     # cost 0.65, prob 0.95 → edge 0.30, room 35c
    assert taker_gate(ok, S) is None
    pricey, _ = ev_with((0.30, 0.30, 0.27), probs=(0.45, 0.40, 0.14))   # cost 0.87 > 0.85
    assert taker_cost_per_unit(pricey) == pytest.approx(0.87)
    assert "room to move" in taker_gate(pricey, S)
    thin, _ = ev_with((0.30, 0.30, 0.25), probs=(0.35, 0.30, 0.20))     # edge < 10 points
    assert "taker edge" in taker_gate(thin, S)


@pytest.mark.unit
def test_live_gate_horizon_and_market_gap():
    s = Settings(_env_file=None)
    assert live_gate(20, 0.6, "F", s) is None
    assert "36h" in live_gate(40, 0.2, "F", s)
    assert "from market" in live_gate(20, 1.4, "F", s)
    assert "from market" in live_gate(20, 1.2, "C", s)
    assert "no market-implied" in live_gate(20, None, "F", s)


@pytest.mark.unit
async def test_taker_only_never_rests_a_bid(tmp_path: Path):
    db = Database(tmp_path / "t.sqlite")
    ev, v = ev_with((0.30, 0.30, 0.25), probs=(0.35, 0.30, 0.20))   # not takeable
    q = Quoter({"kalshi": v}, db, S, live=True)
    assert await q.place_basket(v, "k", ev, None, NOW, taker_only=True) is None
    assert v.placed == []
    ev2, v2 = ev_with((0.25, 0.25, 0.15))                           # takeable
    q2 = Quoter({"kalshi": v2}, db, S, live=True)
    bid = await q2.place_basket(v2, "k2", ev2, None, NOW, taker_only=True)
    assert bid is not None and all(p["ioc"] for p in v2.placed) and len(v2.placed) == 3
    row = db.conn.execute("SELECT intended_cost FROM baskets WHERE id=?", (bid,)).fetchone()
    assert row["intended_cost"] == pytest.approx(0.65)                # cost at the asks, not at the bids
    db.close()


@dataclass
class FakeMarket:
    key: str
    bins: tuple
    venue: str = "kalshi"


@dataclass
class FakeOpp:
    market: FakeMarket
    best: object
    decision: str = "quote"
    reason: str | None = None
    hours: float = 20.0
    books: dict = field(default_factory=dict)


@pytest.mark.unit
def test_shadow_fills_when_ask_drops_to_bid_and_scores(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    s = Settings(_env_file=None, bid_ttl_min=30, min_hours_to_target=8)
    ev, v = ev_with((0.25, 0.25, 0.15))
    sh = ShadowBids(db, s)
    mk = FakeMarket("kalshi:KNYC:2026-10-10:cli_max", BINS)
    sh.update([FakeOpp(mk, ev, books=v.books)], NOW)
    row = db.conn.execute("SELECT * FROM shadow_baskets").fetchone()
    assert row["status"] == "open"
    bids = {l["iid"]: l["bid"] for l in json.loads(row["legs"])}
    # ask on leg c falls to our bid → c filled; others not
    v.books["c"] = book("c", bids["c"] - 0.01, bids["c"])
    sh.update([FakeOpp(mk, ev, books=v.books)], NOW + dt.timedelta(minutes=5))
    legs = json.loads(db.conn.execute("SELECT legs FROM shadow_baskets").fetchone()["legs"])
    assert [l["iid"] for l in legs if l["filled_ts"]] == ["c"]
    # a frozen basket does not re-quote after TTL; horizon end closes it
    sh.update([FakeOpp(mk, ev, books=v.books, hours=7.0)], NOW + dt.timedelta(minutes=60))
    assert db.conn.execute("SELECT status FROM shadow_baskets").fetchone()["status"] == "closed"
    # settlement: c's bin won → pnl = qty * (1 - bid_c)
    db.conn.execute("INSERT INTO settlements(ts, venue, market_key, truth_value, winning_bin) VALUES (?,?,?,?,?)",
                    (NOW.isoformat(), "kalshi", mk.key, 65.0, BINS[1].label))
    rep = shadow_report(db)
    assert "some legs filled  n=   1" in rep and f"{1 - bids['c']:+7.2f}" in rep
    db.close()


@pytest.mark.unit
def test_shadow_requotes_unfilled_after_ttl_and_expires_when_not_quotable(tmp_path: Path):
    db = Database(tmp_path / "s2.sqlite")
    s = Settings(_env_file=None, bid_ttl_min=30)
    ev, v = ev_with((0.25, 0.25, 0.15))
    sh = ShadowBids(db, s)
    mk = FakeMarket("k", BINS)
    sh.update([FakeOpp(mk, ev, books=v.books)], NOW)
    sh.update([FakeOpp(mk, ev, books=v.books)], NOW + dt.timedelta(minutes=31))     # re-quote, still open
    assert db.conn.execute("SELECT status FROM shadow_baskets").fetchone()["status"] == "open"
    sh.update([FakeOpp(mk, None, decision="skip", reason="gone", books=v.books)], NOW + dt.timedelta(minutes=62))
    assert db.conn.execute("SELECT status FROM shadow_baskets").fetchone()["status"] == "expired"
    db.close()


@pytest.mark.unit
def test_book_logging_is_throttled(tmp_path: Path):
    db = Database(tmp_path / "b.sqlite")
    b1 = OrderBook("x", (BookLevel(0.2, 10),), (BookLevel(0.3, 10),), NOW)
    db.add_book("kalshi", b1)
    db.add_book("kalshi", OrderBook("x", b1.bids, b1.asks, NOW + dt.timedelta(minutes=1)))     # unchanged → skip
    db.add_book("kalshi", OrderBook("x", b1.bids, (BookLevel(0.29, 10),), NOW + dt.timedelta(minutes=2)))  # changed
    db.add_book("kalshi", OrderBook("x", b1.bids, (BookLevel(0.29, 10),), NOW + dt.timedelta(minutes=20)))  # stale→store
    assert db.conn.execute("SELECT count(*) FROM books").fetchone()[0] == 3
    db.close()
