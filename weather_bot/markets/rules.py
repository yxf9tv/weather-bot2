"""Parse venue rules text into a RulesParse. Anything uncertain -> confident=False."""

from __future__ import annotations

import datetime as dt
import re

from .model import Bin, RulesParse
from .stations import CLI_TO_ICAO, STATIONS

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

# Kalshi: "If the maximum temperature recorded at New York City (CLINYC) for Oct 6, 2026, is
# greater than 70° fahrenheit according to The Weather Company, then the market resolves to Yes."
_KALSHI_RE = re.compile(
    r"(?P<measure>maximum|minimum) temperature recorded at (?P<name>.+?) \((?P<cli>CLI[A-Z0-9]{2,4})\) "
    r"for (?P<mon>[A-Z][a-z]{2})[a-z]* (?P<day>\d{1,2}), (?P<year>\d{4}),.*?"
    r"(?P<unit>fahrenheit|celsius).*?according to (?P<source>The Weather Company|National Weather Service)",
    re.S,
)

# Polymarket description: "...highest temperature recorded by NOAA at the LaGuardia Airport Station
# in degrees Fahrenheit on 5 Oct '26." resolutionSource: ".../timeseries?site=klga"
_POLY_RE = re.compile(
    r"(?P<measure>highest|lowest) temperature recorded(?: by NOAA)? at the (?P<name>.+?) in degrees "
    r"(?P<unit>Fahrenheit|Celsius)",
    re.S,
)
_POLY_SITE_RE = re.compile(r"timeseries\?site=(?P<site>[a-z0-9]{3,4})", re.I)
_POLY_SLUG_DATE_RE = re.compile(r"-on-(?P<mon>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})$")
_POLY_HOURLY_RE = re.compile(r"Show Hourly Data", re.I)


def parse_kalshi_rules(rules_primary: str) -> RulesParse:
    m = _KALSHI_RE.search(rules_primary or "")
    problems: list[str] = []
    if not m:
        return RulesParse(None, None, None, None, None, None, False, ("rules text did not match",))
    if m["measure"] != "maximum":
        problems.append(f"measure is {m['measure']}")
    cli = m["cli"]
    icao = CLI_TO_ICAO.get(cli)
    if icao is None:
        problems.append(f"unknown CLI id {cli}")
    try:
        date = dt.date(int(m["year"]), _MONTHS[m["mon"].lower()], int(m["day"]))
    except (KeyError, ValueError):
        date = None
        problems.append("date unparseable")
    unit = "F" if m["unit"] == "fahrenheit" else "C"
    if unit != "F":
        problems.append("unit is not Fahrenheit")
    source = m["source"]
    if source != "The Weather Company":
        problems.append(f"source is {source}")
    return RulesParse(icao, f"{m['name']} ({cli})", date, unit, "cli_max", source,
                      confident=not problems, problems=tuple(problems))


def parse_polymarket_rules(description: str, resolution_source: str | None, slug: str) -> RulesParse:
    problems: list[str] = []
    m = _POLY_RE.search(description or "")
    if not m:
        return RulesParse(None, None, None, None, None, None, False, ("description did not match",))
    if m["measure"] != "highest":
        problems.append(f"measure is {m['measure']}")
    unit = "F" if m["unit"] == "Fahrenheit" else "C"
    if unit != "F":
        problems.append("unit is not Fahrenheit")
    site = _POLY_SITE_RE.search(resolution_source or "") or _POLY_SITE_RE.search(description or "")
    icao = None
    if site:
        icao = site["site"].upper()
        if icao not in STATIONS:
            problems.append(f"unknown station {icao}")
    else:
        problems.append("no weather.gov timeseries site in resolution source")
    if not _POLY_HOURLY_RE.search(description or ""):
        problems.append("description does not bind to 'Show Hourly Data'")
    sd = _POLY_SLUG_DATE_RE.search(slug or "")
    date = None
    if sd:
        try:
            date = dt.date(int(sd["year"]), _MONTHS[sd["mon"][:3].lower()], int(sd["day"]))
        except (KeyError, ValueError):
            problems.append("slug date unparseable")
    else:
        problems.append("slug has no date")
    return RulesParse(icao, m["name"], date, unit, "hourly_max", "weather.gov timeseries",
                      confident=not problems, problems=tuple(problems))


# Bin labels. Kalshi: "62° or below" | "63° to 64°" | "71° or above"
# Polymarket: "61°F or below" | "62-63°F" | "80°F or higher"
_BELOW_RE = re.compile(r"^(?P<t>-?\d+)°?F? or below$")
_ABOVE_RE = re.compile(r"^(?P<t>-?\d+)°?F? or (?:above|higher)$")
_RANGE_RE = re.compile(r"^(?P<lo>-?\d+)°?F?\s*(?:to|-)\s*(?P<hi>-?\d+)°?F?$")


def parse_bin_label(label: str, instrument_id: str) -> Bin | None:
    text = label.strip()
    if m := _BELOW_RE.match(text):
        return Bin(text, None, int(m["t"]), instrument_id)
    if m := _ABOVE_RE.match(text):
        return Bin(text, int(m["t"]), None, instrument_id)
    if m := _RANGE_RE.match(text):
        lo, hi = int(m["lo"]), int(m["hi"])
        if lo > hi:
            return None
        return Bin(text, lo, hi, instrument_id)
    return None
