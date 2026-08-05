"""
test_backtest.py -- the invariants that keep the two engines honest.

backtest.py's docstring has always claimed "the two must agree on the most
recent session or one of them is wrong; test_backtest.py asserts that." Until
now that file did not exist, so nothing asserted it. This is that file.

Deliberately stdlib unittest and deliberately synthetic data. A test that
downloads bars fails when a vendor has an outage, and a test that fails for
reasons unrelated to the code is a test people learn to ignore -- which is
worse than no test, because the workflow still shows a green tick when it
passes for the wrong reason.

Run:  python -m unittest discover -s engine -p 'test_*.py' -v
"""

import math
import os
import random
import statistics
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import signals as S
import backtest as B
import options as O
import options_backtest as OB


def synthetic(n=400, seed=7, shock_at=None, shock=0.09):
    """Deterministic open/close bars on consecutive calendar days.

    Calendar days rather than trading days on purpose: neither engine knows
    what a weekend is, they both just consume an ordered list of bars, and
    pretending otherwise here would test a calendar we do not have.
    """
    rng = random.Random(seed)
    rows, day = [], datetime(2020, 1, 1, tzinfo=timezone.utc)
    price = 100.0
    for i in range(n):
        o = price
        r = rng.gauss(0, 0.008)
        if shock_at is not None and i == shock_at:
            r = shock
        c = o * (1 + r)
        rows.append(((day + timedelta(days=i)).strftime("%Y-%m-%d"),
                     round(o, 4), round(c, 4)))
        price = c * (1 + rng.gauss(0, 0.003))
    return rows


class TestEnginesAgree(unittest.TestCase):
    """compute() scores one session; replay() scores all of them.

    Where they overlap they must produce the same number. If this ever fails,
    the dashboard and the track record on the ticker pages are describing two
    different strategies, and there is no way to tell from the pages which one
    you are actually trading.
    """

    def test_z_matches_on_the_shared_session(self):
        # The shared session is the second-to-last bar: the newest one
        # replay() can score, since it needs a following session to trade in.
        rows = synthetic(shock_at=398)
        # compute() reads the LAST bar it is given, so hand it everything up
        # to and including the session replay() treats as signal index -2.
        sig = S.compute(rows[:-1])
        self.assertIsNotNone(sig)

        trades = B.replay(rows, spread_bp=0.14)
        match = [t for t in trades if t["sd"] == rows[-2][0]]
        if not match:
            self.assertEqual(sig["action"], "none",
                             "replay saw no trade but compute fired")
            return
        self.assertAlmostEqual(sig["z"], match[0]["z"], places=3)
        self.assertEqual(sig["position"], match[0]["pos"])

    def test_the_shock_actually_fires(self):
        """Guards the test above from passing because nothing ever triggers."""
        rows = synthetic(shock_at=398)
        sig = S.compute(rows[:-1])
        self.assertEqual(sig["action"], "short")
        self.assertLessEqual(sig["position"], -S.THRESHOLD + 0.001)

    def test_vol_window_excludes_the_scored_session(self):
        """z must not be damped by the move it is measuring."""
        rows = synthetic(shock_at=399)
        sess = [(d, c / o - 1) for d, o, c in rows]
        hist = [r for _, r in sess[-(S.VOL_WINDOW + 1):-1]]
        expected = sess[-1][1] / statistics.pstdev(hist)
        self.assertAlmostEqual(S.compute(rows)["z"], round(expected, 3),
                               places=3)


