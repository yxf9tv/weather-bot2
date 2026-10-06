import datetime as dt
import json
from pathlib import Path

import pytest

from weather_bot.config import Settings
from weather_bot.markets.rules import parse_bin_label, parse_kalshi_rules, parse_polymarket_rules
from weather_bot.venues.kalshi import KalshiVenue, kalshi_taker_fee
from weather_bot.venues.polymarket import PolymarketVenue, polymarket_min_qty, polymarket_taker_fee

FIX = Path(__file__).parent / "fixtures"


@pytest.mark.unit
def test_kalshi_rules_parse():
    text = ("If the maximum temperature recorded at New York City (CLINYC) for Oct 6, 2026, is greater than 70° "
            "fahrenheit according to The Weather Company, then the market resolves to Yes.")
    r = parse_kalshi_rules(text)
    assert r.confident and r.station_icao == "KNYC" and r.target_date == dt.date(2026, 10, 6)
    assert r.quantity == "cli_max" and r.unit == "F"


@pytest.mark.unit
def test_kalshi_rules_reject_minimum_and_unknown_station():
    r = parse_kalshi_rules("If the minimum temperature recorded at Nowhere (CLIXXX) for Oct 6, 2026, is less than 50° "
                           "fahrenheit according to The Weather Company, then the market resolves to Yes.")
    assert not r.confident and any("minimum" in p for p in r.problems) and any("CLIXXX" in p for p in r.problems)


@pytest.mark.unit
def test_polymarket_rules_parse():
    desc = ("This market will resolve to the temperature range that contains the highest temperature recorded by NOAA "
            "at the LaGuardia Airport Station in degrees Fahrenheit on 5 Oct '26.\n\nThis market will resolve off of "
            "the Hourly Data provided using the \"Show Hourly Data\" button.")
    r = parse_polymarket_rules(desc, "https://www.weather.gov/wrh/timeseries?site=klga",
                               "highest-temperature-in-nyc-on-october-5-2026")
    assert r.confident and r.station_icao == "KLGA" and r.target_date == dt.date(2026, 10, 5)
    assert r.quantity == "hourly_max"


@pytest.mark.unit
@pytest.mark.parametrize("label,lo,hi", [
    ("62° or below", None, 62), ("63° to 64°", 63, 64), ("71° or above", 71, None),
    ("61°F or below", None, 61), ("62-63°F", 62, 63), ("80°F or higher", 80, None), ("100° or above", 100, None),
])
def test_bin_labels(label, lo, hi):
    b = parse_bin_label(label, "x")
    assert b is not None and (b.lo, b.hi) == (lo, hi)


@pytest.mark.unit
def test_bin_label_garbage_returns_none():
    assert parse_bin_label("hot", "x") is None


@pytest.mark.unit
def test_kalshi_fixture_builds_contiguous_markets():
    settings = Settings(_env_file=None)
    v = KalshiVenue(settings, client=None)
    rows = json.loads((FIX / "kalshi_markets_kxhighny.json").read_text())["markets"]
    by_event = {}
    for m in rows:
        by_event.setdefault(m["event_ticker"], []).append(m)
    for ev, group in by_event.items():
        wm = v._build_market("KXHIGHNY", ev, group)
        assert wm is not None and wm.station_icao == "KNYC" and wm.quantity == "cli_max"
        assert len(wm.bins) == 6 and wm.bins_are_contiguous(), wm.rules.problems
        assert wm.rules.confident, wm.rules.problems
        assert wm.key.startswith("kalshi:KNYC:")


@pytest.mark.unit
def test_polymarket_fixture_builds_markets_with_expected_stations():
    settings = Settings(_env_file=None)
    v = PolymarketVenue(settings, client=None)
    events = json.loads((FIX / "gamma_us_highs.json").read_text())
    built = [v.build_market(e) for e in events]
    built = [b for b in built if b is not None]
    assert len(built) == len(events) > 0
    stations = {b.city.split()[0]: b.station_icao for b in built}
    by_slug = {b.event_id: b for b in built}
    nyc = next(b for s, b in by_slug.items() if "-nyc-" in s)
    assert nyc.station_icao == "KLGA" and nyc.quantity == "hourly_max" and len(nyc.bins) == 11
    assert nyc.bins_are_contiguous() and nyc.rules.confident, nyc.rules.problems
    den = next(b for s, b in by_slug.items() if "-denver-" in s)
    assert den.station_icao == "KBKF"
    chi = next(b for s, b in by_slug.items() if "-chicago-" in s)
    assert chi.station_icao == "KORD"


@pytest.mark.unit
def test_kalshi_fee_rounds_up_to_cent():
    assert kalshi_taker_fee(0.42, 1) == 0.02   # 0.0171 -> 0.02
    assert kalshi_taker_fee(0.03, 1) == 0.01   # 0.00204 -> 0.01
    assert kalshi_taker_fee(0.50, 10) == 0.18  # 0.175 -> 0.18
    assert kalshi_taker_fee(0.50, 100) == 1.75


@pytest.mark.unit
def test_polymarket_fee_and_min_qty():
    assert polymarket_taker_fee(0.15, 10) == pytest.approx(0.06375, abs=1e-5)
    assert polymarket_min_qty(0.15) == 7     # ceil(1/0.15)=7 > 5
    assert polymarket_min_qty(0.50) == 5     # 5 shares, $2.50 notional
    assert polymarket_min_qty(0.01) == 100
