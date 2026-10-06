"""Equal quantity across legs; venue minimums; per-venue basket cost cap."""

from __future__ import annotations

import math

from ..venues.base import Venue


def basket_qty(venue: Venue, leg_prices: list[float], max_basket_cost: float) -> float | None:
    """Largest whole quantity such that every leg meets the venue minimum and cost ≤ cap. None if impossible."""
    if not leg_prices or any(p <= 0 for p in leg_prices):
        return None
    min_q = max(venue.min_qty(p) for p in leg_prices)
    sum_p = sum(leg_prices)
    cap_q = math.floor(max_basket_cost / sum_p + 1e-9)
    if cap_q < min_q:
        return None
    return float(cap_q) if venue.name == "kalshi" else float(min_q)
