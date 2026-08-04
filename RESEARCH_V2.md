# v2 — the graded reversal

**Question:** can we get more trades out of the same instruments?

**Answer:** yes — 251 position-days a year instead of 16, and it holds up out of
sample. But it does not make more money once execution is priced honestly, and
the reason why is the most useful thing this study produced.

Data: six ETFs, 1999-09-15 to 2026-08-03, ~6,760 sessions each, pulled fresh and
validated against the published v1 backtest (QQQ 418 trades / SR 0.758 here
against 416 / 0.716 published — the gap is history depth and vendor, not logic).

---

## What was tried

| | approach | result |
|---|---|---|
| 1 | Lower the threshold | More trades, proportionally less edge each |
| 2 | Shorter/longer vol window | Marginal; 20-day slightly better in a portfolio |
| 3 | EWMA volatility instead of simple stdev | λ=0.97 gave +0.04 SR. Noise. |
| 4 | **Continuous sizing, no threshold** | **15x the trades, half the drawdown** |
| 5 | Deadband hybrid | Strictly worse than either pure form |

### The threshold sweep, QQQ, net Sharpe / trades per year

| vol window | z≥1.0 | z≥1.5 | z≥2.0 | z≥2.5 |
|---|---|---|---|---|
| 20 | 0.57 / 82 | 0.60 / 40 | **0.84 / 19** | 0.68 / 9 |
| 60 (v1) | 0.52 / 74 | 0.58 / 35 | **0.76 / 16** | 0.61 / 7 |
| 250 | 0.60 / 66 | 0.60 / 30 | 0.61 / 14 | 0.57 / 7 |

Lowering the threshold trades Sharpe for frequency more or less linearly. Nothing
free.

### Continuous sizing — `position = clamp(−z × SIZER, ±2)` every session

| variant | SR | trades/yr | max DD | avg bp |
|---|---|---|---|---|
| **v1** — threshold z≥2, vw 60 | 0.758 | 16 | **−24.7%** | 80.0 |
| v2 — `−z × 0.25` | **0.774** | 251 | **−9.3%** | 2.4 |
| v2 — `−z × 0.5` | 0.756 | 251 | −18.0% | 4.5 |
| v2 — `−z × 1.0` | 0.671 | 251 | −33.2% | 6.6 |

Same Sharpe, 15x the trades, a third of the drawdown. That looked like the answer.

---

## Then costs were applied

v2 pays the spread 251 times a year for an edge 33x smaller per trade. v1 pays it
16 times for a big one.

| slippage | v1 QQQ | v2 QQQ | v1 portfolio | v2 portfolio |
|---|---|---|---|---|
| 0.2bp *(shipped assumption)* | 0.758 | **0.774** | 0.782 | **0.828** |
| 1bp | **0.744** | 0.722 | **0.759** | 0.744 |
| 2bp | **0.725** | 0.657 | **0.730** | 0.638 |
| 5bp | **0.670** | 0.463 | **0.644** | 0.320 |
| 8bp | **0.614** | 0.268 | **0.557** | 0.002 |
| **breakeven** | **40.2bp** | 12.1bp | — | 8.0bp |

**v2 wins only below roughly 1bp of slippage.** Above that v1 wins and the gap
widens fast. The audit flagged `SLIPPAGE_BP = 0.2` as optimistic for
market-on-open and market-on-close fills; this is where that assumption stops
being academic and starts choosing the strategy for you.

---

## The actual finding

**The |z| ≥ 2 threshold is a cost filter, not a signal filter.**

The edge does not live only above 2σ — sub-threshold sessions carry real
predictive content, which is why v2 works at all before costs. What the threshold
does is concentrate trading into the sessions where the expected move is large
enough to pay for crossing the spread. Lowering it doesn't destroy the edge; it
destroys the edge's ability to cover execution.

That reframes v1's 14-trades-a-year sparseness. It isn't the strategy being
picky about signal quality. It's the strategy refusing to trade when the prize
is smaller than the toll.

---

## What holds up

**Walk-forward** (fit era vs holdout, no reoptimisation):

| | 1999–2013 | 2013–2026 |
|---|---|---|
| v1 QQQ | 0.667 | 0.925 |
| v2 QQQ `−z×0.25` | 0.695 | 0.898 |
| v1 portfolio | 0.765 | 0.798 |
| v2 portfolio `−z×0.5`, vw 20 | **0.840** | **0.817** |

Both survive. The v2 portfolio is the most stable thing tested — 0.840 then
0.817 across two independent halves. So the structure is real; it is only the
cost arithmetic that sinks it.

**By decade**, v2 is weaker in the 2010–2015 chop (SR 0.20 against v1's 0.58) and
similar elsewhere. Not a regime-independent improvement.

---

## Recommendation

Keep v1 as the traded strategy. Run v2 in parallel as research.

**Then settle it with your own data.** The dashboard already logs fills, and the
alerts page already says the right thing: entry is meant to be the session open,
exit the close, "where the two disagree is your slippage." Log real fills for a
few months and measure it.

- Under 1bp → v2 is genuinely better: same Sharpe, a third of the drawdown, and
  something to do most days instead of fourteen times a year.
- Over 1bp → v1 is better and v2 stays research.

That single measurement decides it, and you are already instrumented to take it.

---

## Also worth knowing

The equal-weight portfolio of QQQ + XLK + DIA + SPY beat QQQ alone on every
variant tested — v1 portfolio SR 0.782 against QQQ's 0.758, with max drawdown
−15.2% against −24.7%. Those are instruments already in the universe, sitting in
the watch tier. Promoting them is a bigger, cheaper improvement than anything v2
does, and it doesn't touch the cost budget because the trades stay sparse.

The catch is that the watch tier's per-name numbers are the ones the audit could
not reconcile with the research note (XLK: note 0.31, engine 0.705). Resolve that
before trading them.
