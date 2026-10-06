"""Score every logged market after its day ends: truth, winning bin, venue result cross-check, Brier, calibration."""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from zoneinfo import ZoneInfo

import httpx

from ..markets.station_lookup import load_cache
from ..markets.stations import STATIONS
from ..storage.db import Database
from ..weather import truth as truth_mod


def _winning_bin(bins: list[dict], value: int) -> dict | None:
    for b in bins:
        lo, hi = b.get("lo"), b.get("hi")
        if (lo is None or value >= lo) and (hi is None or value <= hi):
            return b
    return None


async def score_pending(db: Database, client: httpx.AsyncClient, now: dt.datetime | None = None) -> int:
    """For each (venue, market_key) with opportunities whose target day is over and no settlement row: fetch truth."""
    now = now or dt.datetime.now(dt.timezone.utc)
    from ..config import load_settings

    load_cache(load_settings().stations_cache)
    rows = db.conn.execute(
        "SELECT DISTINCT o.venue, o.market_key, o.station, o.target_date, o.quantity FROM opportunities o "
        "LEFT JOIN settlements s ON s.market_key = o.market_key WHERE s.id IS NULL").fetchall()
    twc_cache: dict[dt.date, dict] = {}
    scored = 0
    for r in rows:
        st = STATIONS.get(r["station"])
        if st is None:
            continue
        target = dt.date.fromisoformat(r["target_date"])
        day_end = dt.datetime.combine(target + dt.timedelta(days=1), dt.time(6, 0), tzinfo=ZoneInfo(st.tz))
        if now < day_end:
            continue
        try:
            if r["quantity"] == "cli_max":
                if target not in twc_cache:
                    twc_cache[target] = await truth_mod.twc_daily_max(client, target)
                t = twc_cache[target].get(st.icao)
                if t is None or t.value is None or t.status in ("no_report", "pending"):
                    t2 = await truth_mod.iem_cli_daily_max(client, st.icao, target)
                    if t2.value is not None:
                        t = t2
                if t is None or t.value is None:
                    continue
            else:
                t = await truth_mod.polymarket_truth(client, st.icao, target, st.tz, st.unit,
                                                     hourly_filter=(st.unit == "F"))
                if t.value is None or t.status == "partial":
                    continue
        except httpx.HTTPError as exc:
            db.add_event("warn", "score", f"{r['market_key']}: {exc!r}")
            continue
        snap = db.conn.execute("SELECT bins FROM market_snapshots WHERE market_key=? ORDER BY ts DESC LIMIT 1",
                               (r["market_key"],)).fetchone()
        bins = json.loads(snap["bins"]) if snap else []
        win = _winning_bin(bins, t.value)
        db.conn.execute(
            "INSERT OR REPLACE INTO settlements(ts, venue, market_key, truth_value, truth_source, venue_result, "
            "winning_bin, agree, notes) VALUES (?,?,?,?,?,?,?,?,?)",
            (now.isoformat(timespec="seconds"), r["venue"], r["market_key"], t.value, f"{t.source}:{t.status}", None,
             win["label"] if win else None, None, json.dumps(t.detail)))
        scored += 1
    return scored


def calibration_report(db: Database) -> str:
    """Bucket predicted range probability (NBM, Open-Meteo, market mid) vs realized hit rate, plus Brier per source."""
    rows = db.conn.execute(
        "SELECT o.*, s.truth_value FROM opportunities o JOIN settlements s ON s.market_key = o.market_key "
        "WHERE o.range_lo IS NOT NULL OR o.range_hi IS NOT NULL").fetchall()
    buckets: dict[str, dict[str, list[int]]] = {"nbm": defaultdict(list), "openmeteo": defaultdict(list),
                                                 "market": defaultdict(list)}
    brier: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        lo, hi, truth = r["range_lo"], r["range_hi"], r["truth_value"]
        hit = int((lo is None or truth >= lo) and (hi is None or truth <= hi))
        bins = json.loads(r["bins"])
        mids = []
        for b in bins:
            if b.get("bid") is not None and b.get("ask") is not None:
                mids.append(((b["bid"] + b["ask"]) / 2, b))
        market_p = None
        if mids:
            total = sum(m for m, _ in mids) or 1.0
            in_range = [m for m, b in mids if _in_range(b, lo, hi)]
            market_p = sum(in_range) / total
        for source, p in (("nbm", r["prob_nbm"]), ("openmeteo", r["prob_openmeteo"]), ("market", market_p)):
            if p is None:
                continue
            key = f"{int(p * 20) * 5:02d}-{int(p * 20) * 5 + 5:02d}%"
            buckets[source][key].append(hit)
            brier[source].append((p - hit) ** 2)
    out = [f"settled opportunities with a basket: {len(rows)}"]
    for source in ("nbm", "openmeteo", "market"):
        if not brier[source]:
            continue
        out.append(f"\n{source}: Brier {sum(brier[source]) / len(brier[source]):.4f}  (n={len(brier[source])})")
        for key in sorted(buckets[source]):
            hits = buckets[source][key]
            out.append(f"  predicted {key:<8} n={len(hits):<4} hit {sum(hits) / len(hits):6.1%}")
    settled = db.conn.execute("SELECT venue, COUNT(*) n FROM settlements GROUP BY venue").fetchall()
    out.append("\nsettlements: " + ", ".join(f"{s['venue']} {s['n']}" for s in settled))
    return "\n".join(out)


def _in_range(b: dict, lo, hi) -> bool:
    blo = b.get("lo") if b.get("lo") is not None else -10_000
    bhi = b.get("hi") if b.get("hi") is not None else 10_000
    rlo = lo if lo is not None else -10_000
    rhi = hi if hi is not None else 10_000
    return blo >= rlo and bhi <= rhi


def pnl_report(db: Database) -> str:
    rows = db.conn.execute(
        "SELECT b.*, s.truth_value, s.winning_bin FROM baskets b LEFT JOIN settlements s ON s.market_key=b.market_key "
        "WHERE b.status IN ('complete','partial','unwound','completing')").fetchall()
    lines = [f"baskets with fills: {len(rows)}"]
    total_cost = total_payout = 0.0
    for b in rows:
        legs = json.loads(b["legs"])
        qty = float(b["qty"] or 0)
        cost = float(b["filled_cost"] or 0)
        payout = None
        if b["truth_value"] is not None:
            payout = sum(qty for l in legs if _in_range({"lo": _lo(l), "hi": _hi(l)}, b["truth_value"], b["truth_value"]))
            total_cost += cost
            total_payout += payout
        lines.append(f"  #{b['id']} {b['venue']} {b['market_key']} {b['status']} cost ${cost:.2f}"
                     + (f" payout ${payout:.2f} pnl ${payout - cost:+.2f}" if payout is not None else " (unsettled)"))
    lines.append(f"settled: cost ${total_cost:.2f} payout ${total_payout:.2f} pnl ${total_payout - total_cost:+.2f}")
    return "\n".join(lines)


def _lo(leg: dict):
    label = leg.get("label", "")
    from ..markets.rules import parse_bin_label

    b = parse_bin_label(label, "x")
    return b.lo if b else None


def _hi(leg: dict):
    from ..markets.rules import parse_bin_label

    b = parse_bin_label(leg.get("label", ""), "x")
    return b.hi if b else None
