"""Price a basket two ways: lifting asks (taker) and resting bids at model fair minus edge (maker)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..markets.model import Bin
from ..venues.base import OrderBook, Venue
from .baskets import range_label


@dataclass(frozen=True)
class Leg:
    bin: Bin
    prob: float
    best_bid: float | None
    best_ask: float | None
    ask_size: float | None
    bid_price: float  # our resting bid


@dataclass(frozen=True)
class BasketEval:
    label: str
    legs: tuple[Leg, ...]
    prob: float                 # model probability of the range
    qty: float
    sum_bids: float             # Σ our bid prices (per contract)
    sum_asks: float | None      # Σ best asks (None if a leg has no ask)
    maker_fee: float            # total $ fees if filled as maker
    taker_fee: float | None     # total $ fees if lifting asks
    net_edge_maker: float       # prob − Σbids − fees/qty
    net_edge_taker: float | None
    gap: float | None           # Σ(best_ask − our bid): how far our quotes sit from executable
    expected_profit: float      # qty·prob − qty·Σbids − fees
    expected_roi: float

    @property
    def cost(self) -> float:
        return self.qty * self.sum_bids

    def as_row(self) -> dict:
        return {"range_lo": self.legs[0].bin.lo, "range_hi": self.legs[-1].bin.hi, "n_bins": len(self.legs),
                "prob_used": round(self.prob, 4), "cost_bids": round(self.sum_bids, 4),
                "cost_asks": None if self.sum_asks is None else round(self.sum_asks, 4),
                "fees": round(self.maker_fee, 4), "net_edge": round(self.net_edge_maker, 4),
                "expected_profit": round(self.expected_profit, 4), "expected_roi": round(self.expected_roi, 4),
                "qty": self.qty}


def _round_down_tick(x: float, tick: float) -> float:
    return math.floor(x / tick + 1e-9) * tick


def price_legs(bins: tuple[Bin, ...], probs: dict[str, float], books: dict[str, OrderBook], *,
               min_net_edge: float, tick: float) -> tuple[Leg, ...]:
    """Bid_i = min(best_ask_i − tick, p_i − edge_share_i), edge_share ∝ p_i. Never below one tick."""
    total_p = sum(probs[b.label] for b in bins)
    legs = []
    for b in bins:
        p = probs[b.label]
        share = min_net_edge * (p / total_p if total_p > 0 else 1 / len(bins))
        book = books.get(b.instrument_id)
        best_ask = book.best_ask if book else b.yes_ask
        best_bid = book.best_bid if book else b.yes_bid
        ask_size = book.asks[0].size if book and book.asks else b.ask_size
        bid = p - share
        if best_ask is not None:
            bid = min(bid, best_ask - tick)
        bid = max(_round_down_tick(bid, tick), tick)
        legs.append(Leg(b, p, best_bid, best_ask, ask_size, round(bid, 4)))
    return tuple(legs)


def evaluate(bins: tuple[Bin, ...], probs: dict[str, float], books: dict[str, OrderBook], venue: Venue, *,
             min_net_edge: float, qty: float, slippage_per_bin: float = 0.0) -> BasketEval:
    legs = price_legs(bins, probs, books, min_net_edge=min_net_edge, tick=venue.tick)
    prob = sum(l.prob for l in legs)
    sum_bids = sum(l.bid_price for l in legs)
    maker_fee = sum(venue.maker_fee(l.bid_price, qty) for l in legs)
    asks = [l.best_ask for l in legs]
    if all(a is not None for a in asks):
        sum_asks = sum(asks)  # type: ignore[arg-type]
        taker_fee = sum(venue.taker_fee(a, qty) for a in asks)  # type: ignore[arg-type]
        net_taker = prob - sum_asks - taker_fee / qty - slippage_per_bin * len(legs)
        gap = sum(a - l.bid_price for a, l in zip(asks, legs))  # type: ignore[operator]
    else:
        sum_asks, taker_fee, net_taker, gap = None, None, None, None
    net_maker = prob - sum_bids - maker_fee / qty
    exp_profit = qty * prob - qty * sum_bids - maker_fee
    cost = qty * sum_bids
    return BasketEval(range_label(bins), legs, prob, qty, sum_bids, sum_asks, maker_fee, taker_fee,
                      net_maker, net_taker, gap, exp_profit, exp_profit / cost if cost > 0 else 0.0)
