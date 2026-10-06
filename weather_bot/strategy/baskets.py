"""Enumerate contiguous bin ranges."""

from __future__ import annotations

from ..markets.model import Bin


def contiguous_ranges(sorted_bins: tuple[Bin, ...], min_bins: int, max_bins: int) -> list[tuple[Bin, ...]]:
    n = len(sorted_bins)
    out: list[tuple[Bin, ...]] = []
    for width in range(min_bins, min(max_bins, n) + 1):
        for start in range(0, n - width + 1):
            out.append(tuple(sorted_bins[start:start + width]))
    return out


def range_label(bins: tuple[Bin, ...]) -> str:
    lo = bins[0].lo
    hi = bins[-1].hi
    left = "≤" if lo is None else str(lo)
    right = "+" if hi is None else str(hi)
    if lo is None and hi is None:
        return "all"
    if lo is None:
        return f"≤{hi}"
    if hi is None:
        return f"{lo}+"
    return f"{lo}-{hi}"
