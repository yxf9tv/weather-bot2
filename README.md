# weather-bot-2

Trades daily high-temperature markets on Kalshi and Polymarket. It buys contiguous baskets of
temperature bins with resting limit bids when the modeled probability of the range exceeds the
cost by a configurable margin. Every opportunity, taken or not, is stored for calibration.

Design notes: `docs/research/weather-data-sources.md`. Plan: `~/.claude/plans/hey-i-m-going-to-dapper-spark.md`.

## Setup

```sh
uv sync
cp .env.example .env   # then fill in keys
```

`.env` keys: `KALSHI_API_KEY` + `KALSHI_PRIVATE_KEY_PATH` (PEM file, gitignored), `POLY_PK`, `POLY_FUNDER`,
`POLY_SIG_TYPE=3`, `OPENMETEO_API_KEY` (optional), `NWS_USER_AGENT`, `LIVE_TRADING`, `POLYMARKET_TRADING_UNTIL`.

## Commands

```sh
uv run python -m weather_bot scan --once --dry      # one evaluation pass, no orders (add -v for every market)
uv run python -m weather_bot forecast KNYC 2026-10-07
uv run python -m weather_bot run                    # trading loop (orders only if LIVE_TRADING=true)
uv run python -m weather_bot run --dry              # loop, never places orders
uv run python -m weather_bot status                 # kill state, risk, balances, baskets, events
uv run python -m weather_bot score --report         # pull settlement truth, print calibration + PnL
uv run python -m weather_bot verify-truth 2026-10-04 # our truth vs what each venue settled
uv run python -m weather_bot calibrate              # fit per-station offsets from stored data
uv run python -m weather_bot disable-trading        # kill switch on (open baskets keep reconciling)
uv run python -m weather_bot enable-trading
uv run pytest
```

## How a cycle works

1. Discover open daily-high events on both venues; parse rules; cross-check station against the verified table.
   Any mismatch marks the market DO NOT TRADE.
2. Pull the latest NBM probabilistic bulletin (station MaxT percentiles), Open-Meteo ensemble members, and the
   NWS gridpoint max. Build a whole-degree distribution for the venue's quantity (CLI max or hourly max).
3. Enumerate 3–6 contiguous bins, price each leg at model-fair minus its share of `MIN_NET_EDGE`, never crossing
   the ask. Size at the venue minimum within `MAX_BASKET_COST_<VENUE>`.
4. Gate on range probability, net edge, model disagreement, NBM age, horizon, risk caps and the kill switch.
5. Post GTC post-only bids for every leg. Re-quote on TTL or model move; resolve partial baskets after a timeout.
6. Score finished days against the settlement source; print calibration buckets for NBM, Open-Meteo and the market.

## Settlement facts (verified 2026-10-05)

| | Kalshi | Polymarket |
|---|---|---|
| Source | The Weather Company relaying the NWS Daily Climate Report | Max of hourly "Temp" on weather.gov/wrh/timeseries |
| NYC | CLINYC Central Park | KLGA LaGuardia |
| Chicago | CLIMDW Midway | KORD O'Hare |
| Dallas | CLIDFW | KDAL Love Field |
| Denver | CLIDEN | KBKF Buckley |
| Fees | taker 0.07·C·P(1−P) rounded up; maker 0 | taker 0.05·C·p(1−p); maker 0 + 25% rebate |
| Min order | 1 contract | 5 shares and ≥$1 |
