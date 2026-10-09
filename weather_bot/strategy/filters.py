"""Hard gates. Each returns a reason string when the opportunity must be skipped."""

from __future__ import annotations

import datetime as dt

from ..config import Settings
from ..markets.model import WeatherMarket
from ..weather.service import Distributions, StationForecast
from .edge import BasketEval


def market_implied_mean(m: WeatherMarket) -> float | None:
    """Mean of bin centres weighted by normalised mid-prices. Open tails use their edge ± 1°F."""
    weights = []
    for b in m.sorted_bins():
        if b.yes_bid is None and b.yes_ask is None:
            return None
        if b.yes_bid is not None and b.yes_ask is not None:
            mid = (b.yes_bid + b.yes_ask) / 2
        else:
            mid = b.yes_ask if b.yes_ask is not None else b.yes_bid
        if b.lo is None and b.hi is None:
            return None
        centre = (b.hi - 1.0) if b.lo is None else ((b.lo + 1.0) if b.hi is None else (b.lo + b.hi) / 2)
        weights.append((centre, max(mid, 0.0)))
    total = sum(w for _, w in weights)
    if total <= 0:
        return None
    return sum(c * w for c, w in weights) / total


def market_gate_vs_model(m: WeatherMarket, model_mean: float, s: Settings) -> str | None:
    mkt = market_implied_mean(m)
    if mkt is None:
        return None
    if abs(mkt - model_mean) > s.max_market_disagreement(m.unit):
        return f"market disagrees (model {model_mean:.1f} vs market {mkt:.1f})"
    return None


def market_gate(m: WeatherMarket, now: dt.datetime, s: Settings) -> str | None:
    if not m.rules.confident:
        return "rules: " + "; ".join(m.rules.problems)
    h = m.hours_to_target(now)
    if h < s.min_hours_to_target:
        return f"too close ({h:.1f}h)"
    if h > s.max_hours_to_target:
        return f"too far ({h:.1f}h)"
    if m.venue == "polymarket" and not s.polymarket_allowed_on(now.date()):
        return "polymarket disabled by POLYMARKET_TRADING_UNTIL"
    return None


def forecast_gate(fc: StationForecast, dists: Distributions, now: dt.datetime, s: Settings,
                  unit: str = "F") -> str | None:
    if unit == "C":
        # International: Open-Meteo ensemble only. Confidence = agreement between ECMWF / GEFS / ICON means.
        if fc.ensemble is None or dists.openmeteo is None:
            return "no ensemble"
        age = (now - fc.ensemble.fetched_at).total_seconds() / 3600
        if age > s.max_bulletin_age_h:
            return f"ensemble stale ({age:.1f}h)"
        spread = fc.ensemble.model_spread()
        if spread > s.max_intl_model_spread_c:
            means = fc.ensemble.model_means()
            return "models disagree (" + ", ".join(f"{k.split('_')[0]} {v:.1f}" for k, v in means.items()) + ")"
        if dists.openmeteo.sd > s.max_nbp_sd_f * 5 / 9:
            return f"ensemble sd {dists.openmeteo.sd:.1f}C too wide"
        return None
    if fc.nbm is None or dists.nbm is None:
        return "no NBM row"
    age = fc.nbm_age_h(now)
    if age is not None and age > s.max_bulletin_age_h + 1.0:  # bulletin lands ~1h after cycle
        return f"NBM stale ({age:.1f}h)"
    if fc.nbm.sd > s.max_nbp_sd_f:
        return f"NBM sd {fc.nbm.sd:.0f}F too wide"
    if dists.openmeteo is not None and abs(dists.openmeteo.mean - dists.nbm.mean) > s.max_model_disagreement_f:
        return f"models disagree ({dists.nbm.mean:.1f} vs {dists.openmeteo.mean:.1f})"
    if fc.nws_max is not None and abs(fc.nws_max - fc.nbm.mean) > s.max_nws_disagreement_f:
        return f"NWS disagrees ({fc.nws_max:.0f} vs {fc.nbm.mean:.0f})"
    return None


def basket_gate(ev: BasketEval, s: Settings) -> str | None:
    if ev.prob < s.min_range_probability:
        return f"range prob {ev.prob:.2f} < {s.min_range_probability}"
    if ev.net_edge_maker < s.min_net_edge - 1e-9:
        return f"net edge {ev.net_edge_maker:.3f} < {s.min_net_edge}"
    if any(l.best_ask is None for l in ev.legs):
        return "a leg has no ask (dead book)"
    if any(l.bid_price >= (l.best_ask or 1.0) for l in ev.legs):
        return "bid would cross"
    return None


def taker_cost_per_unit(ev: BasketEval) -> float | None:
    """What one $1-payout unit of the range costs when every ask is lifted, fees included."""
    if ev.sum_asks is None or ev.taker_fee is None or ev.qty <= 0:
        return None
    return ev.sum_asks + ev.taker_fee / ev.qty


def taker_gate(ev: BasketEval, s: Settings) -> str | None:
    """A range is buyable at the asks only with the model's edge AND room to move."""
    if ev.prob < s.min_range_probability:
        return f"range prob {ev.prob:.2f} < {s.min_range_probability}"
    cost = taker_cost_per_unit(ev)
    if cost is None or ev.net_edge_taker is None:
        return "a leg has no ask"
    if ev.net_edge_taker < s.min_net_edge - 1e-9:
        return f"taker edge {ev.net_edge_taker:.3f} < {s.min_net_edge}"
    if cost > s.live_max_ask_cost + 1e-9:
        return f"ask cost {cost:.2f} > {s.live_max_ask_cost} (no room to move)"
    return None


def live_gate(hours: float, market_gap: float | None, unit: str, s: Settings) -> str | None:
    """Stricter gates for live taker orders, on top of everything the scanner already checked."""
    if hours > s.live_max_hours_to_target:
        return f"live: {hours:.0f}h > {s.live_max_hours_to_target:.0f}h"
    if market_gap is None:
        return "live: no market-implied mean"
    if market_gap > s.live_max_market_gap(unit) + 1e-9:
        return f"live: model {market_gap:.1f} from market > {s.live_max_market_gap(unit)}"
    return None
