"""SQLite store. Every opportunity, forecast, book, order and settlement lands here."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, data TEXT);
CREATE TABLE IF NOT EXISTS market_snapshots (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, venue TEXT NOT NULL, event_id TEXT NOT NULL, market_key TEXT NOT NULL,
  station TEXT, target_date TEXT, quantity TEXT, confident INTEGER, problems TEXT, hours_to_target REAL,
  sum_of_asks REAL, bins TEXT NOT NULL, rules_text TEXT);
CREATE INDEX IF NOT EXISTS ix_snap_key ON market_snapshots(market_key, ts);
CREATE TABLE IF NOT EXISTS books (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, venue TEXT NOT NULL, instrument_id TEXT NOT NULL,
  bids TEXT NOT NULL, asks TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forecasts (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, source TEXT NOT NULL, station TEXT NOT NULL, target_date TEXT NOT NULL,
  issued_at TEXT, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_fc ON forecasts(source, station, target_date, ts);
CREATE TABLE IF NOT EXISTS opportunities (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, venue TEXT NOT NULL, market_key TEXT NOT NULL, event_id TEXT NOT NULL,
  station TEXT NOT NULL, target_date TEXT NOT NULL, quantity TEXT NOT NULL, hours_to_target REAL,
  range_lo INTEGER, range_hi INTEGER, n_bins INTEGER, bins TEXT NOT NULL,
  prob_nbm REAL, prob_openmeteo REAL, prob_used REAL, cost_bids REAL, cost_asks REAL, fees REAL,
  net_edge REAL, expected_profit REAL, expected_roi REAL, confidence TEXT,
  decision TEXT NOT NULL, reason TEXT, dist_nbm TEXT, dist_openmeteo TEXT, nws_max REAL, qty REAL);
CREATE INDEX IF NOT EXISTS ix_opp_key ON opportunities(market_key, ts);
CREATE TABLE IF NOT EXISTS baskets (
  id INTEGER PRIMARY KEY, created_ts TEXT NOT NULL, venue TEXT NOT NULL, market_key TEXT NOT NULL,
  opportunity_id INTEGER, status TEXT NOT NULL, legs TEXT NOT NULL, intended_cost REAL, filled_cost REAL,
  qty REAL, updated_ts TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, venue TEXT NOT NULL, basket_id INTEGER, instrument_id TEXT NOT NULL,
  client_id TEXT NOT NULL, venue_order_id TEXT, side TEXT NOT NULL, price REAL NOT NULL, qty REAL NOT NULL,
  post_only INTEGER, status TEXT NOT NULL, filled_qty REAL DEFAULT 0, avg_fill_price REAL, fee REAL, raw TEXT,
  updated_ts TEXT);
CREATE TABLE IF NOT EXISTS settlements (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, venue TEXT NOT NULL, market_key TEXT NOT NULL UNIQUE,
  truth_value REAL, truth_source TEXT, venue_result TEXT, winning_bin TEXT, agree INTEGER, notes TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---- events ----
    def add_event(self, level: str, kind: str, text: str, data: dict | None = None) -> None:
        self.conn.execute("INSERT INTO events(ts, level, kind, text, data) VALUES (?,?,?,?,?)",
                          (_now(), level, kind, text, json.dumps(data) if data else None))

    # ---- market snapshots ----
    def add_market_snapshot(self, market, hours_to_target: float | None) -> None:
        bins = [{"label": b.label, "lo": b.lo, "hi": b.hi, "id": b.instrument_id,
                 "bid": b.yes_bid, "ask": b.yes_ask, "ask_size": b.ask_size} for b in market.sorted_bins()]
        self.conn.execute(
            "INSERT INTO market_snapshots(ts, venue, event_id, market_key, station, target_date, quantity, confident,"
            " problems, hours_to_target, sum_of_asks, bins, rules_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), market.venue, market.event_id, market.key, market.station_icao, market.target_date.isoformat(),
             market.quantity, int(market.rules.confident), json.dumps(list(market.rules.problems)),
             hours_to_target, market.sum_of_asks(), json.dumps(bins), market.raw_rules_text))

    def add_book(self, venue: str, book) -> None:
        self.conn.execute("INSERT INTO books(ts, venue, instrument_id, bids, asks) VALUES (?,?,?,?,?)",
                          (book.fetched_at.isoformat(timespec="seconds"), venue, book.instrument_id,
                           json.dumps([[l.price, l.size] for l in book.bids]),
                           json.dumps([[l.price, l.size] for l in book.asks])))

    # ---- forecasts ----
    def add_forecast(self, source: str, station: str, target_date: dt.date, issued_at: str | None, payload: dict) -> None:
        self.conn.execute("INSERT INTO forecasts(ts, source, station, target_date, issued_at, payload) VALUES (?,?,?,?,?,?)",
                          (_now(), source, station, target_date.isoformat(), issued_at, json.dumps(payload)))

    def latest_forecast(self, source: str, station: str, target_date: dt.date) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM forecasts WHERE source=? AND station=? AND target_date=? ORDER BY ts DESC LIMIT 1",
            (source, station, target_date.isoformat())).fetchone()
        return dict(row) | {"payload": json.loads(row["payload"])} if row else None

    # ---- opportunities ----
    def add_opportunity(self, row: dict) -> int:
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        cur = self.conn.execute(f"INSERT INTO opportunities(ts, {cols}) VALUES (?, {marks})", (_now(), *row.values()))
        return int(cur.lastrowid)

    # ---- kv ----
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO kv(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
