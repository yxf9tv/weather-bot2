"""Settlement station table. Verified against live venue rules on 2026-10-05.

Kalshi settles on the NWS Daily Climate Report (CLI) value relayed by The Weather Company;
its rules name the CLI product id (CLINYC). Polymarket settles on the hourly max at the
ICAO named in its resolutionSource URL (site=klga). The two venues use different stations
for NYC, Chicago, Dallas and Denver.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    icao: str
    name: str
    lat: float
    lon: float
    tz: str
    cli_id: str | None  # NWS CLI product id (e.g. CLINYC) when a CLI exists


STATIONS: dict[str, Station] = {
    s.icao: s
    for s in (
        Station("KNYC", "New York Central Park", 40.7789, -73.9692, "America/New_York", "CLINYC"),
        Station("KLGA", "New York LaGuardia", 40.7772, -73.8726, "America/New_York", "CLILGA"),
        Station("KEWR", "Newark", 40.6895, -74.1745, "America/New_York", "CLIEWR"),
        Station("KMDW", "Chicago Midway", 41.7868, -87.7522, "America/Chicago", "CLIMDW"),
        Station("KORD", "Chicago O'Hare", 41.9742, -87.9073, "America/Chicago", "CLIORD"),
        Station("KDFW", "Dallas-Fort Worth", 32.8998, -97.0403, "America/Chicago", "CLIDFW"),
        Station("KDAL", "Dallas Love Field", 32.8471, -96.8518, "America/Chicago", None),
        Station("KDEN", "Denver International", 39.8561, -104.6737, "America/Denver", "CLIDEN"),
        Station("KBKF", "Denver Buckley SFB", 39.7017, -104.7517, "America/Denver", None),
        Station("KHOU", "Houston Hobby", 29.6454, -95.2789, "America/Chicago", "CLIHOU"),
        Station("KDCA", "Washington Reagan", 38.8512, -77.0402, "America/New_York", "CLIDCA"),
        Station("KBOS", "Boston Logan", 42.3656, -71.0096, "America/New_York", "CLIBOS"),
        Station("KPHL", "Philadelphia", 39.8729, -75.2437, "America/New_York", "CLIPHL"),
        Station("KATL", "Atlanta Hartsfield", 33.6407, -84.4277, "America/New_York", "CLIATL"),
        Station("KSEA", "Seattle-Tacoma", 47.4502, -122.3088, "America/Los_Angeles", "CLISEA"),
        Station("KSFO", "San Francisco", 37.6213, -122.3790, "America/Los_Angeles", "CLISFO"),
        Station("KPHX", "Phoenix Sky Harbor", 33.4342, -112.0116, "America/Phoenix", "CLIPHX"),
        Station("KLAX", "Los Angeles", 33.9416, -118.4085, "America/Los_Angeles", "CLILAX"),
        Station("KMIA", "Miami International", 25.7959, -80.2870, "America/New_York", "CLIMIA"),
        Station("KAUS", "Austin-Bergstrom", 30.1975, -97.6664, "America/Chicago", "CLIAUS"),
    )
}

CLI_TO_ICAO: dict[str, str] = {s.cli_id: s.icao for s in STATIONS.values() if s.cli_id}

# Kalshi series ticker -> expected CLI id (cross-checked against rules text at runtime)
KALSHI_SERIES_STATION: dict[str, str] = {
    "KXHIGHNY": "CLINYC", "KXHIGHCHI": "CLIMDW", "KXHIGHMIA": "CLIMIA", "KXHIGHAUS": "CLIAUS",
    "KXHIGHDEN": "CLIDEN", "KXHIGHLAX": "CLILAX", "KXHIGHPHIL": "CLIPHL", "KXHIGHTHOU": "CLIHOU",
    "KXHIGHTDAL": "CLIDFW", "KXHIGHTATL": "CLIATL", "KXHIGHTSEA": "CLISEA", "KXHIGHTSFO": "CLISFO",
    "KXHIGHTPHX": "CLIPHX", "KXHIGHTDC": "CLIDCA", "KXHIGHTBOS": "CLIBOS", "KXHIGHTEWR": "CLIEWR",
}

# Polymarket slug city -> expected ICAO (cross-checked against resolutionSource at runtime)
POLYMARKET_CITY_STATION: dict[str, str] = {
    "nyc": "KLGA", "chicago": "KORD", "dallas": "KDAL", "denver": "KBKF", "houston": "KHOU",
    "atlanta": "KATL", "seattle": "KSEA", "san-francisco": "KSFO", "los-angeles": "KLAX",
    "miami": "KMIA", "austin": "KAUS",
}


def station(icao: str) -> Station | None:
    return STATIONS.get(icao.upper())
