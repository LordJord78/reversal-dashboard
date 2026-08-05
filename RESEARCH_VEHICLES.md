# Vehicles — TQQQ and QQQM

Measured 2026-08-04. The question is narrow and deliberately not "does the
signal work on these." It cannot: QQQM tracks the same index as QQQ and TQQQ
is QQQ levered 3x, so any significance test on them is measuring QQQ again
through a correlated instrument. The question is **should QQQ's signal be
traded through a different instrument.**

## The tick floor decides most of it

Every US equity quotes in one-cent increments. What that cent is *worth* in
basis points is set entirely by the share price:

| | Price | One cent | vs QQQ |
|---|---|---|---|
| **QQQ** | $723.85 | **0.138 bp** | 1.0x |
| **QQQM** | $298.03 | **0.336 bp** | **2.4x** |
| **TQQQ** | $74.82 | **1.337 bp** | **9.7x** |

This is a floor, not an estimate. No amount of liquidity gets QQQM below
0.336bp, because a cent is a cent.

Observed spreads at 20:38 ET, after the close, so wide — the ranking is the
signal here, not the levels:

| | Bid / ask | Spread | In bp |
|---|---|---|---|
| QQQ | 723.45 / 723.52 | $0.07 | 0.97 |
| QQQM | 297.85 / 297.90 | $0.05 | 1.68 |
| TQQQ | 74.75 / 74.77 | $0.02 | 2.67 |

## QQQM is strictly worse here

QQQM exists for one reason: a lower expense ratio than QQQ. Expense ratio is
charged for **holding**, and this strategy holds roughly 14 sessions a year.

- Expense saving, pro-rated across ~14 of 252 sessions: on the order of
  **0.003%/yr**. Effectively zero.
- Spread cost: **2.4x QQQ's floor**, on every trade.

The instrument's only advantage is the one this strategy cannot collect, and
its disadvantage lands on every fill. Against a ~68bp edge, QQQ's tick floor
is 0.2% of the edge and QQQM's is 0.5% — small in absolute terms, but there is
no compensating benefit whatsoever.

**Conclusion: no reason to trade QQQM instead of QQQ. Kept in the universe as
a displayed watch row, not as a candidate.**

## TQQQ — the decay objection does not apply, the exposure one does

**Volatility decay is not the problem here.** Decay is a compounding effect
across multiple daily resets. This rule enters at the open and exits at the
close of the *same session*, so it experiences exactly one reset and no
compounding. The standard warning about leveraged ETFs is aimed at holding
periods this strategy never takes.

Spread is a real but modest cost: 1.337bp per cent against roughly 3x the
per-trade move, so about 0.65% of the levered edge versus 0.2% for QQQ.

**The problem is exposure.** At the same 2.0 position cap:

```
QQQ   pos 2.0  ->  2x the index
TQQQ  pos 2.0  ->  6x the index
```

The published worst QQQ trade is **−15.5%**. The same session expressed
through TQQQ at the same cap is roughly **−46%**.

That is a deliberate choice, made with the number in front of it. The engine
publishes `leverage` and `effective_exposure` on every instrument — including
the ones where it equals the position — so 6x is never an unlabelled figure on
a card, and the site renders it in red on any instrument with embedded
leverage.

**Conclusion: TQQQ is a viable way to get leverage without margin, and the
usual decay objection does not bite on a one-session hold. It is carried at
the same 2.0 cap by explicit decision, which triples the tail.**

## What is not established

- Neither instrument has been through the cost model, walk-forward, or crisis
  exclusion. Both sit in the **watch** tier with `stats: null`, and the site
  shows no track record beside them because there isn't one.
- TQQQ's history starts 2010 and QQQM's 2020, so neither spans the crisis
  periods the QQQ result was tested against.
- The spread figures above are one after-hours snapshot. The tick-floor
  arithmetic is exact and permanent; the observed spreads are not.
