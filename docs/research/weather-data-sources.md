# Weather data sources for the daily-high basket bot

Research date: 2026-10-05. Three parallel research passes (paid APIs, free NOAA/ECMWF data, settlement truth and competitor evidence) plus direct live checks from this Mac. Every URL below was fetched or read today unless marked UNVERIFIED.

## 1. Recommendation

### Tier 0 — settlement truth (free, build first)
| Venue | Literal settlement value | Endpoint | Archive for calibration |
|---|---|---|---|
| Kalshi | NWS Daily Climate Report (CLI) daily max, relayed by The Weather Company | `GET https://weather.com/kalshi/api/climate/primary?date=YYYY-MM-DD` → per-station `maxTemp`, `status` pending/preliminary/official/revised (43 stations; undocumented, poll ≤ every 15 min) | IEM `https://mesonet.agron.iastate.edu/json/cli.py?station=KNYC&year=2026` (KNYC from 2003). Verified identical to TWC for 7 stations on 2026-10-04. ACIS `StnData maxt` as cross-check. |
| Polymarket (intl site) | Max of hourly "Temp" column on weather.gov/wrh/timeseries; page calls Synoptic `api.synopticdata.com/v2/stations/timeseries`, rounds `air_temp` to whole °F in JS, keeps obs with minute :51–:59 or :00–:04 | Lowest-latency free replica: `https://aviationweather.gov/api/data/metar?ids=KLGA` (routine METARs, ~1 min), then IEM `asos.py` (1 req/s) | IEM ASOS hourly archive `report_type=3,4`, filtered to the same minute window, local clock day |
| Polymarket US | NWS CLI daily max, 8:00 ET next day (KNYC, KSFO, KMIA, KMDW, KLAX) | same as Kalshi | same as Kalshi |

Known offsets: CLI max is ≥ hourly max on ~45% of station-days (mean gap 0.75°F, max 4.6°F) because CLI uses 1-minute data. CLI day = local standard time midnight-to-midnight; hourly pages use clock time. Model both quantities separately.

### Tier 1 — free probabilistic forecast (build second, 3–5 h)
**NOAA National Blend of Models text bulletins**, especially the probabilistic **NBP** product.
- `https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.YYYYMMDD/HH/text/blend_nbptx.tHHz` (35 MB, all ~9,000 stations). Also `blend_nbstx` (3-hourly, TXN/XND rows) and `blend_nbetx`.
- NBP rows per station: `TXNMN` (mean), `TXNSD` (SD), `TXNP1/P2/P5/P7/P9` (10/25/50/75/90th pct) of the 12Z–06Z max window, in the 00Z columns. Full NBP cycles 01/07/13/19Z; partial at 00/12Z.
- All 20 stations present, including KNYC, KLGA, KMDW, KBKF (verified 2026-10-05 19Z). Example KNYC Oct 6: mean 62, SD 2, P10 61, P90 64. Kalshi's book that minute: "62 or below" 0.59, "63–64" 0.42 — the market tracks NBM closely.
- Latency 55–75 min after cycle on AWS; NOMADS ~20 min earlier. Per-station history via IEM AFOS `retrieve.py?pil=NBPNYC` for backtests.
- Gotcha: TXN window (12Z–06Z) ≠ CLI calendar day; fit a per-station correction against CLI truth.
- Gridded NBM QMD GRIB2 has no TMAX percentiles and lagged ~7 h today. Do not use.

