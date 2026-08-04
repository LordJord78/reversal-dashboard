"""
backtest.py -- replay the session-reversal rule over the full archive and
write data/backtest/<TICKER>.json for the per-ticker pages.

Identical rule to signals.py, applied to every session we have bars for:

    z_t = (session t open->close) / pstdev(the 60 sessions before t)
    if |z_t| >= THRESHOLD:  position = clamp(-z_t, +/-CAP)
    the position is held on session t+1 only: in at the open, out at the close

signals.py computes exactly one z -- the one for tomorrow. This file computes
every z there has ever been. The two must agree on the most recent session or
one of them is wrong; test_backtest.py asserts that.

COST WARNING
------------
Everything above is the research note's rule. The cost number is NOT. The note
used a volatility-scaled cost model whose internals aren't in this repo, so
COSTS below is a plainer stand-in and its assumptions are stated inline. Both
gross and net are published, and the per-ticker page shows the recomputed
figures against the note's published ones. Treat a widening gap as a finding,
not as noise -- same reasoning as the TLT/GLD control tier.
"""

import json, os, statistics, sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import signals as S

TRADING_DAYS = 252

# Half a spread crossed on the way in, half on the way out = one full spread
# per round trip, charged on the levered notional. SLIPPAGE_BP is an added
# allowance for the market-on-open and market-on-close fills this rule
# actually transacts at -- it is an assumption, not a measurement. Raise it
# and watch how fast the edge moves if you want to know how fragile it is.
SLIPPAGE_BP = 0.2


def cost_bp(spread_bp, position):
    if spread_bp is None:
        spread_bp = 1.0
    return abs(position) * (spread_bp + SLIPPAGE_BP)


# ---------------------------------------------------------------- replay
def replay(rows, spread_bp):
    """rows: sorted [(date, open, close)]. Returns the full trade list."""
    sess = [(d, c / o - 1) for d, o, c in rows if o > 0]
    if len(sess) < S.VOL_WINDOW + 3:
        return []

    trades = []
    # i indexes the SIGNAL session; the trade happens on i+1.
    for i in range(S.VOL_WINDOW, len(sess) - 1):
        hist = [r for _, r in sess[i - S.VOL_WINDOW:i]]
        sd = statistics.pstdev(hist)
        if sd <= 0:
            continue
        z = sess[i][1] / sd
        if abs(z) < S.THRESHOLD:
            continue
        pos = max(-S.CAP, min(S.CAP, -z))

        d, o, c = rows[i + 1]
        if o <= 0:
            continue
        gross = pos * (c / o - 1)
        net = gross - cost_bp(spread_bp, pos) / 1e4
        trades.append({
            "d": d,                                   # session traded
            "sd": rows[i][0],                         # session that fired
            "z": round(z, 3),
            "side": "short" if pos < 0 else "buy",
            "pos": round(pos, 3),
            "o": round(o, 2), "c": round(c, 2),
            "g": round(gross, 6), "n": round(net, 6),
        })
    return trades


# ---------------------------------------------------------------- stats
def max_drawdown(curve):
    peak, worst = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst


def sharpe(trades, n_sessions, key="n"):
    """Annualised Sharpe of the strategy as actually held.

    The daily series is zero on every session the rule sat out -- which is
    most of them, roughly 14 trading days a year. That is the honest
    denominator: capital was committed to this rule the whole time even
    though it was only exposed occasionally. Computing Sharpe over trade
    days only would roughly triple it and mean nothing.
    """
    if not trades or n_sessions < 2:
        return None
    rets = [t[key] for t in trades]
    # The full series is these returns plus a zero for every session sat out.
    # Mean and population variance depend only on the multiset, not the order,
    # so the zeros can be folded in analytically instead of materialised.
    mean = sum(rets) / n_sessions
    var = (sum((r - mean) ** 2 for r in rets)
           + (n_sessions - len(rets)) * mean ** 2) / n_sessions
    if var <= 0:
        return None
    return mean / var ** 0.5 * (TRADING_DAYS ** 0.5)


def summarise(trades, n_sessions, years):
    rets = [t["n"] for t in trades]
    gross = [t["g"] for t in trades]
    sr_n = sharpe(trades, n_sessions, "n")
    sr_g = sharpe(trades, n_sessions, "g")
    eq, v = [], 1.0
    for r in rets:
        v *= (1 + r)
        eq.append(v)
    wins = sum(1 for r in rets if r > 0)
    return {
        "trades": len(trades),
        "per_year": round(len(trades) / years, 1) if years else None,
        "avg_bp": round(sum(rets) / len(rets) * 1e4, 1),
        "avg_bp_gross": round(sum(gross) / len(gross) * 1e4, 1),
        "median_bp": round(statistics.median(rets) * 1e4, 1),
        "hit_rate": round(wins / len(rets), 4),
        "sr": round(sr_n, 3) if sr_n is not None else None,
        "sr_gross": round(sr_g, 3) if sr_g is not None else None,
        "total_return": round(eq[-1] - 1, 4),
        "max_dd": round(max_drawdown(eq), 4),
        "best": round(max(rets), 4),
        "worst": round(min(rets), 4),
        "long": sum(1 for t in trades if t["pos"] > 0),
        "short": sum(1 for t in trades if t["pos"] < 0),
    }