class TestIncompleteBarGuard(unittest.TestCase):
    """The 2026-08-04 fault: a partial bar scored as a finished session."""

    def rows(self):
        return [("2026-08-03", 100.0, 101.0), ("2026-08-04", 101.0, 103.0)]

    def test_drops_a_bar_whose_session_has_not_closed(self):
        # 13:30 UTC on the 4th -- market open, bar still moving.
        now = datetime(2026, 8, 4, 13, 30, tzinfo=timezone.utc)
        kept = S.drop_incomplete(self.rows(), now=now)
        self.assertEqual([r[0] for r in kept], ["2026-08-03"])

    def test_keeps_a_settled_bar(self):
        now = datetime(2026, 8, 4, 21, 0, tzinfo=timezone.utc)
        kept = S.drop_incomplete(self.rows(), now=now)
        self.assertEqual([r[0] for r in kept], ["2026-08-03", "2026-08-04"])

    def test_boundary_is_exclusive_below_the_close(self):
        now = datetime(2026, 8, 4, 20, 59, tzinfo=timezone.utc)
        kept = S.drop_incomplete(self.rows(), now=now)
        self.assertEqual([r[0] for r in kept], ["2026-08-03"])

    def test_pops_more_than_one_unsettled_row(self):
        rows = self.rows() + [("2026-08-05", 103.0, 104.0)]
        now = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)
        kept = S.drop_incomplete(rows, now=now)
        self.assertEqual([r[0] for r in kept], ["2026-08-03"])

    def test_a_late_run_produces_yesterdays_signal_not_todays(self):
        """The whole point of the guard, stated as behaviour."""
        rows = synthetic(n=400)
        late = datetime.strptime(rows[-1][0], "%Y-%m-%d").replace(
            tzinfo=timezone.utc) + timedelta(hours=14)
        kept = S.drop_incomplete(rows, now=late)
        self.assertEqual(S.compute(kept)["prev_session"], rows[-2][0])


class TestCostModel(unittest.TestCase):
    def test_slippage_charged_on_both_prints(self):
        """MOO in and MOC out are two fills, so two slippage allowances."""
        c = B.cost_bp(spread_bp=0.14, position=1.0)
        self.assertAlmostEqual(c, 0.14 + 2 * B.SLIPPAGE_BP, places=6)

    def test_cost_scales_with_leverage(self):
        self.assertAlmostEqual(B.cost_bp(0.14, 2.0),
                               2 * B.cost_bp(0.14, 1.0), places=6)

    def test_missing_spread_falls_back_wide(self):
        """An unknown spread must never be cheaper than a known one."""
        self.assertGreater(B.cost_bp(None, 1.0), B.cost_bp(0.56, 1.0))

    def test_net_is_never_better_than_gross(self):
        trades = B.replay(synthetic(shock_at=380), spread_bp=0.14)
        self.assertTrue(trades)
        for t in trades:
            self.assertLess(t["n"], t["g"])


class TestSharpe(unittest.TestCase):
    def test_sat_out_sessions_are_in_the_denominator(self):
        """Sharpe over trade days only would roughly triple the number."""
        trades = B.replay(synthetic(shock_at=380), spread_bp=0.14)
        few = B.sharpe(trades, n_sessions=400)
        many = B.sharpe(trades, n_sessions=4000)
        self.assertIsNotNone(few)
        self.assertLess(abs(many), abs(few))

    def test_no_trades_is_none_not_zero(self):
        self.assertIsNone(B.sharpe([], n_sessions=400))


class TestRowAlignment(unittest.TestCase):
    """Audit finding 1: a filtered sess indexed against unfiltered rows.

    A bar with open <= 0 is dropped from sess but not from rows, so every
    index after it points at a different session. Nothing raises and the
    output stays plausible -- the trade is simply booked against the wrong
    day's open and close.

    A bad bar is INSERTED into a known-good series here. Once filtered it is
    gone again, so the two runs must produce byte-identical trades. Anything
    else is the misalignment.
    """

    def test_a_zero_open_bar_does_not_shift_later_trades(self):
        clean = synthetic(shock_at=200)
        bad = clean[:10] + [("2020-01-11", 0.0, 0.0)] + clean[10:]

        t_clean = B.replay(clean, spread_bp=0.14)
        t_bad = B.replay(bad, spread_bp=0.14)

        self.assertTrue(t_clean)
        self.assertEqual([(t["sd"], t["d"]) for t in t_clean],
                         [(t["sd"], t["d"]) for t in t_bad])
        self.assertEqual(t_clean, t_bad)

    def test_entry_and_exit_come_from_the_session_that_was_traded(self):
        rows = synthetic(shock_at=200)
        by_date = {d: (o, c) for d, o, c in rows}
        for t in B.replay(rows, spread_bp=0.14):
            o, c = by_date[t["d"]]
            self.assertAlmostEqual(t["o"], round(o, 2), places=2)
            self.assertAlmostEqual(t["c"], round(c, 2), places=2)

    def test_signal_session_is_the_one_before_the_traded_session(self):
        rows = synthetic(shock_at=200)
        order = [d for d, _, _ in rows]
        for t in B.replay(rows, spread_bp=0.14):
            self.assertEqual(order.index(t["d"]), order.index(t["sd"]) + 1)

    def test_compute_price_matches_the_session_it_scored(self):
        """signals.py had the same pattern: price read from unfiltered rows."""
        rows = synthetic(n=200)
        good_close = rows[-1][2]
        sig = S.compute(rows + [("2020-12-31", 0.0, 0.0)])
        self.assertEqual(sig["prev_session"], rows[-1][0])
        self.assertAlmostEqual(sig["price"], round(good_close, 2), places=2)


