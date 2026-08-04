"""
signals.py -- compute session-reversal signals and write data/signals.json

Rule (see QQQ Session Reversal research note):
    z = (prior session open->close) / (60-day stdev of open->close)
    trade when |z| >= 2.0, position = -z capped at +/-2.0
    enter at the open, exit at the close, same day

Universe is tiered deliberately. Only QQQ cleared every test -- crisis
exclusion, walk-forward, and a volatility-scaled cost model. The watch tier
is shown for context, not for trading. The control tier MUST NOT produce a
working signal; if it ever does, this pipeline is broken.

Runs in GitHub Actions on a weekday cron. No broker connection needed --
everything here comes from public daily open/close bars.

Also writes data/prices.json: the same daily bars the signal is computed
from, published for the dashboard to chart. This is deliberate -- the page
is static, so it cannot call a price API at render time (no server to hold
a key, and the free feeds block browser requests outright). Publishing the
bars we already downloaded means the chart and the signal can never
disagree, which a second live feed could not guarantee.
"""

import json, os, statistics, sys, urllib.request, io, csv
from datetime import datetime, timezone

VOL_WINDOW = 60
THRESHOLD  = 2.0
CAP        = 2.0

# How much daily history to publish for the dashboard charts. The signal
# itself only ever looks at the last VOL_WINDOW + 2 sessions, so this is
# purely for the price-history view.
HISTORY_SESSIONS = 520          # ~2 trading years