### Tier 2 — independent ensemble members (paid, $99/month)
**Open-Meteo Professional.** Only self-serve source in budget with raw members: ECMWF IFS ENS 51, GEFS 31, ICON-EPS 40, MOGREPS-G 18, Google WeatherNext 2 64. Daily `temperature_2m_max` per member at lat/lon. All paid tiers include Ensemble, Previous Runs and Single Runs APIs with a commercial licence (https://open-meteo.com/en/pricing; $29/$99 figures from Open-Meteo's own newsletter, not on the pricing page). Requests with >10 variables or >2 weeks count fractionally more.
- Members are retained only ~3 days (GitHub issue #1665). Archive every pull from day one.
- 25 km grid cells: station bias must be learned from CLI truth. Previous Runs API (`temperature_2m_previous_dayN`, hourly, lead-indexed) is the non-leaky backtest source for deterministic models.
- Free tier (10k calls/day) is non-commercial; a trading bot should be on a paid plan.

Free alternative to Tier 2: ECMWF Open Data ENS (50 members, `ecmwf-opendata`, ~460 MB/run global, ~7.7 h latency) and GEFS on AWS via Herbie (~325 MB/cycle, ~4 h latency). Roughly 10–14 h of GRIB plumbing and 1–3 GB/day bandwidth. Open-Meteo removes that for $99.

### Tier 3 — deterministic anchors (free, 1–3 h)
- `api.weather.gov` gridpoint `maxTemperature` (NDFD, forecaster-edited, the "official" number retail anchors on). User-Agent header required.
- GFS MOS MAV/MEX via IEM `https://mesonet.agron.iastate.edu/api/1/mos.json?station=KNYC&model=GFS` (`n_x` populated).
- Day-0 running max: aviationweather METARs (6-hour max groups in RMK), HRRR, LAMP.

### Not recommended
- Meteomatics (best point handling, 90 m downscaled members, but sales-gated; expect > $300/month).
- Tomorrow.io (percentiles exist; tier gating unverified; no lead-indexed archive).
- Visual Crossing (spread scalar only), Pirate Weather (no members), Weatherbit, OpenWeather, WeatherAPI, Meteosource, Stormglass, Windy, Foreca, Xweather, Azure Maps (deterministic only).
- Google WeatherNext 3 via BigQuery (64 members, hourly inits, station-trained 5 km 2 m T; data free, BigQuery scans ~$0–20/month) — strongest single AI ensemble, but access form (5–7 business days) and real-time output is under experimental terms that may not permit trading use. Submit the request; treat as Tier 2b later.

### Monthly cost of the recommended stack
| Item | Cost |
|---|---|
| TWC Kalshi endpoint, IEM, aviationweather, NBM on AWS, api.weather.gov | $0 |
| Open-Meteo Professional | $99 |
| Total | **$99/month** |

## 2. What the evidence says about the strategy

Every rigorous public test finds the Kalshi mid beats free forecast products, including NBM:
- Crosier, arXiv 2609.23969 (Sep 2026): 7 Kalshi cities, 7,590 city-days. Market-implied mean RMSE 2.44°F at open vs NBM 2.70°F; 1.88 vs 2.12 at the final bulletin. Market ignores NBM updates; NBM moves toward the market.
- anaborne/kalshi-temperature-calibration: 60,906 contracts, market Brier 0.077 vs model 0.174; every trade type lost.
- SomeGuy966/weather-bot: model beat NBM but the market beat the model; taking 5¢ edges returned −$0.024/contract after the 7% taker fee.
- DarienNouri/kalshi-forecast-value: NYC, market Brier beat calibrated NDFD at 12 h and 6 h; disagreement did not predict market errors.
- Schmiedey/kalshi-weather: the only documented positive edge is selling 1–4¢ YES longshots the day before (~+1¢/contract, 90% CI +0.7..+1.5¢), gone by 9am event day. Consistent with the favorite-longshot bias in Bürgi, Deng, Whelan (SSRN 5502658): Kalshi takers lose ~32% on average, makers ~10%; <10¢ contracts lose >60%.
- Northlake Labs post-mortem: 0–32 record; Gaussian tails under-estimated by 2x; fee death zone below ~15¢; bots act within seconds of model cycles.

Implication for the basket plan: a 10-point taker edge from an ensemble that the market already prices (and beats) will fire rarely, and when it does the edge is more likely a model error than a market error. Where structural edge is documented: (a) maker-side quoting — zero maker fee on Kalshi weather, 25% rebate on Polymarket intl, a paid rebate (0.0125·C·p·(1−p)) on Polymarket US; (b) faster truth feeds than the crowd; (c) the CLI-vs-hourly and Central Park-vs-LaGuardia differences between venues; (d) favorite-longshot bias in the tails. The opportunity logger and calibration table in the plan are still the right first product; the execution mode (taker vs resting bids) is the decision to revisit.

## 3. Venue mechanics confirmed today
- Kalshi: fee `quadratic` ×1 → taker `0.07·C·P·(1−P)` rounded up to the cent; weather is NOT on the maker-fee list (makers pay 0). V2 orders `POST /trade-api/v2/portfolio/events/orders` (side bid/ask, fixed-point `count` and `price` strings, IOC/FOK/GTC, `post_only`, `client_order_id`); batch `/portfolio/events/orders/batched` no longer needs Advanced tier. Basic tier 200 read / 100 write tokens per second. Events open 14:00Z day before; close 01:00 local day after; settle ~07:10 local.
- Polymarket intl: taker `0.05·C·p·(1−p)`, makers 0 + 25% rebate; min 5 shares and ≥$1 per marketable BUY; dynamic tick 0.01→0.001 near extremes; neg-risk V2 exchange; US IPs are close-only and VPN use breaches ToS (docs.polymarket.com/developers/CLOB/geoblock).
- Polymarket US: KYC app account; API keys at polymarket.us/developer (Key ID + Secret, EdDSA headers `X-PM-Access-Key/Timestamp/Signature`, 30 s clock skew); `pip install polymarket-us`; taker Θ=0.0695, maker rebate Θ=−0.0125, banker's rounding to the cent; weather for KNYC/KSFO/KMIA/KMDW/KLAX on CLI at 8:00 ET. Live weather events not visible via the public gateway search; confirm in the app.

## 4. Open items
- Confirm KBKF settlement path for Polymarket intl Denver (no CLI product; hourly page exists) — fine for Polymarket, irrelevant for Kalshi (CLIDEN).
- Confirm NBP 00Z/12Z partial cycles carry TXN rows for our stations.
- Submit the Google WeatherNext data request; read the real-time terms.
- Get a Synoptic token (free Open Access is non-commercial; commercial pricing is behind a bot wall) if byte-exact Polymarket replication is wanted; otherwise aviationweather METARs.
- Watch ECMWF 0.1° open data launch (late Oct 2026) if the GRIB path is ever built.
