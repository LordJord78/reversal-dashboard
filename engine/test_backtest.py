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


if __name__ == "__main__":
    unittest.main(verbosity=2)
