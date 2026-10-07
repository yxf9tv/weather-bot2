import datetime as dt
from pathlib import Path

import pytest

from weather_bot.config import Settings
from weather_bot.execution.quoter import Quoter
from weather_bot.markets.model import Bin
from weather_bot.storage.db import Database
from weather_bot.strategy.edge import evaluate
from weather_bot.venues.base import BookLevel, OrderBook, OrderResult

NOW = dt.datetime(2026, 10, 6, 12, tzinfo=dt.timezone.utc)
BINS = (Bin("63° to 64°", 63, 64, "b"), Bin("65° to 66°", 65, 66, "c"), Bin("67° to 68°", 67, 68, "d"))
PROBS = {"63° to 64°": 0.30, "65° to 66°": 0.30, "67° to 68°": 0.20}


def book(iid, bid, ask):
    return OrderBook(iid, (BookLevel(bid, 100.0),), (BookLevel(ask, 100.0),), NOW)


class FakeVenue:
    name = "kalshi"
    tick = 0.01

    def __init__(self, reject: set[str] | None = None):
        self.orders: dict[str, dict] = {}
        self.books = {"b": book("b", 0.20, 0.30), "c": book("c", 0.20, 0.30), "d": book("d", 0.12, 0.20)}
        self.reject = reject or set()
        self.cancelled: list[str] = []
        self.placed: list[dict] = []

    async def discover(self):
        return []

    async def orderbook(self, iid):
        return self.books[iid]

    def taker_fee(self, p, q):
        return 0.0

    def maker_fee(self, p, q):
        return 0.0

    def min_qty(self, p, marketable=False):
        return 1.0

    async def place_limit(self, iid, price, qty, *, side="buy", post_only=True, ioc=False, client_id):
        self.placed.append({"iid": iid, "price": price, "qty": qty, "side": side, "ioc": ioc})
        if iid in self.reject:
            return OrderResult("", "rejected", 0.0, None, {"why": "test"})
        oid = f"o{len(self.orders) + 1}"
        self.orders[oid] = {"iid": iid, "price": price, "qty": qty, "filled": 0.0, "status": "resting", "side": side}
        return OrderResult(oid, "resting", 0.0, None, {})

    async def cancel(self, oid):
        self.cancelled.append(oid)
        if self.orders[oid]["status"] == "resting":
            self.orders[oid]["status"] = "cancelled"

    async def order_status(self, oid):
        o = self.orders[oid]
        return OrderResult(oid, o["status"], o["filled"], o["price"] if o["filled"] else None, {})

    async def positions(self):
        return []

    async def balance(self):
        return 100.0

    def fill(self, iid, qty=None):
        for oid, o in self.orders.items():
            if o["iid"] == iid and o["status"] in ("resting", "partial"):
                o["filled"] = o["qty"] if qty is None else qty
                o["status"] = "filled" if o["filled"] >= o["qty"] else "partial"


@pytest.fixture
def env(tmp_path: Path):
    db = Database(tmp_path / "t.sqlite")
    s = Settings(_env_file=None, bid_ttl_min=30, basket_complete_timeout_min=120, min_net_edge=0.10)
    v = FakeVenue()
    q = Quoter({"kalshi": v}, db, s, live=True)
    ev = evaluate(BINS, PROBS, v.books, v, min_net_edge=0.10, qty=1.0)
    yield db, s, v, q, ev
    db.close()


def basket(db, bid):
    return db.conn.execute("SELECT * FROM baskets WHERE id=?", (bid,)).fetchone()


@pytest.mark.unit
async def test_dry_mode_places_nothing(tmp_path):
    db = Database(tmp_path / "d.sqlite")
    v = FakeVenue()
    q = Quoter({"kalshi": v}, db, Settings(_env_file=None), live=False)
    ev = evaluate(BINS, PROBS, v.books, v, min_net_edge=0.10, qty=1.0)
    assert await q.place_basket(v, "k", ev, None, NOW) is None
    assert v.placed == [] and db.conn.execute("SELECT COUNT(*) c FROM baskets").fetchone()["c"] == 0


@pytest.mark.unit
async def test_place_then_all_fill_completes(env):
    db, s, v, q, ev = env
    bid = await q.place_basket(v, "kalshi:KNYC:2026-10-07:cli_max", ev, None, NOW)
    assert basket(db, bid)["status"] == "resting" and len(v.placed) == 3
    assert all(p["side"] == "buy" and not p["ioc"] for p in v.placed)
    for iid in "bcd":
        v.fill(iid)
    await q.refresh(NOW + dt.timedelta(minutes=5))
    b = basket(db, bid)
    assert b["status"] == "complete"
    assert b["filled_cost"] == pytest.approx(ev.sum_bids)


