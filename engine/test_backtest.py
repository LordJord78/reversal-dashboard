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

import os
import random
import statistics
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import signals as S
import backtest as B


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
