"""One-off: thin books and opportunities to one row per instrument/market per 15 minutes, then VACUUM.
Run with the service STOPPED:  uv run python deploy/prune_db.py data/weather_bot.sqlite
Keeps every opportunity referenced by a basket and every decision change."""
import sqlite3, sys, time

path = sys.argv[1]
db = sqlite3.connect(path, isolation_level=None)
t0 = time.time()
before = {t: db.execute(f"select count(*) from {t}").fetchone()[0] for t in ("books", "opportunities")}
db.execute("BEGIN")
db.execute("""CREATE TEMP TABLE keep_books AS SELECT min(id) id FROM books
              GROUP BY instrument_id, substr(ts,1,14) || (CAST(substr(ts,15,2) AS INTEGER) / 15)""")
db.execute("DELETE FROM books WHERE id NOT IN (SELECT id FROM keep_books)")
db.execute("""CREATE TEMP TABLE keep_opp AS
              SELECT min(id) id FROM opportunities
              GROUP BY market_key, decision, substr(ts,1,14) || (CAST(substr(ts,15,2) AS INTEGER) / 15)
              UNION SELECT opportunity_id FROM baskets WHERE opportunity_id IS NOT NULL""")
db.execute("DELETE FROM opportunities WHERE id NOT IN (SELECT id FROM keep_opp)")
db.execute("COMMIT")
after = {t: db.execute(f"select count(*) from {t}").fetchone()[0] for t in ("books", "opportunities")}
print("rows before", before, "after", after)
db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
db.execute("VACUUM")
print(f"vacuumed in {time.time() - t0:.0f}s")