class TestMaxDrawdown(unittest.TestCase):
    """Audit finding 5: peak seeded from equity after the first trade."""

    def test_drawdown_into_a_losing_first_trade_is_counted(self):
        # Equity never recovers above 1.0, so measuring from curve[0] would
        # report no drawdown at all.
        self.assertAlmostEqual(B.max_drawdown([0.9, 0.95, 0.92]), -0.10,
                               places=6)

    def test_a_winning_start_is_unaffected(self):
        self.assertAlmostEqual(B.max_drawdown([1.10, 1.21, 1.089]), -0.10,
                               places=6)

    def test_monotonic_gains_have_no_drawdown(self):
        self.assertEqual(B.max_drawdown([1.05, 1.10, 1.20]), 0.0)

    def test_never_positive(self):
        trades = B.replay(synthetic(shock_at=380), spread_bp=0.14)
        eq, v = [], 1.0
        for t in trades:
            v *= (1 + t["n"])
            eq.append(v)
        self.assertLessEqual(B.max_drawdown(eq), 0.0)


class TestOptionPricing(unittest.TestCase):
    """Black-Scholes identities. If these drift, every option number does."""

    S, SIG, T = 723.68, 0.34, 1 / 252

    def test_put_call_parity(self):
        c = O.bs_price(self.S, 700, self.T, self.SIG, True)
        p = O.bs_price(self.S, 700, self.T, self.SIG, False)
        rhs = self.S - 700 * math.exp(-O.RISK_FREE * self.T)
        self.assertAlmostEqual(c - p, rhs, places=9)

    def test_expiry_is_intrinsic(self):
        self.assertAlmostEqual(O.bs_price(self.S, 700, 0, self.SIG, True),
                               self.S - 700, places=9)
        self.assertEqual(O.bs_price(self.S, 800, 0, self.SIG, True), 0.0)
        self.assertAlmostEqual(O.bs_price(self.S, 800, 0, self.SIG, False),
                               800 - self.S, places=9)

    def test_extrinsic_never_negative(self):
        for k in (600, 700, 723, 750, 900):
            self.assertGreaterEqual(
                O.extrinsic(self.S, k, self.T, self.SIG, True), 0.0)

    def test_strike_solver_hits_its_target(self):
        """Before snapping, the solved strike must reproduce the delta."""
        for target in (0.80, 0.90, 0.92, 0.95):
            for is_call in (True, False):
                k = O.strike_for_delta(self.S, self.T, self.SIG, is_call,
                                       step=0.0001, target_delta=target)
                got = abs(O.bs_delta(self.S, k, self.T, self.SIG, is_call))
                self.assertAlmostEqual(got, target, places=3)

    def test_deep_itm_carries_less_extrinsic_than_atm(self):
        """The entire reason the rule targets a high delta."""
        atm = O.strike_for_delta(self.S, self.T, self.SIG, True, 1.0, 0.50)
        itm = O.strike_for_delta(self.S, self.T, self.SIG, True, 1.0, 0.92)
        e_atm = O.extrinsic(self.S, atm, self.T, self.SIG, True)
        e_itm = O.extrinsic(self.S, itm, self.T, self.SIG, True)
        self.assertLess(e_itm, e_atm / 3)


