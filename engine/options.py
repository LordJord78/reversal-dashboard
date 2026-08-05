"""
options.py -- Black-Scholes helpers and deep-ITM strike selection.

Shared by signals.py (which publishes tomorrow's recommended contract) and
options_backtest.py (which replays every historical trade as that contract).
One module so the two can never disagree about what "the recommended strike"
means -- the same class of bug the signals/backtest agreement test exists to
catch.

WHY DEEP IN THE MONEY
---------------------
Measured on live QQQ chains 2026-08-04, 1 DTE:

    at-the-money   delta 0.560   extrinsic $4.18
    deep ITM       delta 0.926   extrinsic $0.87

Extrinsic value is the entire cost of using an option to express a one-session
directional view -- all of it decays by the close. Deep ITM buys the same
directional exposure with a fifth of the decay. That is the whole reason for
the delta target.

WHAT THIS MODEL IS NOT
----------------------
Black-Scholes with a flat volatility. Real chains have a skew, real quotes have
a spread that widens when it matters most, and IV is not realised vol. The
IV_MULTIPLE constant below is a measured stand-in for that gap, not a theory,
and it is the single assumption most likely to be wrong. Every number this
module produces should be read as an estimate with a sensitivity attached --
see options_backtest.py, which reports a grid rather than a point.
"""

import math
from statistics import NormalDist

_N = NormalDist()

TRADING_DAYS = 252
RISK_FREE = 0.04

# Target delta for the recommended contract. High enough that extrinsic is
# small, low enough that the contract still costs meaningfully less than the
# shares. 0.90-0.95 is the band; 0.92 is the middle of it.
TARGET_DELTA = 0.92

# Implied vol runs above trailing realised vol, and the gap is widest exactly
# when this strategy fires -- a 2-sigma session is what re-prices the chain.
#
# Measured 2026-08-04, the day of a +2.18% session in QQQ:
#     trailing 60d realised (annualised)   16.9%
#     ATM 1DTE implied                     34.3%
#     ratio                                2.03
#
# So 2.0. One observation, one day, one instrument.
#
# VALIDATED, AND IT MATTERS LESS THAN EXPECTED
# --------------------------------------------
# Checked against a real expired contract -- QQQ 645 call, 2026-07-30, the
# session the signal actually fired and was traded:
#
#     IV multiple   modelled entry   real entry   modelled exit   real exit
#     1.0                   29.86        29.03           38.55       38.59
#     2.0                   29.93        29.03           38.55       38.59
#     2.5                   30.15        29.03           38.55       38.59
#
# Two things follow, and the second was a surprise:
#
# 1. The exit model is exact. -0.1%. Expiry value is intrinsic and needs no
#    volatility assumption at all.
# 2. **The IV multiple barely moves a deep-ITM price.** 1.0 to 2.5 shifts the
#    modelled entry by under 1%, because extrinsic is tiny by construction --
#    which is the whole point of the delta target. The input flagged as the
#    weakest link here is nearly irrelevant for this specific contract choice.
#    It would dominate an at-the-money study. It does not dominate this one.
#
# The residual +3% entry error is not the pricer. The option's 09:30 print
# implied an underlying of 674.03 against an official open of 674.76 -- the
# same definition-of-the-open problem that decides marginal signals. It biases
# modelled entry prices UP, so the synthetic backtest understates option
# performance rather than flattering it.
IV_MULTIPLE = 2.0


def annualise(sigma_daily):
    return sigma_daily * math.sqrt(TRADING_DAYS)


def implied_from_realised(sigma_daily):
    """Trailing realised session vol -> the IV to price the contract at."""
    return annualise(sigma_daily) * IV_MULTIPLE


# ---------------------------------------------------------------- pricing
def _d1_d2(S, K, T, sigma, r):
    st = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / st
    return d1, d1 - st


def bs_price(S, K, T, sigma, is_call, r=RISK_FREE):
    """European price. T <= 0 returns intrinsic, which is the exit case."""
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        return max((S - K) if is_call else (K - S), 0.0)
    d1, d2 = _d1_d2(S, K, T, sigma, r)
    disc = K * math.exp(-r * T)
    if is_call:
        return S * _N.cdf(d1) - disc * _N.cdf(d2)
    return disc * _N.cdf(-d2) - S * _N.cdf(-d1)


def bs_delta(S, K, T, sigma, is_call, r=RISK_FREE):
    if T <= 0 or sigma <= 0:
        if is_call:
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1, _ = _d1_d2(S, K, T, sigma, r)
    return _N.cdf(d1) if is_call else _N.cdf(d1) - 1.0


def extrinsic(S, K, T, sigma, is_call, r=RISK_FREE):
    intrinsic = max((S - K) if is_call else (K - S), 0.0)
    return max(bs_price(S, K, T, sigma, is_call, r) - intrinsic, 0.0)


# ---------------------------------------------------------------- strike
def strike_for_delta(S, T, sigma, is_call, step,
                     target_delta=TARGET_DELTA, r=RISK_FREE):
    """Solve Black-Scholes for the strike giving |delta| = target, then snap.

    Inverting N(d1) is exact, so no search is needed. Snapping is to the
    NEAREST listed strike rather than always deeper in the money: rounding one
    way every time would quietly push the real delta above the target and make
    the contract more expensive than the rule says it is. The caller gets the
    post-snap delta back and should publish that, not the target.
    """
    if S <= 0 or T <= 0 or sigma <= 0 or step <= 0:
        return None
    q = target_delta if is_call else 1.0 - target_delta
    d1 = _N.inv_cdf(q)
    ln_sk = d1 * sigma * math.sqrt(T) - (r + sigma * sigma / 2) * T
    k = S / math.exp(ln_sk)
    return round(round(k / step) * step, 4)


def recommend(spot, sigma_daily, action, step, dte=1.0,
              target_delta=TARGET_DELTA):
    """The contract to buy for a firing signal.

    action 'short' -> BUY PUT, 'buy' -> BUY CALL. Buying only: selling options
    carries an uncapped tail and margin this strategy never modelled.

    dte is in sessions. 1.0 means the expiry at the end of the session being
    traded -- entry at the open, exit at the close, expiry at that same close.
    That is the minimum-extrinsic choice and it is available daily on QQQ and
    SPY. It is only safe because these chains carry late_close_state enabled,
    so the options session runs to 16:15 and the 16:00 exit lands inside it.
    """
    if action not in ("buy", "short"):
        return None
    is_call = action == "buy"
    T = dte / TRADING_DAYS
    sigma = implied_from_realised(sigma_daily)
    k = strike_for_delta(spot, T, sigma, is_call, step, target_delta)
    if k is None or k <= 0:
        return None

    price = bs_price(spot, k, T, sigma, is_call)
    delta = bs_delta(spot, k, T, sigma, is_call)
    return {
        "side": "call" if is_call else "put",
        "strike": k,
        "target_delta": target_delta,
        "delta": round(abs(delta), 3),
        "est_price": round(price, 2),
        "est_extrinsic": round(extrinsic(spot, k, T, sigma, is_call), 2),
        "iv_used": round(sigma, 4),
        "dte": dte,
        "ref_spot": round(spot, 2),
        # The strike is derived from the last close, but entry is at the next
        # open, which does not exist when the engine runs. Anything beyond a
        # small gap invalidates it.
        "provisional": True,
        "recompute_if_gap_pct": 0.5,
    }
