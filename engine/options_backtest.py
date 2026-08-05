"""
options_backtest.py -- replay every historical trade as the recommended
deep-ITM contract instead of shares, and report what the option version would
have earned.

The question this answers is narrow and worth stating precisely: **given the
signal fired and was right as often as it historically was, does expressing it
through an option leave anything on the table?** It is not a test of the
signal. The signal is the same. Only the instrument changes.

METHOD
------
For each trade the underlying backtest produced:

    1. price the recommended contract at the entry open, Black-Scholes,
       using the same trailing vol that generated the signal, scaled by
       IV_MULTIPLE
    2. the contract expires at that session's close, so its exit value is
       intrinsic -- no model needed on the way out
    3. charge half the spread on entry and half on exit
    4. size to match the share position's delta exposure, so the two are
       comparable on the same capital

DELTA MATCHING
--------------
A raw option return is not comparable to a share return -- the leverage is
completely different. So contracts are sized to deliver the same delta
exposure the share position had:

    contracts   = (pos x equity / S) / (delta x 100)
    return      = pos x (intrinsic - mid - spread) / (S x delta)

which lands on the same scale as the underlying's `pos x (C/O - 1)`.

WHAT WOULD MAKE THIS WRONG
--------------------------
IV_MULTIPLE, and it is calibrated from a single observation. Flat vol, no
skew, and a spread assumed as a percentage of premium rather than measured
per-contract. So the output is a GRID, not a number: if the conclusion flips
inside the grid, the grid is the finding and no single cell should be quoted.
"""

import json, os, statistics, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import signals as S
import backtest as B
import options as O

OUT = os.path.join(S.DATA, "options")

# Spread as a fraction of premium. The 2026-08-04 measurement on a deep-ITM
# QQQ contract was 5.5%, taken after the close when spreads are at their
# widest -- so the true RTH figure is lower and 3% is the working default.
# Sample it properly before quoting any single cell.
SPREAD_GRID = [0.01, 0.02, 0.03, 0.05, 0.08]
IV_GRID = [1.5, 2.0, 2.5]
DEFAULT_SPREAD = 0.03

MIN_TICK = 0.01          # QQQ and SPY quote in pennies at every price
DTE = 1.0                # expiry at the close of the session being traded


def option_trade(t, spread_pct, iv_mult, step, min_tick=MIN_TICK):
    """One share trade -> the same trade expressed as a deep-ITM option.

    Returns None when the contract cannot be formed -- a zero or negative
    modelled premium, or a delta the solver could not reach. Those are
    dropped rather than defaulted, because a silently substituted value here
    would flow straight into the summary statistics.
    """
    pos, S_open, S_close = t["pos"], t["o"], t["c"]
    if pos == 0 or S_open <= 0 or t.get("vol", 0) <= 0:
        return None

    is_call = pos > 0
    sigma = O.annualise(t["vol"]) * iv_mult
    T = DTE / O.TRADING_DAYS

    k = O.strike_for_delta(S_open, T, sigma, is_call, step)
    if not k or k <= 0:
        return None

    mid = O.bs_price(S_open, k, T, sigma, is_call)
    delta = abs(O.bs_delta(S_open, k, T, sigma, is_call))
    if mid <= 0 or delta <= 0.01:
        return None

    spread = max(spread_pct * mid, min_tick)
    intrinsic = max((S_close - k) if is_call else (k - S_close), 0.0)

    # per share of underlying-equivalent exposure.
    #
    # abs(pos), NOT pos. The option already carries the direction: a short
    # signal buys a PUT, which gains when the underlying falls, exactly as the
    # short share position does. Scaling by the signed position flips the sign
    # on every short trade and reports losses as gains. It survived a first
    # run looking entirely plausible -- the summary statistics were the right
    # magnitude and only the sign was wrong.
    pnl_per_share = intrinsic - mid - spread
    ret = abs(pos) * pnl_per_share / (S_open * delta)

    # Premium outlay as a fraction of equity. Above 1.0 the trade is not
    # takeable at all, which the summary counts rather than hides.
    outlay = abs(pos) * mid / (S_open * delta)

    return {
        "d": t["d"], "sd": t["sd"], "z": t["z"], "pos": pos,
        "side": "call" if is_call else "put",
        "k": round(k, 2),
        "delta": round(delta, 3),
        "mid": round(mid, 4),
        "spread": round(spread, 4),
        "intrinsic": round(intrinsic, 4),
        "extrinsic": round(O.extrinsic(S_open, k, T, sigma, is_call), 4),
        "ret": round(ret, 6),
        "share_ret": t["n"],
        "outlay": round(outlay, 4),
    }


