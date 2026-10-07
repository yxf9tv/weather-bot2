"""`probe`: prove that THIS machine can trade on each venue. Rests one minimum-size bid far below the market on
one live weather bin, confirms it is accepted, cancels it, confirms the cancel. Prints PASS/FAIL per venue."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

from .config import Settings
from .venues.base import Venue


async def probe_venue(venue: Venue) -> tuple[bool, str]:
    try:
        markets = await venue.discover()
    except Exception as exc:
        return False, f"discover failed: {exc!r}"
    for m in markets:
        if not m.rules.confident:
            continue
        for b in m.sorted_bins():
            if b.yes_bid is None or b.yes_bid < 0.10:
                continue
            price = round(max(venue.tick, b.yes_bid / 2), 2)      # far below the bid: cannot fill
            qty = venue.min_qty(price, marketable=False)
            try:
                res = await venue.place_limit(b.instrument_id, price, qty, side="buy", post_only=True, ioc=False,
                                              client_id=str(uuid.uuid4()))
            except Exception as exc:
                return False, f"order rejected: {exc!r}"
            if res.status != "resting" or not res.order_id:
                return False, f"order not accepted: {res.status} {res.raw}"
            try:
                await venue.cancel(res.order_id)
            except Exception as exc:
                return False, f"RESTING ORDER {res.order_id} COULD NOT BE CANCELLED: {exc!r}"
            for _ in range(5):
                st = await venue.order_status(res.order_id)
                if st.status == "cancelled":
                    return True, f"{m.city} {b.label}: bid {qty:g} @ {price} rested and cancelled"
                if st.filled_qty:
                    return False, f"probe order FILLED {st.filled_qty:g} @ {st.avg_fill_price} on {m.key} {b.label}"
                await asyncio.sleep(0.5)
            return False, f"cancel sent but order {res.order_id} still reports {st.status}"
    return False, "no quotable bin found"


async def polymarket_geoblock() -> dict:
    """Polymarket's own IP check: {'blocked': bool, 'ip', 'country', 'region'}."""
    import httpx

    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as c:
        r = await c.get("https://polymarket.com/api/geoblock")
        r.raise_for_status()
        return r.json()


async def run_probe(settings: Settings) -> int:
    from .venues.kalshi import KalshiVenue
    from .venues.polymarket import PolymarketVenue

    print(f"probe at {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}")
    ok_all = True
    try:
        g = await polymarket_geoblock()
        # Informational only: this is the WEBSITE check. Countries that are close-only on the frontend (e.g. NL)
        # report blocked=true here while the CLOB API still accepts orders. The order probe below is the verdict.
        print(f"polymarket geoblock (website check): ip {g.get('ip')} country {g.get('country')} "
              f"region {g.get('region')} blocked={g.get('blocked')}")
    except Exception as exc:
        print(f"polymarket geoblock check failed: {exc!r}")
    for venue in (KalshiVenue(settings), PolymarketVenue(settings)):
        try:
            bal = await venue.balance()
        except Exception as exc:
            bal = None
            print(f"{venue.name:<10} balance: ERROR {exc!r}")
        ok, msg = await probe_venue(venue)
        ok_all &= ok
        print(f"{venue.name:<10} {'PASS' if ok else 'FAIL'}  balance ${bal:.2f}  {msg}" if bal is not None
              else f"{venue.name:<10} {'PASS' if ok else 'FAIL'}  {msg}")
    return 0 if ok_all else 1
