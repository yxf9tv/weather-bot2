import json
from pathlib import Path

import pytest

from weather_bot.scoring.settle import pnl_report
from weather_bot.storage.db import Database

KEY = "kalshi:KSEA:2026-10-27:cli_max"
LEGS = [{"instrument_id": "B56.5", "label": "56° to 57°"},
        {"instrument_id": "B58.5", "label": "58° to 59°"}]


@pytest.fixture
def db(tmp_path: Path):
    d = Database(tmp_path / "t.sqlite")
    yield d
    d.close()


def settle(db: Database, truth: float) -> str:
    db.conn.execute("INSERT INTO baskets(created_ts, venue, market_key, status, legs, intended_cost, filled_cost, qty)"
                    " VALUES (?,?,?,?,?,?,?,?)", ("2026-10-26T14:00:00+00:00", "kalshi", KEY, "complete",
                                                  json.dumps(LEGS), 0.58, 0.58, 1.0))
    db.conn.execute("INSERT INTO settlements(ts, venue, market_key, truth_value) VALUES (?,?,?,?)",
                    ("2026-10-28T14:00:00+00:00", "kalshi", KEY, truth))
    return pnl_report(db)


@pytest.mark.unit
@pytest.mark.parametrize("truth", [56.0, 57.0, 59.0])
def test_truth_inside_a_two_degree_leg_pays(db, truth):
    """Regression: Kalshi 2-degree bins never matched, so every Kalshi win showed $0 (2026-11-02)."""
    assert "payout $1.00 pnl $+0.42" in settle(db, truth)


@pytest.mark.unit
def test_truth_outside_every_leg_pays_nothing(db):
    assert "payout $0.00 pnl $-0.58" in settle(db, 60.0)
