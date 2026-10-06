"""Plain-text dashboard (spec §25)."""

from __future__ import annotations

from ..markets.model import WeatherMarket
from ..strategy.edge import BasketEval
from ..weather.service import Distributions, StationForecast

LINE = "-" * 60


def market_block(m: WeatherMarket, fc: StationForecast | None, dists: Distributions | None,
                 best: BasketEval | None, decision: str, reason: str | None, hours: float) -> str:
    out = [LINE, f"{m.city.upper()} HIGH | {m.target_date:%b %d} | {m.venue.upper()}",
           f"Station: {m.station_icao} ({m.quantity}, °{m.unit})   {hours:.1f}h to target"]
    if fc and fc.nbm is None and dists and dists.openmeteo and fc.ensemble:
        means = fc.ensemble.model_means()
        out.append(f"Ensemble: mean {dists.openmeteo.mean:.1f} sd {dists.openmeteo.sd:.1f}  "
                   + "  ".join(f"{k.split('_')[0]} {v:.1f}" for k, v in means.items()))
    if fc and fc.nbm:
        n = fc.nbm
        out.append(f"NBM: mean {n.mean:.0f} sd {n.sd:.0f}  P10 {n.p10:.0f} P50 {n.p50:.0f} P90 {n.p90:.0f}"
                   + (f"   OM mean {dists.openmeteo.mean:.1f}" if dists and dists.openmeteo else "")
                   + (f"   NWS {fc.nws_max:.0f}" if fc.nws_max is not None else ""))
    if best:
        out.append(f"BEST BASKET {best.label}  qty {best.qty:g}")
        for l in best.legs:
            ask = f"{l.best_ask:.2f}" if l.best_ask is not None else "  -- "
            bid = f"{l.best_bid:.2f}" if l.best_bid is not None else "  -- "
            out.append(f"  {l.bin.label:<16} p={l.prob:.2f}  bid {bid} ask {ask}  our bid {l.bid_price:.2f}")
        out.append(f"Model probability: {best.prob:6.1%}   Σ our bids: {best.sum_bids:6.3f}   "
                   + (f"Σ asks: {best.sum_asks:6.3f}" if best.sum_asks is not None else "Σ asks:   --  "))
        out.append(f"Net edge (maker): {best.net_edge_maker:6.1%}   "
                   + (f"Net edge (taker): {best.net_edge_taker:6.1%}   " if best.net_edge_taker is not None else "")
                   + (f"gap to asks: {best.gap:.3f}" if best.gap is not None else ""))
        out.append(f"Cost at bids: ${best.cost:.2f}   EV: ${best.expected_profit:+.2f}")
    out.append(f"STATUS: {decision.upper()}" + (f" - {reason}" if reason else ""))
    return "\n".join(out)


def header(bankroll: dict[str, float | None], open_risk: float, risk_today: float) -> str:
    bk = "  ".join(f"{k} ${v:.2f}" if v is not None else f"{k} --" for k, v in bankroll.items())
    return "\n".join(["=" * 60, "WEATHER BOT", "=" * 60, f"Balances: {bk}", f"Open risk: ${open_risk:.2f}   "
                      f"Risk today: ${risk_today:.2f}"])