class TestAgainstARealContract(unittest.TestCase):
    """Regression guard against real market data, not model self-consistency.

    QQQ 645 call expiring 2026-07-30 -- the session the signal fired and was
    traded. Underlying opened 674.76 and closed 683.55; the contract printed
    29.03 at 09:30 and 38.59 into the close. Pulled from the broker's expired
    contract history, so these are prices that actually happened.
    """

    K, T = 645.0, 1 / O.TRADING_DAYS
    S_OPEN, S_CLOSE = 674.76, 683.55
    REAL_OPEN, REAL_CLOSE = 29.03, 38.59
    SIGMA = O.annualise(0.010403)          # trailing 60d vol at signal time

    def test_exit_value_matches_reality(self):
        """Expiry is intrinsic. No volatility assumption survives to here."""
        modelled = O.bs_price(self.S_CLOSE, self.K, 0, self.SIGMA, True)
        self.assertLess(abs(modelled - self.REAL_CLOSE) / self.REAL_CLOSE, 0.01)

    def test_entry_within_a_few_percent(self):
        modelled = O.bs_price(self.S_OPEN, self.K, self.T,
                              self.SIGMA * O.IV_MULTIPLE, True)
        self.assertLess(abs(modelled - self.REAL_OPEN) / self.REAL_OPEN, 0.05)

    def test_entry_error_is_conservative(self):
        """The model should not flatter the option. Overstating the entry
        price understates the return."""
        modelled = O.bs_price(self.S_OPEN, self.K, self.T,
                              self.SIGMA * O.IV_MULTIPLE, True)
        self.assertGreater(modelled, self.REAL_OPEN)

    def test_iv_assumption_barely_moves_a_deep_itm_price(self):
        """The input flagged as weakest turns out not to drive this result.
        It would drive an at-the-money one."""
        lo = O.bs_price(self.S_OPEN, self.K, self.T, self.SIGMA * 1.0, True)
        hi = O.bs_price(self.S_OPEN, self.K, self.T, self.SIGMA * 2.5, True)
        self.assertLess((hi - lo) / lo, 0.02)

    def test_the_same_swing_is_large_at_the_money(self):
        """Contrast case — confirms the insensitivity is a property of moneyness
        and not of the pricer being broken."""
        atm = O.strike_for_delta(self.S_OPEN, self.T, self.SIGMA, True, 1.0, 0.50)
        lo = O.bs_price(self.S_OPEN, atm, self.T, self.SIGMA * 1.0, True)
        hi = O.bs_price(self.S_OPEN, atm, self.T, self.SIGMA * 2.5, True)
        self.assertGreater((hi - lo) / lo, 0.5)


class TestRecommendation(unittest.TestCase):
    def test_short_buys_a_put_and_buy_buys_a_call(self):
        short = O.recommend(723.68, 0.0107, "short", 1.0)
        long_ = O.recommend(723.68, 0.0107, "buy", 1.0)
        self.assertEqual(short["side"], "put")
        self.assertEqual(long_["side"], "call")

    def test_deep_itm_means_put_strike_above_and_call_strike_below(self):
        spot = 723.68
        self.assertGreater(O.recommend(spot, 0.0107, "short", 1.0)["strike"], spot)
        self.assertLess(O.recommend(spot, 0.0107, "buy", 1.0)["strike"], spot)

    def test_no_recommendation_without_a_signal(self):
        self.assertIsNone(O.recommend(723.68, 0.0107, "none", 1.0))

    def test_published_delta_is_post_snap_not_the_target(self):
        """Snapping moves the real delta; publishing the target would lie."""
        r = O.recommend(723.68, 0.0107, "buy", step=5.0)
        recomputed = abs(O.bs_delta(
            r["ref_spot"], r["strike"], r["dte"] / O.TRADING_DAYS,
            r["iv_used"], True))
        self.assertAlmostEqual(r["delta"], round(recomputed, 3), places=3)

    def test_recommendation_is_flagged_provisional(self):
        """The strike comes from the close; entry is at an open that does
        not exist yet when the engine runs."""
        self.assertTrue(O.recommend(723.68, 0.0107, "buy", 1.0)["provisional"])


