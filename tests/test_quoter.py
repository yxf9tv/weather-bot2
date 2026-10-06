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
