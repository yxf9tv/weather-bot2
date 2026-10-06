import datetime as dt
from pathlib import Path

import pytest

from weather_bot.config import Settings
from weather_bot.execution.risk import KillSwitch, allows, snapshot
from weather_bot.storage.db import Database

NOW = dt.datetime(2026, 10, 6, 12, tzinfo=dt.timezone.utc)


@pytest.fixture
def db(tmp_path: Path):
    d = Database(tmp_path / "t.sqlite")
    yield d
    d.close()


def settings(tmp_path: Path, **kw) -> Settings:
    return Settings(_env_file=None, kill_file=tmp_path / ".kill", **kw)


def add_basket(db, key, status, intended, filled=0.0, created=None):
    db.conn.execute("INSERT INTO baskets(created_ts, venue, market_key, status, legs, intended_cost, filled_cost, qty)"
                    " VALUES (?,?,?,?,?,?,?,?)", ((created or NOW).isoformat(), "kalshi", key, status, "[]",
                                                  intended, filled, 1.0))


@pytest.mark.unit
def test_snapshot_and_caps(db, tmp_path):
    s = settings(tmp_path, max_daily_new_risk=5.0, max_total_open_risk=10.0, max_city_date_exposure=5.0)
    add_basket(db, "kalshi:KNYC:2026-10-07:cli_max", "resting", 0.70)
    add_basket(db, "kalshi:KMDW:2026-10-07:cli_max", "complete", 0.60, filled=0.58)
    add_basket(db, "kalshi:KMIA:2026-10-07:cli_max", "expired", 0.90)
    snap = snapshot(db, NOW)
    assert snap.open_risk == pytest.approx(0.70 + 0.58)
    assert snap.risk_today == pytest.approx(0.70 + 0.58)
    assert allows(0.5, "kalshi:KNYC:2026-10-07:cli_max", "kalshi", snap, s, NOW) == "already_exposed"
    assert allows(0.5, "kalshi:KBOS:2026-10-07:cli_max", "kalshi", snap, s, NOW) is None
    assert "daily" in allows(4.0, "kalshi:KBOS:2026-10-07:cli_max", "kalshi", snap, s, NOW)


@pytest.mark.unit
def test_polymarket_date_flag(db, tmp_path):
    s = settings(tmp_path, polymarket_trading_until=dt.date(2026, 10, 1))
    snap = snapshot(db, NOW)
    assert "date flag" in allows(1.0, "polymarket:KLGA:2026-10-07:hourly_max", "polymarket", snap, s, NOW)
    assert allows(1.0, "kalshi:KNYC:2026-10-07:cli_max", "kalshi", snap, s, NOW) is None


@pytest.mark.unit
def test_kill_switch_trips_after_three_errors(db, tmp_path):
    s = settings(tmp_path)
    ks = KillSwitch(s, db)
    assert ks.is_tripped() is None
    ks.record_error("discover", RuntimeError("boom"))
    ks.record_error("discover", RuntimeError("boom"))
    assert ks.is_tripped() is None
    ks.record_error("discover", RuntimeError("boom"))
    assert "consecutive" in ks.is_tripped()
    ks.reset()
    assert ks.is_tripped() is None
    ks.record_ok()
    s2 = settings(tmp_path, trading_enabled=False)
    assert KillSwitch(s2, db).is_tripped() == "TRADING_ENABLED=false"


@pytest.mark.unit
def test_recent_abort_cooldown(db, tmp_path):
    s = settings(tmp_path)
    add_basket(db, "polymarket:KLGA:2026-10-07:hourly_max", "aborted", 4.96, created=NOW - dt.timedelta(minutes=5))
    add_basket(db, "polymarket:KSEA:2026-10-07:hourly_max", "aborted", 3.45, created=NOW - dt.timedelta(minutes=45))
    snap = snapshot(db, NOW)
    assert "aborted" in allows(4.96, "polymarket:KLGA:2026-10-07:hourly_max", "polymarket", snap, s, NOW)
    assert allows(3.45, "polymarket:KSEA:2026-10-07:hourly_max", "polymarket", snap, s, NOW) is None