def summarise(opt_trades, share_trades, n_sessions):
    if not opt_trades:
        return None
    rets = [t["ret"] for t in opt_trades]
    share = [t["n"] for t in share_trades]
    years = n_sessions / B.TRADING_DAYS
    mean_o = sum(rets) / len(rets)
    mean_s = sum(share) / len(share) if share else 0.0
    wins = sum(1 for r in rets if r > 0)
    # What fraction of the share strategy's edge survives being expressed as
    # an option. Only defined when the share edge is POSITIVE -- with a
    # negative denominator the ratio inverts, so a worse option result reads
    # as a larger number. On random data that produced "edge_retained 3.39"
    # against a strategy losing money in both instruments, which is exactly
    # the kind of confidently-wrong figure this repo exists to not publish.
    retained, reason = None, None
    if mean_s > 0:
        retained = round(mean_o / mean_s, 3)
    else:
        reason = "share edge is non-positive; the ratio would be uninterpretable"

    return {
        "trades": len(rets),
        "option_avg_bp": round(mean_o * 1e4, 1),
        "share_avg_bp": round(mean_s * 1e4, 1),
        "edge_retained": retained,
        "edge_retained_undefined": reason,
        "option_sr": B.sharpe(opt_trades, n_sessions, key="ret"),
        "share_sr": B.sharpe(share_trades, n_sessions, key="n"),
        "hit_rate": round(wins / len(rets), 3),
        "avg_extrinsic_pct": round(
            sum(t["extrinsic"] / t["mid"] for t in opt_trades) / len(opt_trades), 4),
        "max_outlay": round(max(t["outlay"] for t in opt_trades), 3),
        "unfundable": sum(1 for t in opt_trades if t["outlay"] > 1.0),
        "years": round(years, 1),
    }


def grid(share_trades, n_sessions, step):
    """Sensitivity over the two assumptions most likely to be wrong."""
    out = []
    for iv in IV_GRID:
        for sp in SPREAD_GRID:
            opts = [o for o in (option_trade(t, sp, iv, step)
                                for t in share_trades) if o]
            s = summarise(opts, share_trades, n_sessions)
            if s:
                out.append({"iv_multiple": iv, "spread_pct": sp,
                            "edge_retained": s["edge_retained"],
                            "option_avg_bp": s["option_avg_bp"],
                            "option_sr": s["option_sr"]})
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    tickers = [t for t, _, _ in S.UNIVERSE if S.meta(t)["options"]]
    if not tickers:
        print("no option-enabled instruments", file=sys.stderr)
        return

    raw = S.load_all(tickers, period="max", limit=None)
    if not raw:
        print("no data from any source", file=sys.stderr)
        sys.exit(1)

    index = []
    for tk, tier, note in S.UNIVERSE:
        if tk not in raw or not S.meta(tk)["options"]:
            continue
        rows = raw[tk]
        share_trades = B.replay(rows, note.get("spread_bp"))
        if not share_trades:
            continue
        n_sessions = len(rows)
        step = S.meta(tk)["strike_step"]

        opts = [o for o in (option_trade(t, DEFAULT_SPREAD, O.IV_MULTIPLE, step)
                            for t in share_trades) if o]
        base = summarise(opts, share_trades, n_sessions)

        payload = {
            "ticker": tk,
            "tier": tier,
            "method": "synthetic Black-Scholes, deep-ITM, expiry at the exit close",
            "assumptions": {
                "target_delta": O.TARGET_DELTA,
                "iv_multiple": O.IV_MULTIPLE,
                "spread_pct": DEFAULT_SPREAD,
                "risk_free": O.RISK_FREE,
                "dte_sessions": DTE,
                "strike_step": step,
                "min_tick": MIN_TICK,
            },
            "coverage": {"first_session": rows[0][0],
                         "last_session": rows[-1][0],
                         "sessions": n_sessions},
            "base_case": base,
            "sensitivity": grid(share_trades, n_sessions, step),
            "trades": opts,
        }
        with open(os.path.join(OUT, f"{tk}.json"), "w") as f:
            json.dump(payload, f, separators=(",", ":"))

        index.append({"ticker": tk, "tier": tier, **(base or {})})
        if base:
            print(f"{tk}: {base['trades']} trades | option "
                  f"{base['option_avg_bp']:+.1f}bp vs share "
                  f"{base['share_avg_bp']:+.1f}bp | retained "
                  f"{base['edge_retained']}")

    with open(os.path.join(OUT, "index.json"), "w") as f:
        json.dump({"instruments": index,
                   "spread_grid": SPREAD_GRID, "iv_grid": IV_GRID}, f, indent=2)


if __name__ == "__main__":
    main()
