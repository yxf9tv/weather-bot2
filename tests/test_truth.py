import datetime as dt

import pytest

from weather_bot.scoring.settle import _in_range, _winning_bin
from weather_bot.weather.truth import hourly_max_from_obs

UTC = dt.timezone.utc


@pytest.mark.unit
def test_hourly_max_applies_minute_filter_and_local_day():
    # Oct 4 2026, America/New_York (EDT = UTC-4). 04:51Z = 00:51 local Oct 4.
    obs = [
        (dt.datetime(2026, 10, 4, 4, 51, tzinfo=UTC), 62.0),   # 00:51 local Oct 4 -> counts
        (dt.datetime(2026, 10, 4, 17, 25, tzinfo=UTC), 70.9),  # :25 special -> excluded
        (dt.datetime(2026, 10, 4, 17, 51, tzinfo=UTC), 63.4),  # counts, rounds to 63
        (dt.datetime(2026, 10, 5, 3, 51, tzinfo=UTC), 65.0),   # 23:51 local Oct 4 -> counts
        (dt.datetime(2026, 10, 5, 4, 51, tzinfo=UTC), 80.0),   # 00:51 local Oct 5 -> next day, excluded
        (dt.datetime(2026, 10, 4, 18, 2, tzinfo=UTC), 64.4),   # :02 -> counts (00-04 window), rounds to 64
    ]
    value, n = hourly_max_from_obs(obs, dt.date(2026, 10, 4), "America/New_York")
    assert (value, n) == (65, 4)


@pytest.mark.unit
def test_hourly_max_empty():
    assert hourly_max_from_obs([], dt.date(2026, 10, 4), "America/New_York") == (None, 0)


@pytest.mark.unit
def test_winning_bin_and_range_membership():
    bins = [{"label": "62° or below", "lo": None, "hi": 62}, {"label": "63° to 64°", "lo": 63, "hi": 64},
            {"label": "65° or above", "lo": 65, "hi": None}]
    assert _winning_bin(bins, 62)["label"] == "62° or below"
    assert _winning_bin(bins, 63)["label"] == "63° to 64°"
    assert _winning_bin(bins, 99)["label"] == "65° or above"
    assert _in_range(bins[1], 63, 70) and not _in_range(bins[2], 63, 70) and _in_range(bins[2], 63, None)
