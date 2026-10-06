"""Kalshi venue: public discovery/books now, signed trading later.

Signing scheme ported from ~/projects/live_sports_latency_bot/sports_research/transport.py:
sign(timestamp_ms + METHOD + /trade-api/v2/path) with RSA-PSS SHA256 or Ed25519.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import math
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from ..markets.model import Bin, WeatherMarket
from ..markets.rules import parse_bin_label, parse_kalshi_rules
from ..markets.stations import KALSHI_SERIES_STATION, STATIONS
from .base import BookLevel, OrderBook, OrderResult

REST_PREFIX = "/trade-api/v2"


def _parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _sign(key: Any, timestamp_ms: str, method: str, path: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding

    message = f"{timestamp_ms}{method}{path}".encode()
    if isinstance(key, ed25519.Ed25519PrivateKey):
        sig = key.sign(message)
    else:
        sig = key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                             salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    return base64.b64encode(sig).decode("ascii")


def kalshi_taker_fee(price: float, qty: float, multiplier: float = 1.0) -> float:
    """Kalshi quadratic fee, rounded UP to the next cent per order."""
    raw = 0.07 * multiplier * qty * price * (1.0 - price)
    return math.ceil(raw * 100 - 1e-9) / 100


class KalshiVenue:
    name = "kalshi"
    tick = 0.01

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.host = settings.kalshi_host.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=20.0, headers={"Accept": "application/json"})
        self._private_key = None

    # ---------- auth ----------
    def _load_key(self):
        if self._private_key is None:
            from cryptography.hazmat.primitives import serialization

            path = Path(self.settings.kalshi_private_key_path)
            if not path.is_absolute():
                path = Path(__file__).resolve().parents[2] / path
            self._private_key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        return self._private_key

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if not self.settings.kalshi_api_key:
            raise RuntimeError("KALSHI_API_KEY is not set")
        stamp = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.settings.kalshi_api_key,
            "KALSHI-ACCESS-TIMESTAMP": stamp,
            "KALSHI-ACCESS-SIGNATURE": _sign(self._load_key(), stamp, method, REST_PREFIX + path),
        }

    async def _get(self, path: str, params: dict | None = None, *, auth: bool = False) -> dict:
        headers = self._auth_headers("GET", path) if auth else {}
        r = await self.client.get(self.host + path, params=params, headers=headers)
        r.raise_for_status()
        return r.json()

    async def _send(self, method: str, path: str, body: dict | None) -> tuple[int, dict]:
        headers = self._auth_headers(method, path)
        r = await self.client.request(method, self.host + path, json=body, headers=headers)
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        return r.status_code, payload

    # ---------- discovery ----------
    async def discover(self) -> list[WeatherMarket]:
        markets: list[WeatherMarket] = []
        for series in self.settings.kalshi_series:
            data = await self._get("/markets", {"series_ticker": series, "status": "open", "limit": 100})
            by_event: dict[str, list[dict]] = {}
            for m in data.get("markets", []):
                by_event.setdefault(m["event_ticker"], []).append(m)
            for event_ticker, rows in by_event.items():
                wm = self._build_market(series, event_ticker, rows)
                if wm is not None:
                    markets.append(wm)
        return markets

    def _build_market(self, series: str, event_ticker: str, rows: list[dict]) -> WeatherMarket | None:
        rules_text = rows[0].get("rules_primary") or ""
        rules = parse_kalshi_rules(rules_text)
        problems = list(rules.problems)
        expected_cli = KALSHI_SERIES_STATION.get(series)
        if expected_cli and rules.station_text and f"({expected_cli})" not in rules.station_text:
            problems.append(f"series {series} expected {expected_cli}, rules say {rules.station_text}")
        bins: list[Bin] = []
        for m in rows:
            label = m.get("yes_sub_title") or m.get("subtitle") or ""
            b = parse_bin_label(label, m["ticker"])
            if b is None:
                problems.append(f"unparsed bin label {label!r}")
                continue
            ask = _f(m.get("yes_ask_dollars"))
            ask_size = _f(m.get("yes_ask_size_fp"))
            if ask is not None and ask >= 1.0 and not ask_size:
                ask = None
            bins.append(b.with_quote(yes_bid=_f(m.get("yes_bid_dollars")) or None, yes_ask=ask,
                                     ask_size=ask_size, bid_size=_f(m.get("yes_bid_size_fp"))))
        from dataclasses import replace

        rules = replace(rules, confident=rules.confident and not problems, problems=tuple(problems))
        if rules.station_icao is None or rules.target_date is None:
            icao = STATIONS.get(rules.station_icao or "") or None
            return WeatherMarket("kalshi", event_ticker, series, rules.station_icao or "?",
                                 rules.target_date or dt.date(1970, 1, 1), icao.tz if icao else "UTC",
                                 "cli_max", "F", rules, tuple(bins), raw_rules_text=rules_text)
        st = STATIONS[rules.station_icao]
        wm = WeatherMarket(
            venue="kalshi", event_id=event_ticker, city=st.name, station_icao=st.icao,
            target_date=rules.target_date, timezone=st.tz, quantity="cli_max", unit="F",
            rules=rules, bins=tuple(bins), opens_at=_parse_ts(rows[0].get("open_time")),
            closes_at=_parse_ts(rows[0].get("close_time")), raw_rules_text=rules_text,
            extra={"series": series, "fee_type": "quadratic"},
        )
        if not wm.bins_are_contiguous():
            from dataclasses import replace as _r

            wm = _r(wm, rules=_r(wm.rules, confident=False,
                                 problems=wm.rules.problems + ("bins not contiguous/exhaustive",)))
        return wm

    # ---------- books ----------
    async def orderbook(self, instrument_id: str) -> OrderBook:
        data = await self._get(f"/markets/{instrument_id}/orderbook", {"depth": 20})
        ob = data.get("orderbook_fp") or data.get("orderbook") or {}
        yes = [(float(p), float(s)) for p, s in (ob.get("yes_dollars") or [])]
        no = [(float(p), float(s)) for p, s in (ob.get("no_dollars") or [])]
        bids = tuple(BookLevel(p, s) for p, s in sorted(yes, key=lambda x: -x[0]))
        asks = tuple(BookLevel(round(1.0 - p, 4), s) for p, s in sorted(no, key=lambda x: -x[0]))
        return OrderBook(instrument_id, bids, asks, dt.datetime.now(dt.timezone.utc))

    # ---------- fees / sizing ----------
    def taker_fee(self, price: float, qty: float) -> float:
        return kalshi_taker_fee(price, qty)

    def maker_fee(self, price: float, qty: float) -> float:
        return 0.0

    def min_qty(self, price: float) -> float:
        return 1.0

    # ---------- trading (V2 order API) ----------
    async def place_limit(self, instrument_id: str, price: float, qty: float, *, side: str = "buy",
                          post_only: bool = True, ioc: bool = False, client_id: str) -> OrderResult:
        body = {
            "ticker": instrument_id,
            "side": "bid" if side == "buy" else "ask",   # V2 quotes the YES book
            "count": f"{qty:.2f}",
            "price": f"{price:.4f}",
            "time_in_force": "immediate_or_cancel" if ioc else "good_till_canceled",
            "post_only": bool(post_only and not ioc),
            "self_trade_prevention_type": "maker",
            "cancel_order_on_pause": True,
            "client_order_id": client_id,
        }
        status, payload = await self._send("POST", "/portfolio/events/orders", body)
        if status not in (200, 201):
            return OrderResult("", "rejected", 0.0, None, {"http_status": status, **payload})
        return _order_result(payload.get("order", payload), qty)

    async def cancel(self, order_id: str) -> None:
        await self._send("DELETE", f"/portfolio/orders/{order_id}", None)

    async def order_status(self, order_id: str) -> OrderResult:
        status, payload = await self._send("GET", f"/portfolio/orders/{order_id}", None)
        if status != 200:
            return OrderResult(order_id, "unknown", 0.0, None, {"http_status": status, **payload})
        order = payload.get("order", payload)
        return _order_result(order, _f(order.get("initial_count_fp") or order.get("initial_count")) or 0.0)

    async def positions(self) -> list[dict]:
        data = await self._get("/portfolio/positions", {"limit": 200}, auth=True)
        return data.get("market_positions", [])

    async def balance(self) -> float:
        data = await self._get("/portfolio/balance", auth=True)
        if "balance_dollars" in data:
            return float(data["balance_dollars"])
        return float(data.get("balance", 0)) / 100.0


def _order_result(order: dict, qty: float) -> OrderResult:
    filled = _f(order.get("fill_count_fp") or order.get("fill_count")) or 0.0
    remaining = _f(order.get("remaining_count_fp") or order.get("remaining_count"))
    vstatus = (order.get("status") or "").lower()
    if vstatus in ("canceled", "cancelled"):
        st = "cancelled"
    elif vstatus == "executed" or (qty and filled >= qty - 1e-9) or remaining == 0 and filled > 0:
        st = "filled"
    elif filled > 0:
        st = "partial"
    elif vstatus == "resting" or remaining:
        st = "resting"
    else:
        st = vstatus or "unknown"
    avg = _f(order.get("average_fill_price_dollars") or order.get("average_fill_price"))
    if avg is not None and avg > 1.0:
        avg = avg / 100.0
    return OrderResult(str(order.get("order_id", "")), st, filled, avg, order)


def _f(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_fixture_markets(path: Path) -> list[dict]:
    return json.loads(Path(path).read_text())["markets"]