def by_year(trades):
    years = {}
    for t in trades:
        y = t["d"][:4]
        years.setdefault(y, []).append(t["n"])
    out = []
    for y in sorted(years):
        r = years[y]
        v = 1.0
        for x in r:
            v *= (1 + x)
        out.append({"year": int(y), "trades": len(r), "ret": round(v - 1, 4),
                    "hit": round(sum(1 for x in r if x > 0) / len(r), 3),
                    "avg_bp": round(sum(r) / len(r) * 1e4, 1)})
    return out


# ---------------------------------------------------------------- alerts
ALERT_WINDOW_DAYS = 60


def write_alerts(fired, generated_at):
    """data/alerts.json -- the recent alert log, small enough to load on its own.

    fired: [(ticker, tier, trades)] straight from the replay above.

    Every row is a session where the rule said trade this: the instrument, the
    entry (that session's open) and the exit (its close). These are the prices
    the rule assumes -- market-on-open in, market-on-close out. Your actual
    fills are in your own trade log and will differ; that difference is the
    whole reason both are worth keeping.

    Control-tier fires are included deliberately, flagged actionable=false.
    An alert log that hides the alerts you must not act on is not a log.

    The window is anchored to the newest session in the data rather than to
    today, so a stale workflow shows a short window instead of an empty one.
    """
    latest = max((t[-1]["d"] for _, _, t in fired if t), default=None)
    if latest is None:
        return
    cutoff = (datetime.strptime(latest, "%Y-%m-%d").date()
              - timedelta(days=ALERT_WINDOW_DAYS)).isoformat()

    alerts = []
    for tk, tier, trades in fired:
        for t in trades:
            if t["d"] < cutoff:
                continue
            alerts.append({
                "d": t["d"], "sd": t["sd"], "ticker": tk, "tier": tier,
                "actionable": tier != "control",
                "z": t["z"], "side": t["side"], "pos": t["pos"],
                "entry": t["o"], "exit": t["c"],
                "gross": t["g"], "net": t["n"],
            })
    alerts.sort(key=lambda a: (a["d"], a["ticker"]), reverse=True)

    # Anything firing right now is for the NEXT session, so it has no fill yet.
    pending = []
    try:
        sig = json.load(open(os.path.join(S.DATA, "signals.json")))
        for i in sig.get("instruments", []):
            if i.get("action", "none") != "none":
                pending.append({
                    "ticker": i["ticker"], "tier": i["tier"],
                    "actionable": i["tier"] != "control",
                    "z": i["z"], "side": i["action"], "pos": i["position"],
                    "signal_session": i["prev_session"], "ref_price": i["price"],
                })
    except Exception as e:
        print(f"alerts: could not read signals.json ({e})", file=sys.stderr)

    payload = {"generated_at": generated_at, "window_days": ALERT_WINDOW_DAYS,
               "from": cutoff, "through": latest,
               "pending": pending, "alerts": alerts}
    path = os.path.join(S.DATA, "alerts.json")
    with open(path, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"alerts.json: {len(alerts)} in last {ALERT_WINDOW_DAYS}d "
          f"({cutoff} to {latest}), {len(pending)} pending "
          f"({os.path.getsize(path)/1024:.1f}KB)")


# ---------------------------------------------------------------- main
def main():
    out_dir = os.path.join(S.DATA, "backtest")
    os.makedirs(out_dir, exist_ok=True)

    tickers = [t for t, _, _ in S.UNIVERSE]
    raw = S.load_all(tickers, period="max", limit=None)
    if not raw:
        print("no data from any source", file=sys.stderr)
        sys.exit(1)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    index, fired = [], []

    for tk, tier, note in S.UNIVERSE:
        rows = raw.get(tk)
        if not rows:
            print(f"{tk}: no data, skipped", file=sys.stderr)
            continue
        trades = replay(rows, note.get("spread_bp"))
        if not trades:
            print(f"{tk}: no trades in {len(rows)} sessions", file=sys.stderr)
            continue

        n_sessions = len(rows)
        years = n_sessions / TRADING_DAYS
        payload = {
            "ticker": tk, "tier": tier, "generated_at": generated_at,
            "params": {"vol_window": S.VOL_WINDOW, "threshold": S.THRESHOLD,
                       "cap": S.CAP, "spread_bp": note.get("spread_bp"),
                       "slippage_bp": SLIPPAGE_BP},
            "coverage": {"first_session": rows[0][0], "last_session": rows[-1][0],
                         "sessions": n_sessions, "years": round(years, 1)},
            "note_stats": note,
            "computed": summarise(trades, n_sessions, years),
            "by_year": by_year(trades),
            "trades": trades,
        }
        path = os.path.join(out_dir, f"{tk}.json")
        with open(path, "w") as f:
            json.dump(payload, f, separators=(",", ":"))

        fired.append((tk, tier, trades))
        c = payload["computed"]
        index.append({"ticker": tk, "tier": tier, "trades": c["trades"],
                      "sr": c["sr"], "avg_bp": c["avg_bp"],
                      "years": payload["coverage"]["years"]})
        print(f"{tk:4s} {n_sessions:5d} sessions  {c['trades']:4d} trades  "
              f"{c['per_year']:5.1f}/yr  avg {c['avg_bp']:+7.1f}bp  "
              f"hit {c['hit_rate']*100:4.1f}%  SR {c['sr']}  "
              f"maxDD {c['max_dd']*100:.1f}%  ({os.path.getsize(path)/1024:.0f}KB)")

    with open(os.path.join(out_dir, "index.json"), "w") as f:
        json.dump({"generated_at": generated_at, "tickers": index}, f, indent=2)

    write_alerts(fired, generated_at)


if __name__ == "__main__":
    main()
