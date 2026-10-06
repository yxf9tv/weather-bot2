"""CLI entry point: python -m weather_bot <command>."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

from .config import load_settings


def _fmt(x, w=6, nd=2):
    return f"{x:{w}.{nd}f}" if isinstance(x, (int, float)) else f"{'-':>{w}}"


def _summary(opps, svc, dry: bool, quotes_placed: int | None = None) -> None:
    print("-" * 60)
    print(f"{'venue':<10} {'station':<6} {'date':<10} {'h':>5} {'Σask':>6} {'P':>5} {'edgeM':>6} {'edgeT':>6}  decision")
    for o in opps:
        b = o.best
        print(f"{o.market.venue:<10} {o.market.station_icao:<6} {o.market.target_date.isoformat():<10} {o.hours:5.1f} "
              f"{_fmt(o.market.sum_of_asks(), 6, 3)} {_fmt(b.prob if b else None, 5, 2)} "
              f"{_fmt(b.net_edge_maker if b else None, 6, 3)} {_fmt(b.net_edge_taker if b else None, 6, 3)}  "
              f"{o.decision}{(' - ' + o.reason) if o.reason else ''}")
    quotable = sum(1 for o in opps if o.decision == "quote")
    print(f"\n{len(opps)} markets, {quotable} quotable. NBM cycle {svc.nbm_cycle}. dry={dry}")


async def cmd_scan(args) -> int:
    from .app import App

    app = App.build(load_settings(), dry=args.dry)
    opps = await app.cycle()
    for o in opps:
        if o.decision == "quote" or args.verbose:
            print(o.console())
    _summary(opps, app.svc, args.dry)
    app.db.add_event("info", "scan", f"scan complete: {len(opps)} markets", {"dry": args.dry})
    app.close()
    return 0


async def cmd_run(args) -> int:
    import logging

    from .app import App

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("py_clob_client_v2").setLevel(logging.CRITICAL)  # it logs the expected create-then-derive 400
    settings = load_settings()
    app = App.build(settings, dry=args.dry)
    live = settings.live_trading and not args.dry
    print(f"weather_bot run: live={live} trading_enabled={settings.trading_enabled} "
          f"kill={app.kill.is_tripped() or 'clear'} db={settings.db_path}")
    app.db.add_event("info", "lifecycle", "run started", {"live": live})
    try:
        while True:
            started = dt.datetime.now(dt.timezone.utc)
            try:
                opps = await app.cycle(started)
                for o in opps:
                    if o.decision == "quote":
                        print(o.console())
                _summary(opps, app.svc, args.dry)
                tripped = app.kill.is_tripped()
                if tripped:
                    print(f"!! trading disabled: {tripped}")
            except Exception as exc:  # keep the loop alive, count the error
                app.kill.record_error("cycle", exc)
                print(f"cycle error: {exc!r}")
            elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
            await asyncio.sleep(max(5.0, settings.book_refresh_sec - elapsed))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        app.db.add_event("info", "lifecycle", "run stopped")
        app.close()
    return 0


async def cmd_score(args) -> int:
    import httpx

    from .scoring.settle import calibration_report, pnl_report, score_pending
    from .storage.db import Database

    settings = load_settings()
    db = Database(settings.db_path)
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        n = await score_pending(db, client)
    print(f"scored {n} market(s)")
    if args.report:
        print(calibration_report(db))
        print()
        print(pnl_report(db))
    db.close()
    return 0


def cmd_report() -> int:
    from .scoring.settle import calibration_report, pnl_report
    from .storage.db import Database

    settings = load_settings()
    db = Database(settings.db_path)
    print(calibration_report(db))
    print()
    print(pnl_report(db))
    db.close()
    return 0


async def cmd_verify(args) -> int:
    from .scoring.verify import verify_date

    for line in await verify_date(load_settings(), dt.date.fromisoformat(args.date)):
        print(line)
    return 0


async def cmd_status() -> int:
    from .execution.risk import KillSwitch, snapshot
    from .storage.db import Database
    from .venues.kalshi import KalshiVenue
    from .venues.polymarket import PolymarketVenue

    settings = load_settings()
    db = Database(settings.db_path)
    now = dt.datetime.now(dt.timezone.utc)
    ks = KillSwitch(settings, db)
    snap = snapshot(db, now)
    print(f"live_trading={settings.live_trading}  trading_enabled={settings.trading_enabled}  "
          f"kill={ks.is_tripped() or 'clear'}  polymarket_until={settings.polymarket_trading_until}")
    print(f"open risk ${snap.open_risk:.2f} / ${settings.max_total_open_risk:.2f}   "
          f"risk today ${snap.risk_today:.2f} / ${settings.max_daily_new_risk:.2f}")
    for name, venue in (("kalshi", KalshiVenue(settings)), ("polymarket", PolymarketVenue(settings))):
        try:
            bal = await venue.balance()
            print(f"{name} balance ${bal:.2f}")
        except Exception as exc:
            print(f"{name} balance unavailable: {type(exc).__name__}: {str(exc)[:80]}")
    rows = db.conn.execute("SELECT id, created_ts, venue, market_key, status, intended_cost, filled_cost, qty, notes "
                           "FROM baskets ORDER BY id DESC LIMIT 15").fetchall()
    print(f"\nbaskets (latest {len(rows)}):")
    for r in rows:
        print(f"  #{r['id']} {r['created_ts'][:16]} {r['venue']:<10} {r['market_key']:<40} {r['status']:<10} "
              f"intended ${r['intended_cost'] or 0:.2f} filled ${r['filled_cost'] or 0:.2f} qty {r['qty'] or 0:g} {r['notes'] or ''}")
    ev = db.conn.execute("SELECT ts, level, kind, substr(text,1,100) t FROM events ORDER BY id DESC LIMIT 8").fetchall()
    print("\nrecent events:")
    for e in ev:
        print(f"  {e['ts'][:19]} {e['level']:<8} {e['kind']:<10} {e['t']}")
    db.close()
    return 0


def cmd_calibrate() -> int:
    from .scoring.calibrate import fit, save
    from .storage.db import Database

    settings = load_settings()
    db = Database(settings.db_path)
    cal = fit(db)
    path = settings.db_path.parent / "calibration.json"
    save(cal, path)
    print(f"fitted {len(cal)} station/quantity pairs -> {path}")
    for k, v in sorted(cal.items()):
        print(f"  {k:<22} offset {v['offset_f']:+.2f}F  inflation {v['inflation']:.2f}  n={v['n']}")
    if not cal:
        print("(need ≥15 settled days per station/quantity; keep running score daily)")
    db.close()
    return 0


def cmd_toggle(enable: bool) -> int:
    from .execution.risk import KillSwitch
    from .storage.db import Database

    settings = load_settings()
    db = Database(settings.db_path)
    ks = KillSwitch(settings, db)
    if enable:
        ks.reset()
        print("trading enabled (kill file removed). Open positions are unaffected.")
    else:
        ks.trip("manual: disable-trading command")
        print(f"trading disabled: {settings.kill_file}. Existing baskets keep reconciling; no new quotes.")
    db.close()
    return 0


async def cmd_forecast(args) -> int:
    from .markets.stations import STATIONS
    from .storage.db import Database
    from .weather.service import ForecastService

    settings = load_settings()
    st = STATIONS.get(args.station.upper())
    if st is None:
        print(f"unknown station {args.station}; known: {', '.join(sorted(STATIONS))}")
        return 2
    target = dt.date.fromisoformat(args.date)
    db = Database(settings.db_path)
    svc = ForecastService(settings, db)
    await svc.refresh_all([st])
    fc = svc.forecast(st.icao, target)
    print(f"{st.icao} {st.name}  target {target}  NBM cycle {svc.nbm_cycle}")
    if fc.nbm:
        n = fc.nbm
        print(f"NBM   mean {n.mean:.0f} sd {n.sd:.0f}  P10 {n.p10:.0f} P25 {n.p25:.0f} P50 {n.p50:.0f} "
              f"P75 {n.p75:.0f} P90 {n.p90:.0f}")
    else:
        print("NBM   (no row for this date)")
    if fc.ensemble:
        mm = fc.ensemble.model_means()
        allm = fc.ensemble.all_members()
        print(f"OM    {len(allm)} members  mean {sum(allm)/len(allm):.1f}  " +
              "  ".join(f"{k} {v:.1f}" for k, v in mm.items()))
    else:
        print("OM    (no ensemble)")
    print(f"NWS   maxT {fc.nws_max:.1f}" if fc.nws_max is not None else "NWS   (none)")
    for quantity in ("cli_max", "hourly_max"):
        dists = svc.distributions(fc, quantity)
        print(f"\n[{quantity}]")
        for d in (dists.nbm, dists.openmeteo):
            if d is None:
                continue
            total = sum(d.pmf.values())
            cells = "  ".join(f"{t}:{p:.2f}" for t, p in sorted(d.pmf.items()) if p >= 0.01)
            print(f"  {d.source:<9} mean {d.mean:.1f} sd {d.sd:.2f} sum {total:.3f}\n    {cells}")
    db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="weather_bot")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="discover markets (and later evaluate/quote)")
    s.add_argument("--once", action="store_true")
    s.add_argument("--dry", action="store_true", help="never place orders")
    s.add_argument("-v", "--verbose", action="store_true", help="print the dashboard block for every market")
    r = sub.add_parser("run", help="run the trading loop")
    r.add_argument("--dry", action="store_true", help="never place orders")
    sc = sub.add_parser("score", help="pull settlement truth for finished days")
    sc.add_argument("--report", action="store_true")
    sub.add_parser("report", help="calibration buckets and PnL")
    sub.add_parser("status", help="kill state, risk, balances, baskets, recent events")
    sub.add_parser("calibrate", help="fit per-station NBM offsets/inflation from settled data")
    vt = sub.add_parser("verify-truth", help="compare our truth feeds with venue settlements for a date")
    vt.add_argument("date")
    sub.add_parser("disable-trading", help="kill switch on: stop opening new quotes")
    sub.add_parser("enable-trading", help="kill switch off")
    f = sub.add_parser("forecast", help="print forecast + distributions for one station/date")
    f.add_argument("station")
    f.add_argument("date")
    args = p.parse_args(argv)
    if args.cmd == "scan":
        return asyncio.run(cmd_scan(args))
    if args.cmd == "forecast":
        return asyncio.run(cmd_forecast(args))
    if args.cmd == "run":
        return asyncio.run(cmd_run(args))
    if args.cmd == "score":
        return asyncio.run(cmd_score(args))
    if args.cmd == "report":
        return cmd_report()
    if args.cmd == "calibrate":
        return cmd_calibrate()
    if args.cmd == "status":
        return asyncio.run(cmd_status())
    if args.cmd == "verify-truth":
        return asyncio.run(cmd_verify(args))
    if args.cmd == "disable-trading":
        return cmd_toggle(False)
    if args.cmd == "enable-trading":
        return cmd_toggle(True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