@pytest.mark.unit
async def test_rejected_leg_aborts_and_cancels_others(env):
    db, s, v, q, ev = env
    v.reject = {"c"}
    bid = await q.place_basket(v, "k", ev, None, NOW)
    assert basket(db, bid)["status"] == "aborted"
    assert all(o["status"] == "cancelled" for o in v.orders.values())


@pytest.mark.unit
async def test_ttl_expires_unfilled_basket(env):
    db, s, v, q, ev = env
    bid = await q.place_basket(v, "k", ev, None, NOW)
    await q.refresh(NOW + dt.timedelta(minutes=10))
    assert basket(db, bid)["status"] == "resting"
    await q.refresh(NOW + dt.timedelta(minutes=31))
    assert basket(db, bid)["status"] == "expired" and len(v.cancelled) == 3


@pytest.mark.unit
async def test_model_move_cancels_when_bid_above_fair(env):
    db, s, v, q, ev = env
    bid = await q.place_basket(v, "k", ev, None, NOW)
    fair = {"k": {"b": 0.10, "c": 0.10, "d": 0.05}}  # model dropped: our bids now above fair
    await q.refresh(NOW + dt.timedelta(minutes=2), fair_bids=fair)
    assert basket(db, bid)["status"] == "expired" and basket(db, bid)["notes"] == "model moved"


@pytest.mark.unit
async def test_partial_then_lift_remaining_when_still_positive(env):
    db, s, v, q, ev = env
    bid = await q.place_basket(v, "k", ev, None, NOW)
    v.fill("b")
    v.fill("c")
    await q.refresh(NOW + dt.timedelta(minutes=5))
    assert basket(db, bid)["status"] == "partial"
    # remaining leg d: ask 0.20; filled cost 0.26+0.26=0.52; prob 0.80 → net 0.80-0.72 = 0.08 < 0.10 → unwind
    await q.refresh(NOW + dt.timedelta(minutes=121))
    b = basket(db, bid)
    assert b["status"] == "unwinding"
    sells = [p for p in v.placed if p["side"] == "sell"]
    assert len(sells) == 2 and all(p["price"] == pytest.approx(0.27) for p in sells)


@pytest.mark.unit
async def test_partial_then_completing_when_cheap_ask(env):
    db, s, v, q, ev = env
    bid = await q.place_basket(v, "k", ev, None, NOW)
    v.fill("b")
    v.fill("c")
    v.books["d"] = book("d", 0.05, 0.08)  # remaining leg got cheap: 0.80 - (0.52+0.08) = 0.20 ≥ 0.10
    await q.refresh(NOW + dt.timedelta(minutes=121))
    b = basket(db, bid)
    assert b["status"] == "completing"
    lifts = [p for p in v.placed if p["ioc"]]
    assert len(lifts) == 1 and lifts[0]["iid"] == "d" and lifts[0]["price"] == 0.08


@pytest.mark.unit
async def test_partial_completion_blocked_when_market_gated(env):
    db, s, v, q, ev = env
    bid = await q.place_basket(v, "k", ev, None, NOW)
    v.fill("b")
    v.fill("c")
    v.books["d"] = book("d", 0.05, 0.08)  # cheap enough to complete...
    await q.refresh(NOW + dt.timedelta(minutes=121), blocked_keys={"k"})  # ...but the market is gated
    assert basket(db, bid)["status"] == "unwinding"
    assert not [p for p in v.placed if p["ioc"]]


@pytest.mark.unit
async def test_partial_completion_uses_current_probabilities(env):
    db, s, v, q, ev = env
    bid = await q.place_basket(v, "k", ev, None, NOW)
    v.fill("b")
    v.fill("c")
    v.books["d"] = book("d", 0.05, 0.08)
    # model collapsed: current probabilities make completion -EV even at a cheap ask
    await q.refresh(NOW + dt.timedelta(minutes=121), fair_bids={"k": {"b": 0.10, "c": 0.10, "d": 0.05}})
    assert basket(db, bid)["status"] == "unwinding"