class TestOptionBacktest(unittest.TestCase):
    def trades(self):
        rows = synthetic(n=1200, seed=11)
        share = B.replay(rows, spread_bp=0.14)
        opts = [o for o in (OB.option_trade(t, 0.03, 2.0, 1.0)
                            for t in share) if o]
        return share, opts

    def test_option_and_share_express_the_same_direction(self):
        """The sign bug: a short signal buys a PUT, which already carries the
        direction. Scaling by the signed position flipped every short trade
        and reported losses as gains, with plausible-looking magnitudes."""
        share, opts = self.trades()
        self.assertTrue(opts)
        agree = sum(1 for o in opts if (o["ret"] > 0) == (o["share_ret"] > 0))
        # Not 100%: cost legitimately flips trades that barely won on shares.
        self.assertGreater(agree / len(opts), 0.85)

    def test_a_short_signal_produces_a_put(self):
        _, opts = self.trades()
        for o in opts:
            self.assertEqual(o["side"], "put" if o["pos"] < 0 else "call")

    def test_costs_make_the_option_version_worse(self):
        """On data with no edge, the option must lose more than the shares."""
        share, opts = self.trades()
        s = OB.summarise(opts, share, 1200)
        self.assertLess(s["option_avg_bp"], s["share_avg_bp"])

    def test_edge_retained_withheld_on_a_negative_base(self):
        """A negative denominator inverts the ratio: a worse option result
        reads as a bigger number. Random data produced '3.39' this way."""
        share, opts = self.trades()
        # Forced rather than relying on the synthetic series happening to lose
        # money — that sign is an accident of the seed, and a test that
        # depends on it passes or fails for the wrong reason.
        for t in share:
            t["n"] = -abs(t["n"]) - 0.001
        s = OB.summarise(opts, share, 1200)
        self.assertLess(s["share_avg_bp"], 0)
        self.assertIsNone(s["edge_retained"])
        self.assertIsNotNone(s["edge_retained_undefined"])

    def test_edge_retained_computed_on_a_positive_base(self):
        share, opts = self.trades()
        for t in share:
            t["n"] = abs(t["n"]) + 0.001
        s = OB.summarise(opts, share, 1200)
        self.assertIsNotNone(s["edge_retained"])

    def test_wider_spreads_never_help(self):
        share, _ = self.trades()
        prev = None
        for sp in OB.SPREAD_GRID:
            opts = [o for o in (OB.option_trade(t, sp, 2.0, 1.0)
                                for t in share) if o]
            avg = OB.summarise(opts, share, 1200)["option_avg_bp"]
            if prev is not None:
                self.assertLessEqual(avg, prev)
            prev = avg


class TestUniverseConfig(unittest.TestCase):
    def test_hidden_instruments_are_still_scored(self):
        """Controls that stop being computed stop being controls."""
        for tk in ("TLT", "GLD", "DIA"):
            self.assertFalse(S.meta(tk)["display"])
            self.assertIn(tk, [t for t, _, _ in S.UNIVERSE])

    def test_controls_survived_the_universe_change(self):
        tiers = {t: tier for t, tier, _ in S.UNIVERSE}
        self.assertEqual(tiers["TLT"], "control")
        self.assertEqual(tiers["GLD"], "control")

    def test_new_vehicles_present_and_not_tradeable(self):
        tiers = {t: tier for t, tier, _ in S.UNIVERSE}
        for tk in ("TQQQ", "QQQM"):
            self.assertIn(tk, tiers)
            self.assertNotEqual(tiers[tk], "tradeable")

    def test_leverage_is_declared_for_tqqq(self):
        self.assertEqual(S.meta("TQQQ")["leverage"], 3.0)
        self.assertEqual(S.meta("QQQ")["leverage"], 1.0)

    def test_options_gated_to_penny_tick_names(self):
        """XLK/DIA/TLT/GLD carry a $0.05 floor above $3.00."""
        self.assertTrue(S.meta("QQQ")["options"])
        self.assertTrue(S.meta("SPY")["options"])
        for tk in ("XLK", "DIA", "TLT", "GLD", "TQQQ", "QQQM"):
            self.assertFalse(S.meta(tk)["options"])

    def test_unknown_ticker_falls_back_safely(self):
        m = S.meta("NOPE")
        self.assertFalse(m["options"])
        self.assertEqual(m["leverage"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
