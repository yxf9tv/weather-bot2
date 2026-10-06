"""NOAA National Blend of Models text bulletins (NBP probabilistic, NBS short-range).

NBP gives, per station, the quantile-mapped MaxT distribution: TXNMN mean, TXNSD sd and the
10/25/50/75/90th percentiles. The max for target day D sits in the 00Z column under day D+1
(window 12Z D to 06Z D+1). Full NBP cycles: 01/07/13/19Z; partial: 00/12Z.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

import httpx

AWS_BASE = "https://noaa-nbm-grib2-pds.s3.amazonaws.com"
NBP_CYCLES = (1, 7, 13, 19)
NBP_PARTIAL_CYCLES = (0, 12)
_HEADER_RE = re.compile(r"^ ?(?P<st>[A-Z0-9]{3,5}) +NBM V(?P<ver>[0-9.]+) (?P<prod>NBP|NBS|NBE) GUIDANCE +"
                        r"(?P<mm>\d{2})/(?P<dd>\d{2})/(?P<yyyy>\d{4}) +(?P<hh>\d{2})00 UTC")
_DAY_RE = re.compile(r"(?P<dow>[A-Z]{3}) (?P<day>\d{2})")
_ROW_RE = re.compile(r"^[A-Z][A-Z0-9]{2,4} ")          # data row label (AFOS layout, no leading blank)
_DAY_LINE_RE = re.compile(r"^[A-Z]{3} \d{2}\|")          # day header line (AFOS layout)


@dataclass(frozen=True)
class MaxTPercentiles:
    station: str
    target_date: dt.date
    cycle: dt.datetime
    mean: float
    sd: float
    p10: float
    p25: float
    p50: float
    p75: float
    p90: float

    def as_dict(self) -> dict:
        return {"station": self.station, "target_date": self.target_date.isoformat(),
                "cycle": self.cycle.isoformat(), "mean": self.mean, "sd": self.sd,
                "p10": self.p10, "p25": self.p25, "p50": self.p50, "p75": self.p75, "p90": self.p90}


def nbp_url(cycle: dt.datetime) -> str:
    return f"{AWS_BASE}/blend.{cycle:%Y%m%d}/{cycle:%H}/text/blend_nbptx.t{cycle:%H}z"


def candidate_cycles(now: dt.datetime, lookback_hours: int = 14, full_only: bool = True) -> list[dt.datetime]:
    """Newest first. Bulletins land ~55–75 min after the cycle hour."""
    base = now.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    allowed = NBP_CYCLES if full_only else NBP_CYCLES + NBP_PARTIAL_CYCLES
    out = []
    for h in range(0, lookback_hours + 1):
        c = base - dt.timedelta(hours=h)
        if c.hour in allowed and now - c >= dt.timedelta(minutes=50):
            out.append(c)
    return out


async def fetch_latest_nbp(client: httpx.AsyncClient, now: dt.datetime | None = None,
                           skip_cycles: set[dt.datetime] | None = None) -> tuple[dt.datetime, str] | None:
    now = now or dt.datetime.now(dt.timezone.utc)
    for cycle in candidate_cycles(now, full_only=False):
        if skip_cycles and cycle in skip_cycles:
            return None
        r = await client.get(nbp_url(cycle))
        if r.status_code == 200 and len(r.text) > 100_000:
            return cycle, r.text
    return None


def _normalise(ln: str) -> str:
    """IEM AFOS copies drop the leading blanks of the AWS layout; restore them."""
    if not ln or ln.startswith(" "):
        return ln
    if _DAY_LINE_RE.match(ln):
        return "    " + ln
    if _HEADER_RE.match(ln) or _ROW_RE.match(ln):
        return " " + ln
    return ln


def _split_groups(line: str) -> list[list[str]]:
    """Split a bulletin row into per-day groups of whitespace-separated cells (6-char label removed)."""
    groups = line.split("|")
    groups[0] = groups[0][6:]
    return [g.split() for g in groups]


def parse_nbp(text: str, stations: set[str] | None = None) -> dict[str, list[MaxTPercentiles]]:
    """Return {station: [MaxTPercentiles per target date]} for the requested stations."""
    out: dict[str, list[MaxTPercentiles]] = {}
    lines = [_normalise(ln) for ln in text.splitlines()]
    i = 0
    while i < len(lines):
        m = _HEADER_RE.match(lines[i])
        if not m or (stations and m["st"] not in stations) or m["prod"] != "NBP":
            i += 1
            continue
        cycle = dt.datetime(int(m["yyyy"]), int(m["mm"]), int(m["dd"]), int(m["hh"]), tzinfo=dt.timezone.utc)
        block: dict[str, str] = {}
        day_line = None
        j = i + 1
        while j < len(lines) and lines[j].strip() and not _HEADER_RE.match(lines[j]):
            row = lines[j]
            label = row[:6].strip()
            if row.startswith("    ") and _DAY_RE.search(row):
                day_line = row
            elif label:
                block[label] = row
            j += 1
        i = j
        if day_line is None or "UTC" not in block:
            continue
        try:
            out[m["st"]] = _extract_maxt(m["st"], cycle, day_line, block)
        except (KeyError, ValueError, IndexError):
            continue
    return out


def _extract_maxt(station: str, cycle: dt.datetime, day_line: str, block: dict[str, str]) -> list[MaxTPercentiles]:
    day_groups = day_line.split("|")
    day_numbers = [int(dm["day"]) if (dm := _DAY_RE.search(g)) else None for g in day_groups]
    # Map day numbers to real dates walking forward from the cycle date.
    dates: list[dt.date | None] = []
    cur = cycle.date()
    for dn in day_numbers:
        if dn is None:
            dates.append(None)
            continue
        probe = cur
        for _ in range(0, 40):
            if probe.day == dn:
                break
            probe += dt.timedelta(days=1)
        dates.append(probe)
        cur = probe
    utc = _split_groups(block["UTC"])
    rows = {k: _split_groups(block[k]) for k in ("TXNMN", "TXNSD", "TXNP1", "TXNP2", "TXNP5", "TXNP7", "TXNP9")}
    result = []
    for gi, (date, hours) in enumerate(zip(dates, utc)):
        if date is None:
            continue
        for ci, hour in enumerate(hours):
            if hour != "00":
                continue
            # 00Z column under day D+1 holds the max for target day D.
            target = date - dt.timedelta(days=1)
            try:
                vals = {k: float(rows[k][gi][ci]) for k in rows}
            except (IndexError, ValueError):
                continue
            result.append(MaxTPercentiles(station, target, cycle, vals["TXNMN"], vals["TXNSD"], vals["TXNP1"],
                                          vals["TXNP2"], vals["TXNP5"], vals["TXNP7"], vals["TXNP9"]))
    return result
