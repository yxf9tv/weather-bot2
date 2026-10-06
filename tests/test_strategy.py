import datetime as dt

import pytest

from weather_bot.config import Settings
from weather_bot.markets.model import Bin
from weather_bot.strategy.baskets import contiguous_ranges, range_label
from weather_bot.strategy.edge import evaluate, price_legs
from weather_bot.strategy.filters import basket_gate
from weather_bot.strategy.sizing import basket_qty
from weather_bot.venues.base import BookLevel, OrderBook
from weather_bot.venues.kalshi import KalshiVenue
from weather_bot.venues.polymarket import PolymarketVenue

S = Settings(_env_file=None)
NOW = dt.datetime(2026, 10, 5, tzinfo=dt.timezone.utc)
BINS = (Bin("62° or below", None, 62, "a"), Bin("63° to 64°", 63, 64, "b"), Bin("65° to 66°", 65, 66, "c"),
        Bin("67° to 68°", 67, 68, "d"), Bin("69° to 70°", 69, 70, "e"), Bin("71° or above", 71, None, "f"))
PROBS = {"62° or below": 0.10, "63° to 64°": 0.30, "65° to 66°": 0.30, "67° to 68°": 0.20, "69° to 70°": 0.07,
         "71° or above": 0.03}


def book(iid, bid, ask, size=100.0):
    return OrderBook(iid, (BookLevel(bid, size),), (BookLevel(ask, size),), NOW)


BOOKS = {"a": book("a", 0.05, 0.07), "b": book("b", 0.20, 0.22), "c": book("c", 0.20, 0.22),
         "d": book("d", 0.12, 0.14), "e": book("e", 0.04, 0.06), "f": book("f", 0.01, 0.03)}


@pytest.mark.unit
def test_contiguous_ranges_counts_and_labels():
    rs = contiguous_ranges(BINS, 3, 6)
    assert len(rs) == 4 + 3 + 2 + 1
    assert range_label(rs[0]) == "≤66" and range_label(rs[-1]) == "all"
    assert range_label(BINS[1:4]) == "63-68"


@pytest.mark.unit
def test_price_legs_fair_minus_edge_and_never_crossing():
    legs = price_legs(BINS[1:4], PROBS, BOOKS, min_net_edge=0.10, tick=0.01)
    # total p = 0.80; shares 0.0375, 0.0375, 0.025 → fair-minus-edge 0.2625, 0.2625, 0.175 → capped at ask-tick
    assert [l.bid_price for l in legs] == [0.21, 0.21, 0.13]
    far = {k: book(k, 0.05, 0.60) for k in "bcd"}
    legs2 = price_legs(BINS[1:4], PROBS, far, min_net_edge=0.10, tick=0.01)
    assert [l.bid_price for l in legs2] == [0.26, 0.26, 0.17]
    assert all(l.bid_price < 0.60 for l in legs2)


@pytest.mark.unit
def test_evaluate_kalshi_maker_edge_and_taker_edge():
    v = KalshiVenue(S, client=None)
    ev = evaluate(BINS[1:4], PROBS, BOOKS, v, min_net_edge=0.10, qty=1.0)
    assert ev.prob == pytest.approx(0.80)
    assert ev.sum_bids == pytest.approx(0.55)
    assert ev.maker_fee == 0.0
    assert ev.net_edge_maker == pytest.approx(0.25)
    assert ev.sum_asks == pytest.approx(0.58)
    # taker fees: ceil-cent(0.07·0.22·0.78)=0.02, 0.02, ceil(0.07·0.14·0.86=0.0084)=0.01 → 0.05
    assert ev.taker_fee == pytest.approx(0.05)
    assert ev.net_edge_taker == pytest.approx(0.80 - 0.58 - 0.05)
    assert basket_gate(ev, S) is None


@pytest.mark.unit
def test_basket_gate_rejects_low_probability():
    v = KalshiVenue(S, client=None)
    ev = evaluate(BINS[3:6], PROBS, BOOKS, v, min_net_edge=0.10, qty=1.0)
    assert "range prob" in basket_gate(ev, S)


@pytest.mark.unit
def test_sizing_kalshi_one_dollar_cap():
    v = KalshiVenue(S, client=None)
    assert basket_qty(v, [0.21, 0.21, 0.13], 1.00) == 1.0   # floor(1/0.55)=1
    assert basket_qty(v, [0.21, 0.21, 0.13], 2.00) == 3.0
    assert basket_qty(v, [0.40, 0.40, 0.30], 1.00) is None  # 1.10 > cap


@pytest.mark.unit
def test_sizing_polymarket_floor_and_cap():
    v = PolymarketVenue(S, client=None)
    assert basket_qty(v, [0.21, 0.21, 0.13], 5.00) == 8.0   # ceil(1/0.13)=8 shares each → $4.40
    assert basket_qty(v, [0.21, 0.21, 0.13], 3.00) is None  # 8 × 0.55 = 4.40 > 3
    assert basket_qty(v, [0.30, 0.30, 0.30], 5.00) == 5.0   # 5-share floor → $4.50