@pytest.mark.unit
async def test_taker_path_when_asks_clear_edge(tmp_path):
    db = Database(tmp_path / "t2.sqlite")
    s = Settings(_env_file=None, min_net_edge=0.10)
    v = FakeVenue()
    v.books = {"b": book("b", 0.10, 0.12), "c": book("c", 0.10, 0.12), "d": book("d", 0.05, 0.06)}  # Σask 0.30 vs P 0.80

    async def place(iid, price, qty, *, side="buy", post_only=True, ioc=False, client_id):
        v.placed.append({"iid": iid, "price": price, "qty": qty, "side": side, "ioc": ioc})
        oid = f"o{len(v.orders) + 1}"
        v.orders[oid] = {"iid": iid, "price": price, "qty": qty, "filled": qty if ioc else 0.0,
                         "status": "filled" if ioc else "resting", "side": side}
        return OrderResult(oid, "filled" if ioc else "resting", qty if ioc else 0.0, price if ioc else None, {})

    v.place_limit = place  # type: ignore[method-assign]
    q = Quoter({"kalshi": v}, db, s, live=True)
    ev = evaluate(BINS, PROBS, v.books, v, min_net_edge=0.10, qty=1.0)
    assert ev.net_edge_taker >= 0.10
    bid = await q.place_basket(v, "k", ev, None, NOW)
    b = basket(db, bid)
    assert b["status"] == "complete" and all(p["ioc"] for p in v.placed) and len(v.placed) == 3
    assert b["filled_cost"] == pytest.approx(0.30)
    db.close()


@pytest.mark.unit
async def test_cheap_legs_deferred_then_completed_on_center_fill(tmp_path):
    db = Database(tmp_path / "t3.sqlite")
    s = Settings(_env_file=None, min_net_edge=0.10, min_resting_bid=0.05, basket_complete_timeout_min=0)
    v = FakeVenue()
    probs = {"63° to 64°": 0.45, "65° to 66°": 0.40, "67° to 68°": 0.03}
    v.books = {"b": book("b", 0.30, 0.50), "c": book("c", 0.30, 0.50), "d": book("d", 0.01, 0.02)}
    q = Quoter({"kalshi": v}, db, s, live=True)
    ev = evaluate(BINS, probs, v.books, v, min_net_edge=0.10, qty=1.0)
    bid = await q.place_basket(v, "k", ev, None, NOW)
    assert len(v.placed) == 2, "the 1-cent tail leg must not be rested"
    assert "deferred" in basket(db, bid)["notes"]
    v.fill("b")
    v.fill("c")
    await q.refresh(NOW + dt.timedelta(minutes=1))
    b = basket(db, bid)
    lifts = [p for p in v.placed if p["ioc"]]
    assert b["status"] == "completing" and len(lifts) == 1 and lifts[0]["iid"] == "d" and lifts[0]["price"] == 0.02
    db.close()


