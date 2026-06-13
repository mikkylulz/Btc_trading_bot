# BTC 5m/15m Prediction-Market Bot

A fair-value trading bot for Polymarket BTC up/down markets. It prices `P(close >= open)` for
each window and trades, as a maker, only when its estimate diverges from the order book by more
than fees plus spread. It does not try to predict BTC's direction with momentum indicators.

> Read `Claude.md` before building or changing anything. It is the design source of truth and
> explains *why* the architecture is the way it is.

## Why this design (short version)

- Over 5–15 minutes BTC is near a martingale, so directional prediction has almost no edge.
- The edge is pricing the true probability better than the book, strongest as the window closes.
- 15m markets resolve on the **Chainlink BTC/USD stream**, 1h on **Binance BTC/USDT** — the
  bot prices each against its real resolution feed.
- Crypto carries the platform's highest taker fee, deliberately aimed at latency arbitrage, so
  execution is **maker-first** (limit orders, zero fee).
- The objective is **positive EV net of fees**, not win rate.

## Project layout

```
/bot
  main.py              entry point, async scheduler loop + CLI
  config.py            config + env loading (pydantic-settings)
  models.py            shared dataclasses (MarketInfo, FairValue, Order, ...)
  market_discovery.py  Gamma API discovery, deterministic slugs, feed/window detection
  data_feeds/          spot_ws, chainlink_feed, polymarket_ws
  signals/             volatility, momentum, microstructure, funding, oi (drift adjustments)
  strategy.py          fair-value engine: P = Φ(z)
  execution/           polymarket_client, orders (maker-first), paper_engine
  risk.py              sizing, exposure caps, halt-streak isolation, paper/live gate
  logging_db.py        SQLite trade store + calibration store, decision log
  backtester.py        replays resolution-feed prices, net-of-fee, maker fills only
  monitoring/          Prometheus endpoint + Grafana dashboard stub
  utils.py
/tests
Claude.md              architecture + build spec (read this first)
README.md
requirements.txt
.env.example
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then fill in your keys
```

## Run

```bash
python -m bot.main               # paper mode by default
python -m bot.main discover      # list active BTC markets + detected resolution feed
python -m bot.main backtest      # run the backtester
python -m bot.main status        # open positions + net-of-fee PnL
pytest                           # tests
```

## Going live

Live mode is gated. It requires an explicit env flag **and** the paper-validation record
described in `Claude.md` §11: calibrated `Φ(z)`, and positive EV net of the ~1.8% crypto taker
fee with realistic maker fill rates over a meaningful paper sample. Default is paper.

## Status

Core implemented and tested (54 tests): fair-value engine, feeds with staleness halts,
Gamma discovery (deterministic `btc-updown-{5m,15m}-<open_ts>` slugs, verified live),
maker-first execution with realistic paper fills, risk gates, SQLite trade/calibration
store, backtester, monitoring stubs.

Resolution feed: the default `CHAINLINK_SOURCE=rtds` reads the Chainlink BTC/USD
stream from Polymarket's public real-time data socket (no credentials) — the same
values the resolver sees. For lower latency or redundancy, request a sponsored
Chainlink Data Streams key (https://pm-ds-request.streams.chain.link/) and switch
to `CHAINLINK_SOURCE=direct`.

Known gaps before paper validation can start:
- No 1h series was found on Gamma under `btc-updown-1h-*` (2026-06-10); the 1h path is
  built but has no live market to point at.
- Backtester's synthetic book validates mechanics/calibration only — collect recorded
  book snapshots to measure real edge.

Paper mode only until the §11 validation record exists.
