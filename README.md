# reversal-dashboard

Daily session-reversal signals for a small ETF universe, computed by a GitHub Action
and served as a static page.

**Live:** https://lordjord78.github.io/reversal-dashboard

## The rule

```
z = (prior session open→close) / (60-day stdev of open→close returns)
trade when |z| ≥ 2.0,  position = −z capped at ±2.0
enter at the open, exit at the close, same day
```

Roughly 14 signals a year. Never held overnight — the tested edge is
open-to-close only.

## Universe

| tier | tickers | why |
|---|---|---|
| tradeable | QQQ | SR 0.60 lifetime, 0.51 excluding 2008/2020/2025, 1.03 since 2015. Only name that cleared every test. |
| watch | XLK, DIA, SPY | Positive in both eras and after crisis exclusion, but weaker. Shown for context, not traded. |
| control | TLT, GLD | Non-equity. Must **never** produce a working signal — they're what proved the effect isn't a data artifact. |

Dropped after testing: **XLE** and **EFA** (negative in both eras), **IWM**
(ex-crisis SR −0.00), **XLF** and **EEM** (spread too wide relative to edge).

## Honest limits

- Walk-forward SR is **0.54**, not the headline numbers. Max drawdown −33%, worst
  single trade about −15%.
- Confirming this works from live results would take roughly **27 years**. You are
  trusting a backtest, permanently.
- The cross-instrument replication amounts to about **2 independent tests**, and the
  liquidity mechanism that would explain it was not confirmed.
- Full research record, including three strategies that died on the way here, lives
  in the private vault notes.

## How it works

```
.github/workflows/signals.yml   weekday cron, 12:00 UTC (before the 9:30 ET open)
engine/signals.py               fetches daily bars, computes z, writes data/
data/signals.json               today's state — what the page reads
data/history.json               rolling 500-session log of z per instrument
index.html                      the dashboard, no framework, no build step
```

No broker connection anywhere. Everything derives from public daily open/close bars.

Account equity and the trade log live in browser `localStorage` and are never
committed — the repo is public and holds only z-scores and directions.

## Running locally

```bash
pip install yfinance
python engine/signals.py
python -m http.server 8000
```

Then open http://localhost:8000.

## Not investment advice

A research tool for one person's own account. Signals are computed from a backtest
that may not describe the future.