@pytest.mark.unit
async def test_tail_fill_alone_unwinds_immediately(tmp_path):
    db = Database(tmp_path / "t4.sqlite")
    s = Settings(_env_file=None, min_net_edge=0.10, min_resting_bid=0.0, basket_complete_timeout_min=0)
    v = FakeVenue()
    probs = {"63° to 64°": 0.45, "65° to 66°": 0.40, "67° to 68°": 0.03}
    v.books = {"b": book("b", 0.30, 0.50), "c": book("c", 0.30, 0.50), "d": book("d", 0.01, 0.02)}
    q = Quoter({"kalshi": v}, db, s, live=True)
    ev = evaluate(BINS, probs, v.books, v, min_net_edge=0.10, qty=1.0)
    bid = await q.place_basket(v, "k", ev, None, NOW)
    v.fill("d")  # only the tail fills; completing at 0.50+0.50 has no edge
    await q.refresh(NOW + dt.timedelta(minutes=1))
    b = basket(db, bid)
    assert b["status"] == "unwinding"
    sells = [p for p in v.placed if p["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["iid"] == "d"
    db.close()


@pytest.mark.unit
async def test_unwind_sells_only_net_position_and_ignores_rejected_sells(tmp_path):
    """Regression: a rejected sell left the basket 'unwinding' forever; every TTL re-sold the leg → short 8."""
    db = Database(tmp_path / "t5.sqlite")
    s = Settings(_env_file=None, min_net_edge=0.10, min_resting_bid=0.0, basket_complete_timeout_min=0,
                 unwind_ttl_min=30)
    v = FakeVenue()
    probs = {"63° to 64°": 0.45, "65° to 66°": 0.40, "67° to 68°": 0.03}
    v.books = {"b": book("b", 0.30, 0.50), "c": book("c", 0.30, 0.50), "d": book("d", 0.01, 0.02)}
    q = Quoter({"kalshi": v}, db, s, live=True)
    ev = evaluate(BINS, probs, v.books, v, min_net_edge=0.10, qty=1.0)
    bid = await q.place_basket(v, "k", ev, None, NOW)
    v.fill("d")
    await q.refresh(NOW + dt.timedelta(minutes=1))                       # → unwinding, resting ask placed
    db.conn.execute("UPDATE orders SET status='rejected' WHERE basket_id=? AND side='sell'", (bid,))  # e.g. 401
    for o in v.orders.values():                                          # a rejected order never reached the book
        if o["side"] == "sell":
            o["status"] = "cancelled"
    await q.refresh(NOW + dt.timedelta(minutes=32))                      # TTL → sell at bid (IOC)
    v.fill("d")                                                          # the IOC sell fills
    await q.refresh(NOW + dt.timedelta(minutes=33))
    assert basket(db, bid)["status"] == "unwound"
    await q.refresh(NOW + dt.timedelta(minutes=70))
    await q.refresh(NOW + dt.timedelta(minutes=110))
    sold = sum(p["qty"] for p in v.placed if p["side"] == "sell")
    assert sold <= 2.0                                                   # 1 resting (rejected) + 1 IOC, never more
    filled_sells = sum(o["filled"] for o in v.orders.values() if o["side"] == "sell")
    assert filled_sells == pytest.approx(1.0)                            # net position is flat, not short
    db.close()


@pytest.mark.unit
async def test_completion_counts_only_cost_beyond_reserved_budget(env):
    db, s, v, q, ev = env
    bid = await q.place_basket(v, "k", ev, None, NOW)
    v.fill("b")
    v.fill("c")
    v.books["d"] = book("d", 0.05, 0.08)  # completing costs 0.52 + 0.08 = 0.60 < intended cost already reserved
    tight = Settings(_env_file=None, bid_ttl_min=30, basket_complete_timeout_min=120, min_net_edge=0.10,
                     max_daily_new_risk=round(ev.cost + 0.01, 2), max_total_open_risk=round(ev.cost + 0.01, 2))
    q2 = Quoter({"kalshi": v}, db, tight, live=True)
    await q2.refresh(NOW + dt.timedelta(minutes=121))
    assert basket(db, bid)["status"] == "completing"


@pytest.mark.unit
async def test_partial_fill_is_held_until_completion_timeout(env):
    db, s, v, q, ev = env                      # basket_complete_timeout_min=120 in this fixture
    bid = await q.place_basket(v, "k", ev, None, NOW)
    v.fill("c")
    await q.refresh(NOW + dt.timedelta(minutes=1))
    assert basket(db, bid)["status"] == "partial"
    assert not [p for p in v.placed if p["side"] == "sell"]
    assert Settings(_env_file=None).basket_complete_timeout_min >= 60   # default must not be 0 any more


@pytest.mark.unit
async def test_completion_ignores_total_open_risk_cap(env):
    """A partial basket's cost was approved when it was quoted; the open-risk cap must not strand it as a stub."""
    db, s, v, q, ev = env
    bid = await q.place_basket(v, "k", ev, None, NOW)
    v.fill("b")
    v.fill("c")
    v.books["d"] = book("d", 0.05, 0.08)
    tight = Settings(_env_file=None, bid_ttl_min=30, basket_complete_timeout_min=120, min_net_edge=0.10,
                     max_total_open_risk=0.10, max_daily_new_risk=30.0)   # open cap already blown
    q2 = Quoter({"kalshi": v}, db, tight, live=True)
    await q2.refresh(NOW + dt.timedelta(minutes=121))
    assert basket(db, bid)["status"] == "completing"


@pytest.mark.unit
async def test_polymarket_rests_only_legs_at_or_above_its_own_floor(tmp_path):
    db = Database(tmp_path / "t6.sqlite")
    s = Settings(_env_file=None, min_net_edge=0.10, min_resting_bid=0.05, min_resting_bid_polymarket=0.15)
    v = FakeVenue()
    v.name = "polymarket"
    probs = {"63° to 64°": 0.45, "65° to 66°": 0.40, "67° to 68°": 0.12}
    v.books = {"b": book("b", 0.30, 0.50), "c": book("c", 0.30, 0.50), "d": book("d", 0.08, 0.12)}
    q = Quoter({"polymarket": v}, db, s, live=True)
    ev = evaluate(BINS, probs, v.books, v, min_net_edge=0.10, qty=5.0)
    d_bid = next(l.bid_price for l in ev.legs if l.bin.instrument_id == "d")
    assert 0.05 <= d_bid < 0.15, d_bid                       # would rest on Kalshi, must be deferred on Polymarket
    await q.place_basket(v, "p", ev, None, NOW)
    assert sorted(p["iid"] for p in v.placed) == ["b", "c"]
    db.close()
