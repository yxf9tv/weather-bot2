"""Turn forecasts into a probability mass over whole-degree settlement values, then into bin probabilities."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..markets.model import Bin
from .nbm import MaxTPercentiles

SQRT2 = math.sqrt(2.0)


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / SQRT2))


@dataclass(frozen=True)
class Distribution:
    source: str
    pmf: dict[int, float]  # whole °F -> probability, sums to ~1

    @property
    def mean(self) -> float:
        return sum(t * p for t, p in self.pmf.items())

    @property
    def sd(self) -> float:
        mu = self.mean
        return math.sqrt(sum(p * (t - mu) ** 2 for t, p in self.pmf.items()))

    def prob_range(self, lo: int | None, hi: int | None) -> float:
        return sum(p for t, p in self.pmf.items() if (lo is None or t >= lo) and (hi is None or t <= hi))

    def bin_probabilities(self, bins: tuple[Bin, ...]) -> dict[str, float]:
        return {b.label: self.prob_range(b.lo, b.hi) for b in bins}

    def as_dict(self) -> dict:
        return {"source": self.source, "mean": round(self.mean, 2), "sd": round(self.sd, 2),
                "pmf": {str(t): round(p, 5) for t, p in sorted(self.pmf.items()) if p > 1e-5}}


def _discretise(cdf, center: float, width: float, source: str) -> Distribution:
    lo = int(math.floor(center - width))
    hi = int(math.ceil(center + width))
    pmf: dict[int, float] = {}
    for t in range(lo, hi + 1):
        p = cdf(t + 0.5) - cdf(t - 0.5)
        if p > 0:
            pmf[t] = p
    total = sum(pmf.values())
    pmf = {t: p / total for t, p in pmf.items()} if total > 0 else {int(round(center)): 1.0}
    return Distribution(source, pmf)


def from_percentiles(pct: MaxTPercentiles, *, offset_f: float = 0.0, inflation: float = 1.0,
                     source: str = "nbm") -> Distribution:
    """Piecewise-linear CDF through P10..P90 (deviations from the median scaled by `inflation`),
    normal tails with sigma = sd * inflation outside P10/P90. Values shifted by `offset_f`."""
    med = pct.p50
    pts = []
    last = -1e9
    for q, v in ((0.10, pct.p10), (0.25, pct.p25), (0.50, pct.p50), (0.75, pct.p75), (0.90, pct.p90)):
        x = med + (v - med) * inflation + offset_f
        x = max(x, last + 0.05)  # keep strictly increasing even when percentiles tie
        pts.append((x, q))
        last = x
    sigma = max(pct.sd * inflation, 0.5)
    x10, x90 = pts[0][0], pts[-1][0]

    def cdf(x: float) -> float:
        if x <= x10:
            return 0.10 * 2.0 * _phi((x - x10) / sigma)
        if x >= x90:
            return 1.0 - 0.10 * 2.0 * _phi((x90 - x) / sigma)
        for (xa, qa), (xb, qb) in zip(pts, pts[1:]):
            if xa <= x <= xb:
                return qa + (qb - qa) * (x - xa) / (xb - xa)
        return 0.5

    return _discretise(cdf, med + offset_f, 6 * sigma + 4, source)


def from_members(members: list[float], *, bias_f: float = 0.0, inflation: float = 1.4,
                 extra_sigma_f: float = 1.0, source: str = "openmeteo") -> Distribution:
    """Kernel-dressed ensemble: total variance = inflation^2 * var(members) + extra_sigma^2."""
    n = len(members)
    if n == 0:
        raise ValueError("no members")
    mu = sum(members) / n
    var = sum((m - mu) ** 2 for m in members) / max(n - 1, 1)
    kernel_var = max(inflation ** 2 - 1.0, 0.0) * var + extra_sigma_f ** 2
    k = math.sqrt(max(kernel_var, 0.25))

    def cdf(x: float) -> float:
        return sum(_phi((x - (m + bias_f)) / k) for m in members) / n

    width = 4 * math.sqrt(var + kernel_var) + 4
    return _discretise(cdf, mu + bias_f, width, source)
