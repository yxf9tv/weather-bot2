"""Risk limits and the kill switch. All state comes from the database."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

from ..config import Settings
from ..storage.db import Database

OPEN_STATUSES = ("resting", "partial", "complete", "completing", "unwinding", "held")


ABORT_COOLDOWN_MIN = 30


@dataclass(frozen=True)
class RiskSnapshot:
    open_risk: float
    risk_today: float
    exposure_by_key: dict[str, float]
    open_baskets_by_key: dict[str, int]
    recently_aborted_keys: frozenset[str] = frozenset()


def _basket_cost(row) -> float:
    """Money at risk: resting/partial = the full basket if everything fills; complete/completing/unwinding/held =
    what has actually been paid."""
    if row["status"] in ("complete", "completing", "unwinding", "held"):
        return float(row["filled_cost"] or 0.0)
    if row["status"] in ("unwound", "expired", "aborted"):
        return 0.0
    return float(row["intended_cost"] or 0.0)


def snapshot(db: Database, now: dt.datetime) -> RiskSnapshot:
    rows = db.conn.execute("SELECT * FROM baskets WHERE status IN (%s)" % ",".join("?" * len(OPEN_STATUSES)),
                           OPEN_STATUSES).fetchall()
    open_risk = sum(_basket_cost(r) for r in rows)
    exposure: dict[str, float] = {}
    count: dict[str, int] = {}
    for r in rows:
        exposure[r["market_key"]] = exposure.get(r["market_key"], 0.0) + _basket_cost(r)
        count[r["market_key"]] = count.get(r["market_key"], 0) + 1
    day_start = now.astimezone(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    today_rows = db.conn.execute("SELECT * FROM baskets WHERE created_ts >= ? AND status NOT IN ('aborted','expired')",
                                 (day_start,)).fetchall()
    since = (now - dt.timedelta(minutes=ABORT_COOLDOWN_MIN)).isoformat()
    aborted = db.conn.execute("SELECT DISTINCT market_key FROM baskets WHERE status='aborted' AND created_ts >= ?",
                              (since,)).fetchall()
    return RiskSnapshot(open_risk, sum(_basket_cost(r) for r in today_rows), exposure, count,
                        frozenset(r["market_key"] for r in aborted))


class KillSwitch:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    def is_tripped(self) -> str | None:
        if not self.settings.trading_enabled:
            return "TRADING_ENABLED=false"
        if self.settings.kill_file.exists():
            return "kill file present: " + (self.settings.kill_file.read_text().strip() or "manual")
        return None

    def trip(self, reason: str) -> None:
        self.settings.kill_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings.kill_file.write_text(f"{dt.datetime.now(dt.timezone.utc).isoformat()} {reason}\n")
        self.db.add_event("critical", "kill", f"trading disabled: {reason}")

    def reset(self) -> None:
        if self.settings.kill_file.exists():
            self.settings.kill_file.unlink()
        self.db.set("consecutive_errors", "0")
        self.db.add_event("info", "kill", "trading re-enabled")

    def record_error(self, where: str, exc: BaseException) -> None:
        n = int(self.db.get("consecutive_errors", "0") or 0) + 1
        self.db.set("consecutive_errors", str(n))
        self.db.add_event("error", where, repr(exc), {"consecutive": n})
        if n >= 3:
            self.trip(f"{n} consecutive API errors ({where}: {exc!r})")

    def record_ok(self) -> None:
        self.db.set("consecutive_errors", "0")


def allows(ev_cost: float, market_key: str, venue: str, snap: RiskSnapshot, settings: Settings,
           now: dt.datetime) -> str | None:
    if venue == "polymarket" and not settings.polymarket_allowed_on(now.date()):
        return "polymarket disabled by date flag"
    if snap.open_baskets_by_key.get(market_key, 0) > 0:
        return "already_exposed"
    if market_key in snap.recently_aborted_keys:
        return f"aborted within the last {ABORT_COOLDOWN_MIN} min"
    if snap.exposure_by_key.get(market_key, 0.0) + ev_cost > settings.max_city_date_exposure + 1e-9:
        return "city/date exposure cap"
    if snap.risk_today + ev_cost > settings.max_daily_new_risk + 1e-9:
        return f"daily new risk cap (${snap.risk_today:.2f} used)"
    if snap.open_risk + ev_cost > settings.max_total_open_risk + 1e-9:
        return f"total open risk cap (${snap.open_risk:.2f} open)"
    return None


def legs_json(legs) -> str:
    return json.dumps([{"instrument_id": l.bin.instrument_id, "label": l.bin.label, "prob": l.prob,
                        "bid": l.bid_price, "best_ask": l.best_ask} for l in legs])
