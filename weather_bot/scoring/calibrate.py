"""Fit per-(station, quantity) offsets and spread inflation from stored NBM forecasts vs settlement truth.

Uses this bot's own database: the latest NBM row stored ≥ `min_lead_h` before the target day, paired with the
settlement value for that station/day/quantity. Writes data/calibration.json, which ForecastService loads.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections import defaultdict
from pathlib import Path

from ..storage.db import Database

MIN_SAMPLES = 15


def fit(db: Database, min_lead_h: float = 8.0) -> dict:
    rows = db.conn.execute(
        "SELECT s.market_key, s.truth_value, o.station, o.target_date, o.quantity FROM settlements s "
        "JOIN (SELECT DISTINCT market_key, station, target_date, quantity FROM opportunities) o "
        "ON o.market_key = s.market_key WHERE s.truth_value IS NOT NULL").fetchall()
    errors: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)  # (truth-mean, nbm_sd)
    for r in rows:
        target = dt.date.fromisoformat(r["target_date"])
        cutoff = dt.datetime.combine(target, dt.time(0, 0), tzinfo=dt.timezone.utc) - dt.timedelta(hours=min_lead_h)
        fc = db.conn.execute(
            "SELECT payload FROM forecasts WHERE source='nbm' AND station=? AND target_date=? AND issued_at <= ? "
            "ORDER BY issued_at DESC LIMIT 1", (r["station"], r["target_date"], cutoff.isoformat())).fetchone()
        if not fc:
            continue
        p = json.loads(fc["payload"])
        errors[(r["station"], r["quantity"])].append((float(r["truth_value"]) - float(p["mean"]), float(p["sd"])))
    out: dict[str, dict] = {}
    for (station, quantity), pairs in errors.items():
        n = len(pairs)
        if n < MIN_SAMPLES:
            continue
        offs = [e for e, _ in pairs]
        mean_off = sum(offs) / n
        resid_sd = math.sqrt(sum((e - mean_off) ** 2 for e in offs) / max(n - 1, 1))
        mean_sd = sum(sd for _, sd in pairs) / n
        inflation = max(0.8, min(2.5, resid_sd / mean_sd if mean_sd > 0 else 1.0))
        out[f"{station}:{quantity}"] = {"offset_f": round(mean_off, 2), "inflation": round(inflation, 3),
                                        "n": n, "resid_sd": round(resid_sd, 2), "mean_nbm_sd": round(mean_sd, 2)}
    return out


def save(cal: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fitted_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                "stations": cal}, indent=1))


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text()).get("stations", {})
    except (ValueError, OSError):
        return {}