# tier: tradeable | watch | control
# stats are from the research note: cap 2.0, vol-scaled cost, 1993-2026
UNIVERSE = [
    ("QQQ", "tradeable", {"sr": 0.60, "sr_ex": 0.51, "sr_modern": 1.03,
                          "tpy": 14, "avg_bp": 67.8, "spread_bp": 0.14,
                          "worst_trade": -15.5}),
    ("XLK", "watch",     {"sr": 0.31, "sr_ex": 0.17, "sr_modern": 0.78,
                          "tpy": 14, "avg_bp": 31.7, "spread_bp": 0.56,
                          "worst_trade": None}),
    ("DIA", "watch",     {"sr": 0.35, "sr_ex": 0.21, "sr_modern": 0.56,
                          "tpy": 15, "avg_bp": 23.4, "spread_bp": 0.19,
                          "worst_trade": None}),
    ("SPY", "watch",     {"sr": 0.38, "sr_ex": 0.27, "sr_modern": 0.51,
                          "tpy": 15, "avg_bp": 26.7, "spread_bp": 0.13,
                          "worst_trade": None}),
    ("TLT", "control",   {"sr": -0.17, "sr_ex": 0.04, "sr_modern": None,
                          "tpy": None, "avg_bp": None, "spread_bp": None,
                          "worst_trade": None}),
    ("GLD", "control",   {"sr": -0.09, "sr_ex": 0.14, "sr_modern": None,
                          "tpy": None, "avg_bp": None, "spread_bp": None,
                          "worst_trade": None}),
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


# ---------------------------------------------------------------- data
def from_yfinance(tickers, period="2y"):
    try:
        import yfinance as yf
    except ImportError:
        return {}
    out = {}
    try:
        df = yf.download(tickers, period=period, auto_adjust=False,
                         progress=False, group_by="ticker")
    except Exception as e:
        print(f"yfinance failed: {e}", file=sys.stderr)
        return {}
    for tk in tickers:
        try:
            sub = df[tk] if len(tickers) > 1 else df
            rows = []
            for idx, r in sub.iterrows():
                o, c = float(r["Open"]), float(r["Close"])
                if o > 0 and c > 0:
                    rows.append((str(idx)[:10], o, c))
            if len(rows) > VOL_WINDOW + 5:
                out[tk] = sorted(rows)
        except Exception:
            continue
    return out


def from_stooq(tickers, limit=None):
    """limit=None keeps the full archive -- backtest.py needs it."""
    out = {}
    for tk in tickers:
        try:
            url = f"https://stooq.com/q/d/l/?s={tk.lower()}.us&i=d"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            txt = urllib.request.urlopen(req, timeout=30).read().decode(errors="replace")
            rows = []
            for r in csv.DictReader(io.StringIO(txt)):
                try:
                    rows.append((r["Date"][:10], float(r["Open"]), float(r["Close"])))
                except (ValueError, KeyError, TypeError):
                    continue
            if len(rows) > VOL_WINDOW + 5:
                rows = sorted(rows)
                out[tk] = rows[-limit:] if limit else rows
        except Exception as e:
            print(f"stooq {tk} failed: {e}", file=sys.stderr)
    return out


def load_all(tickers, period="2y", limit=HISTORY_SESSIONS + 40):
    data = from_yfinance(tickers, period=period)
    missing = [t for t in tickers if t not in data]
    if missing:
        print(f"falling back to stooq for {missing}", file=sys.stderr)
        data.update(from_stooq(missing, limit=limit))
    return data


# ---------------------------------------------------------------- signal
def compute(rows):
    sess = [(d, c / o - 1) for d, o, c in rows if o > 0]
    if len(sess) < VOL_WINDOW + 2:
        return None
    prev_date, prev_ret = sess[-1]
    hist = [r for _, r in sess[-(VOL_WINDOW + 1):-1]]
    sd = statistics.pstdev(hist)
    if sd <= 0:
        return None
    z = prev_ret / sd
    pos = 0.0
    action = "none"
    if abs(z) >= THRESHOLD:
        pos = max(-CAP, min(CAP, -z))
        action = "buy" if pos > 0 else "short"
    return {
        "prev_session": prev_date,
        "prev_return": round(prev_ret, 6),
        "vol": round(sd, 6),
        "z": round(z, 3),
        "action": action,
        "position": round(pos, 3),
        "price": round(rows[-1][2], 2),
        "trigger_up": round(THRESHOLD * sd, 6),
        "trigger_dn": round(-THRESHOLD * sd, 6),
    }


# ---------------------------------------------------------------- prices
def write_prices(raw, generated_at):
    """Publish the daily bars for the dashboard's price-history view.

    Parallel arrays rather than a list of objects -- same information, less
    than half the bytes, and the page is fetching this on every load.
    """
    series = {}
    for tk, rows in raw.items():
        tail = rows[-HISTORY_SESSIONS:]
        series[tk] = {
            "dates": [d for d, _, _ in tail],
            "open":  [round(o, 2) for _, o, _ in tail],
            "close": [round(c, 2) for _, _, c in tail],
        }
    payload = {"generated_at": generated_at, "sessions": HISTORY_SESSIONS,
               "series": series}
    with open(os.path.join(DATA, "prices.json"), "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    counts = ", ".join(f"{tk} {len(s['dates'])}" for tk, s in sorted(series.items()))
    print(f"prices.json: {counts}")


def main():
    os.makedirs(DATA, exist_ok=True)
    tickers = [t for t, _, _ in UNIVERSE]
    raw = load_all(tickers)
    if not raw:
        print("no data from any source", file=sys.stderr)
        sys.exit(1)

    instruments, last_session = [], None
    for tk, tier, stats in UNIVERSE:
        if tk not in raw:
            instruments.append({"ticker": tk, "tier": tier, "stats": stats,
                                "error": "no data"})
            continue
        sig = compute(raw[tk])
        if sig is None:
            instruments.append({"ticker": tk, "tier": tier, "stats": stats,
                                "error": "insufficient history"})
            continue
        last_session = last_session or sig["prev_session"]
        instruments.append({"ticker": tk, "tier": tier, "stats": stats, **sig})

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "generated_at": generated_at,
        "last_session": last_session,
        "threshold": THRESHOLD,
        "cap": CAP,
        "instruments": instruments,
    }

    with open(os.path.join(DATA, "signals.json"), "w") as f:
        json.dump(payload, f, indent=2)

    write_prices(raw, generated_at)

    hist_path = os.path.join(DATA, "history.json")
    hist = []
    if os.path.exists(hist_path):
        try:
            hist = json.load(open(hist_path))
        except Exception:
            hist = []
    entry = {"session": last_session,
             "z": {i["ticker"]: i.get("z") for i in instruments},
             "action": {i["ticker"]: i.get("action") for i in instruments}}
    if not hist or hist[-1].get("session") != last_session:
        hist.append(entry)
    else:
        hist[-1] = entry
    with open(hist_path, "w") as f:
        json.dump(hist[-500:], f, indent=2)

    fired = [i["ticker"] for i in instruments if i.get("action", "none") != "none"]
    print(f"session {last_session} | firing: {fired or 'none'}")


if __name__ == "__main__":
    main()
