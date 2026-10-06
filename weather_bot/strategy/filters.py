"""Hard gates. Each returns a reason string when the opportunity must be skipped."""

from __future__ import annotations

import datetime as dt

from ..config import Settings
from ..markets.model import WeatherMarket
from ..weather.service import Distributions, StationForecast
from .edge import BasketEval


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


def forecast_gate(fc: StationForecast, dists: Distributions, now: dt.datetime, s: Settings) -> str | None:
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
