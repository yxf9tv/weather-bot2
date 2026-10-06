import datetime as dt
from pathlib import Path

import pytest

from weather_bot.markets.model import Bin
from weather_bot.weather.distribution import from_members, from_percentiles
from weather_bot.weather.nbm import MaxTPercentiles, candidate_cycles, parse_nbp
from weather_bot.weather.nws import parse_max_temps
from weather_bot.weather.openmeteo import parse_ensemble

FIX = Path(__file__).parent / "fixtures"
NBP = (FIX / "nbp_20261005_19z_subset.txt").read_text()


@pytest.mark.unit
def test_parse_nbp_knyc_oct6():
    out = parse_nbp(NBP, {"KNYC"})
    rows = {r.target_date: r for r in out["KNYC"]}
    r = rows[dt.date(2026, 10, 6)]
    assert (r.mean, r.sd, r.p10, r.p25, r.p50, r.p75, r.p90) == (62, 2, 61, 62, 62, 63, 64)
    assert r.cycle == dt.datetime(2026, 10, 5, 19, tzinfo=dt.timezone.utc)
    assert rows[dt.date(2026, 10, 7)].mean == 68


@pytest.mark.unit
def test_parse_nbp_all_20_stations_and_kbkf():
    out = parse_nbp(NBP)
    assert len(out) == 20
    kbkf = {r.target_date: r for r in out["KBKF"]}[dt.date(2026, 10, 6)]
    assert kbkf.mean == 86 and kbkf.p10 == 84 and kbkf.p90 == 88
    klga = {r.target_date: r for r in out["KLGA"]}[dt.date(2026, 10, 6)]
    assert klga.mean == 63  # LGA runs warmer than Central Park (62)


@pytest.mark.unit
def test_candidate_cycles_skip_too_fresh():
    now = dt.datetime(2026, 10, 5, 19, 30, tzinfo=dt.timezone.utc)
    cycles = candidate_cycles(now)
    assert cycles[0].hour == 13  # 19Z not ready yet (needs ~50 min)
    now2 = dt.datetime(2026, 10, 5, 20, 10, tzinfo=dt.timezone.utc)
    assert candidate_cycles(now2)[0].hour == 19


@pytest.mark.unit
def test_percentile_distribution_sums_to_one_and_centers():
    pct = MaxTPercentiles("KNYC", dt.date(2026, 10, 6), dt.datetime(2026, 10, 5, 19, tzinfo=dt.timezone.utc),
                          62, 2, 61, 62, 62, 63, 64)
    d = from_percentiles(pct)
    assert abs(sum(d.pmf.values()) - 1.0) < 1e-9
    assert abs(d.mean - 62) < 0.6
    assert d.prob_range(None, 62) > 0.45 and d.prob_range(63, 64) > 0.25
    bins = (Bin("62° or below", None, 62, "a"), Bin("63° to 64°", 63, 64, "b"), Bin("65° or above", 65, None, "c"))
    probs = d.bin_probabilities(bins)
    assert abs(sum(probs.values()) - 1.0) < 1e-9


@pytest.mark.unit
def test_percentile_offset_and_inflation_shift_mass():
    pct = MaxTPercentiles("K", dt.date(2026, 1, 1), dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                          70, 2, 68, 69, 70, 71, 72)
    base = from_percentiles(pct)
    shifted = from_percentiles(pct, offset_f=-1.0)
    wider = from_percentiles(pct, inflation=1.5)
    assert abs(shifted.mean - (base.mean - 1.0)) < 0.05
    assert wider.sd > base.sd * 1.2


@pytest.mark.unit
def test_member_distribution():
    members = [60.0] * 20 + [62.0] * 60 + [64.0] * 20
    d = from_members(members)
    assert abs(sum(d.pmf.values()) - 1.0) < 1e-9
    assert abs(d.mean - 62.0) < 0.2
    assert d.sd > 1.0


@pytest.mark.unit
def test_parse_ensemble_groups_members_by_model():
    payload = {"daily": {"time": ["2026-10-06", "2026-10-07"],
                         "temperature_2m_max_gfs_seamless": [60.0, 65.0],
                         "temperature_2m_max_member01_gfs_seamless": [61.0, None],
                         "temperature_2m_max_ecmwf_ifs025": [62.0, 66.0]}}
    days = parse_ensemble("KLGA", payload)
    assert days[0].members == {"gfs_seamless": [60.0, 61.0], "ecmwf_ifs025": [62.0]}
    assert days[1].members == {"gfs_seamless": [65.0], "ecmwf_ifs025": [66.0]}


@pytest.mark.unit
def test_parse_nws_max_temps_buckets_by_local_date():
    payload = {"properties": {"maxTemperature": {"values": [
        {"validTime": "2026-10-06T12:00:00+00:00/PT13H", "value": 16.67},
        {"validTime": "2026-10-07T12:00:00+00:00/PT13H", "value": 20.0}]}}}
    out = parse_max_temps(payload, "America/New_York")
    assert round(out[dt.date(2026, 10, 6)]) == 62 and out[dt.date(2026, 10, 7)] == 68
