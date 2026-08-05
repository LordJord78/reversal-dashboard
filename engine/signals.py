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
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import options as O

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
    # No research-note stats exist for these two. They are not independent
    # tests of the signal -- QQQM tracks the same index as QQQ and TQQQ is
    # QQQ levered 3x -- so they are carried as execution vehicles, to answer
    # "should QQQ's signal be traded through a different instrument", not
    # "does the signal work here too". Any correlation-blind significance
    # test on them will look far better than it is.
    ("TQQQ", "watch",    {"sr": None, "sr_ex": None, "sr_modern": None,
                          "tpy": None, "avg_bp": None, "spread_bp": None,
                          "worst_trade": None}),
    ("QQQM", "watch",    {"sr": None, "sr_ex": None, "sr_modern": None,
                          "tpy": None, "avg_bp": None, "spread_bp": None,
                          "worst_trade": None}),
]

# Per-instrument configuration kept out of UNIVERSE so the (ticker, tier,
# stats) shape every other module unpacks stays stable.
#
#   display     render on the site. False still scores the instrument every
#               run -- the controls have to keep running to be controls, they
#               just do not need a card.
#   leverage    embedded leverage in the instrument itself, multiplied by the
#               position size to get true exposure.
#   options     whether to publish a strike recommendation. Gated on tick
#               size: QQQ and SPY quote in pennies at every price, the rest
#               carry a $0.05 floor above $3.00, which is a hard floor under
#               the spread on a strategy with a ~68bp edge.
#   strike_step listed strike increment near the money. VERIFY against a live
#               chain before trusting a published strike -- these are
#               reasonable defaults, not confirmed values.
META = {
    "QQQ":  {"display": True,  "leverage": 1.0, "options": True,  "strike_step": 1.0},
    "XLK":  {"display": True,  "leverage": 1.0, "options": False, "strike_step": 1.0},
    "SPY":  {"display": True,  "leverage": 1.0, "options": True,  "strike_step": 1.0},
    "TQQQ": {"display": True,  "leverage": 3.0, "options": False, "strike_step": 1.0},
    "QQQM": {"display": True,  "leverage": 1.0, "options": False, "strike_step": 1.0},
    "DIA":  {"display": False, "leverage": 1.0, "options": False, "strike_step": 1.0},
    "TLT":  {"display": False, "leverage": 1.0, "options": False, "strike_step": 1.0},
    "GLD":  {"display": False, "leverage": 1.0, "options": False, "strike_step": 1.0},
}
DEFAULT_META = {"display": True, "leverage": 1.0, "options": False,
                "strike_step": 1.0}


def meta(tk):
    return META.get(tk, DEFAULT_META)

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


# A session dated D is settled only after its close. US equity closes land at
# 20:00 UTC (EDT) or 21:00 UTC (EST); we wait for the later of the two so this
# needs no timezone database. zoneinfo has no data on Windows unless tzdata is
# installed, and the guard has to behave identically on a laptop and on the
# Actions runner -- a guard that silently no-ops on one of them is worse than
# no guard, because it looks like it is working.
SESSION_CLOSE_UTC_HOUR = 21


def drop_incomplete(rows, now=None):
    """Drop trailing bars whose session has not closed yet.

    yfinance returns today's in-progress bar during market hours with Close
    set to the last trade rather than the settlement price. Scoring it treats
    a partial open->current move as a completed session.

    This is not hypothetical. On 2026-08-04 the scheduled run slipped 101
    minutes and landed 11 minutes after the open; the partial bar produced
    phantom 2-sigma fires on SPY, XLK and DIA simultaneously, all of which
    evaporated as the session went on. The rule is open-to-close on a
    COMPLETED session -- anything else is a different strategy wearing its
    name.

    Trailing bars are popped in a loop rather than checked once: a stale feed
    can hand back more than one unsettled row.
    """
    now = now or datetime.now(timezone.utc)
    out = list(rows)
    while out:
        d = datetime.strptime(out[-1][0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if now >= d + timedelta(hours=SESSION_CLOSE_UTC_HOUR):
            break
        out.pop()
    return out


def load_all(tickers, period="2y", limit=HISTORY_SESSIONS + 40):
    data = from_yfinance(tickers, period=period)
    missing = [t for t in tickers if t not in data]
    if missing:
        print(f"falling back to stooq for {missing}", file=sys.stderr)
        data.update(from_stooq(missing, limit=limit))

    # Guard here rather than in compute() so every consumer inherits it --
    # signals.py, prices.json and backtest.py all read through this function,
    # and the published chart must never show a bar the signal did not score.
    cleaned = {}
    for tk, rows in data.items():
        kept = drop_incomplete(rows)
        if len(kept) != len(rows):
            print(f"{tk}: dropped {len(rows) - len(kept)} unsettled bar(s); "
                  f"last settled session {kept[-1][0] if kept else 'none'}",
                  file=sys.stderr)
        if kept:
            cleaned[tk] = kept
    return cleaned


# ---------------------------------------------------------------- signal
def compute(rows):
    # Filter first, then derive -- so rows[-1] and sess[-1] are the same
    # session. Filtering sess alone and then reading rows[-1][2] for price
    # meant a trailing bad bar published a price from one session and a z
    # from another, with nothing to show the two disagreed.
    rows = [r for r in rows if r[1] > 0]
    sess = [(d, c / o - 1) for d, o, c in rows]
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

    instruments, sessions = [], []
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
        sessions.append(sig["prev_session"])
        m = meta(tk)
        entry = {"ticker": tk, "tier": tier, "stats": stats,
                 "display": m["display"], "leverage": m["leverage"], **sig}

        # True market exposure is the position size times whatever leverage is
        # baked into the instrument. Published even when it equals position,
        # so the one case where it does not (TQQQ at 3x) is not a special
        # field that only appears when there is bad news to deliver.
        entry["effective_exposure"] = round(sig["position"] * m["leverage"], 3)

        if m["options"] and sig["action"] != "none":
            entry["option_hint"] = O.recommend(
                spot=sig["price"], sigma_daily=sig["vol"],
                action=sig["action"], step=m["strike_step"])

        instruments.append(entry)

    # The newest session any instrument scored, not whichever one happened to
    # come first in UNIVERSE. Those differ when a feed lags, and the page
    # prints this date next to every z -- including the ones it isn't the
    # date of. A disagreement is also worth surfacing rather than averaging
    # away: it means one instrument is being scored off older bars than the
    # rest, which is the same class of fault as scoring an unsettled bar.
    last_session = max(sessions) if sessions else None
    lagging = sorted({s for s in sessions if s != last_session})
    if lagging:
        print(f"WARNING: feeds disagree on the last session. newest "
              f"{last_session}, also seen {', '.join(lagging)}", file=sys.stderr)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "generated_at": generated_at,
        "last_session": last_session,
        "stale_sessions": lagging,
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
