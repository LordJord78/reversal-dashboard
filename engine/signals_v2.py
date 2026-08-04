"""
signals_v2.py -- the GRADED reversal variant, run alongside v1, never instead of it.

v1 asks a yes/no question: did the prior session move at least 2 standard
deviations? If yes, take the opposite side at 2x. If no, do nothing. That fires
about 16 times a year on QQQ.

v2 asks how much: position = clamp(-z * SIZER, +/-CAP), every session, no
threshold. A z of 1.9 is not discarded, it is simply held smaller than a z of
2.4. Same instruments, same open-to-close execution, same no-overnight rule --
the only change is that the gate becomes a dial.

WHAT THE RESEARCH ACTUALLY FOUND  (27 years, 1999-09 to 2026-08, six ETFs)
-------------------------------------------------------------------------
More trades: yes, decisively. 251 sessions a year with a position instead of 16,
and it survives walk-forward -- first half SR 0.695, second half 0.898, so the
shape is not an artifact of fitting the whole sample.

Better returns: no, not once execution is priced honestly.

    slippage      v1 QQQ    v2 QQQ
    0.2bp          0.758     0.774      <- the shipped assumption
    1bp            0.744     0.722
    2bp            0.725     0.657
    5bp            0.670     0.463
    breakeven      40.2bp    12.1bp

v2 wins only below roughly 1bp of slippage. Above that v1 wins and the gap grows
fast, because v2 pays execution 15x more often for an edge that is 15x smaller
per trade. The audit already flagged SLIPPAGE_BP = 0.2 as optimistic; this
variant is where that assumption stops being academic and starts deciding which
strategy is better.

The real finding is about v1, not v2: the |z| >= 2 threshold is not primarily a
signal filter, it is a COST filter. It concentrates trading into the sessions
where the expected move is large enough to pay for crossing the spread. Lowering
it does not destroy the edge -- it destroys the edge's ability to cover costs.

What v2 does unambiguously improve is drawdown: -9.3% against v1's -24.7% on
QQQ, because it is always on in small size rather than occasionally on at 2x.

HOW TO SETTLE IT
----------------
Log real fills on the dashboard for a few months. Entry is meant to be the
session open and exit the close; the gap between those and what you actually got
is your slippage. Under 1bp, v2 is the better strategy. Over it, v1 is, and this
file stays research.

Writes data/v2/signals.json. Touches nothing v1 owns.
"""

import json, os, statistics, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import signals as S

# pos = clamp(-z * SIZER, +/- CAP). 0.25 was the best risk-adjusted setting on
# QQQ and keeps peak leverage rare: |z| must reach 8 to hit the 2.0 cap, which
# happens a handful of times a decade. Raising it lifts return and drawdown
# roughly proportionally and lowers Sharpe -- 0.5 gave 0.756, 1.0 gave 0.671.
SIZER = 0.25
CAP = 2.0

# Below this the position is rounded away rather than sent. Not a signal
# threshold -- purely a "too small to be worth an order" floor.
MIN_POSITION = 0.02

DATA_V2 = os.path.join(S.DATA, "v2")


def compute_graded(rows):
    """Same inputs as S.compute, no threshold. Returns None if history is short.

    Reads through S.load_all, so it inherits drop_incomplete() -- v2 can no more
    score a live bar than v1 can.
    """
    sess = [(d, c / o - 1) for d, o, c in rows if o > 0]
    if len(sess) < S.VOL_WINDOW + 2:
        return None
    prev_date, prev_ret = sess[-1]
    hist = [r for _, r in sess[-(S.VOL_WINDOW + 1):-1]]
    sd = statistics.pstdev(hist)
    if sd <= 0:
        return None

    z = prev_ret / sd
    pos = max(-CAP, min(CAP, -z * SIZER))
    if abs(pos) < MIN_POSITION:
        pos, action = 0.0, "flat"
    else:
        action = "buy" if pos > 0 else "short"

    return {
        "prev_session": prev_date,
        "prev_return": round(prev_ret, 6),
        "vol": round(sd, 6),
        "z": round(z, 3),
        "action": action,
        "position": round(pos, 3),
        "price": round(rows[-1][2], 2),
        # What v1 would have said on the same data, so the two are always
        # comparable on the page without recomputing anything.
        "v1_action": ("none" if abs(z) < S.THRESHOLD
                      else ("buy" if -z > 0 else "short")),
        "v1_position": (0.0 if abs(z) < S.THRESHOLD
                        else round(max(-S.CAP, min(S.CAP, -z)), 3)),
    }


def main():
    os.makedirs(DATA_V2, exist_ok=True)
    tickers = [t for t, _, _ in S.UNIVERSE]
    raw = S.load_all(tickers)
    if not raw:
        print("v2: no data from any source", file=sys.stderr)
        sys.exit(1)

    instruments, last_session = [], None
    for tk, tier, stats in S.UNIVERSE:
        if tk not in raw:
            instruments.append({"ticker": tk, "tier": tier, "error": "no data"})
            continue
        sig = compute_graded(raw[tk])
        if sig is None:
            instruments.append({"ticker": tk, "tier": tier,
                                "error": "insufficient history"})
            continue
        last_session = last_session or sig["prev_session"]
        instruments.append({"ticker": tk, "tier": tier, **sig})

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_session": last_session,
        "variant": "graded",
        "params": {"sizer": SIZER, "cap": CAP, "vol_window": S.VOL_WINDOW,
                   "min_position": MIN_POSITION},
        "research": {
            "sr_at_0.2bp": 0.774, "sr_at_1bp": 0.722, "sr_at_2bp": 0.657,
            "breakeven_slippage_bp": 12.1,
            "v1_breakeven_slippage_bp": 40.2,
            "walk_forward": {"first_half": 0.695, "second_half": 0.898},
            "max_dd": -0.093, "v1_max_dd": -0.247,
            "positions_per_year": 251, "v1_trades_per_year": 16,
            "caveat": ("Beats v1 only below ~1bp slippage. Above that v1 wins "
                       "and the gap widens. Measure your own fills first."),
        },
        "instruments": instruments,
    }

    with open(os.path.join(DATA_V2, "signals.json"), "w") as f:
        json.dump(payload, f, indent=2)

    live = [f"{i['ticker']}:{i['position']:+.2f}" for i in instruments
            if i.get("position")]
    v1 = [i["ticker"] for i in instruments if i.get("v1_action", "none") != "none"]
    print(f"v2 session {last_session} | graded: {' '.join(live) or 'all flat'}")
    print(f"v2 v1-equivalent firing: {v1 or 'none'}")


if __name__ == "__main__":
    main()
