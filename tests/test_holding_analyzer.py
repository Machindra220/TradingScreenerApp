"""
tests/test_holding_analyzer.py
────────────────────────────────
Phase 4 — Holding period and trading behavior analysis tests.
Pure Python, no DB, no Flask.

Run: python -m pytest tests/test_holding_analyzer.py -v
"""

import unittest
from types import SimpleNamespace
from app.services.holding_analyzer import (
    HoldingAnalyzer, HoldingAnalysis, BUCKETS, _assign_bucket,
)


def _s(pnl, hold, symbol="ABC"):
    return SimpleNamespace(
        gross_pnl=float(pnl),
        holding_period_calendar_days=hold,
        symbol=symbol,
        outcome="WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN"),
    )

def _engine(): return HoldingAnalyzer()


# ── Bucket assignment ─────────────────────────────────────────────────────────

class TestBucketAssignment(unittest.TestCase):
    def test_zero_days(self):    self.assertEqual(_assign_bucket(0),  "0-1")
    def test_one_day(self):      self.assertEqual(_assign_bucket(1),  "0-1")
    def test_two_days(self):     self.assertEqual(_assign_bucket(2),  "2-5")
    def test_five_days(self):    self.assertEqual(_assign_bucket(5),  "2-5")
    def test_six_days(self):     self.assertEqual(_assign_bucket(6),  "6-10")
    def test_ten_days(self):     self.assertEqual(_assign_bucket(10), "6-10")
    def test_eleven_days(self):  self.assertEqual(_assign_bucket(11), "11-20")
    def test_twenty_days(self):  self.assertEqual(_assign_bucket(20), "11-20")
    def test_twenty_one(self):   self.assertEqual(_assign_bucket(21), "21-30")
    def test_thirty_days(self):  self.assertEqual(_assign_bucket(30), "21-30")
    def test_thirty_one(self):   self.assertEqual(_assign_bucket(31), "31-60")
    def test_sixty_days(self):   self.assertEqual(_assign_bucket(60), "31-60")
    def test_sixty_one(self):    self.assertEqual(_assign_bucket(61), "60+")
    def test_hundred_days(self): self.assertEqual(_assign_bucket(100),"60+")


# ── Summary stats ─────────────────────────────────────────────────────────────

class TestSummaryStats(unittest.TestCase):
    def setUp(self):
        self.r = _engine().compute([
            _s(500, 5), _s(-200, 15), _s(300, 30), _s(-100, 2), _s(50, 10),
        ])

    def test_total_trades(self):       self.assertEqual(self.r.total_trades, 5)
    def test_trades_with_days(self):   self.assertEqual(self.r.trades_with_days, 5)
    def test_overall_min(self):        self.assertEqual(self.r.overall_min, 2)
    def test_overall_max(self):        self.assertEqual(self.r.overall_max, 30)
    def test_overall_avg(self):
        self.assertAlmostEqual(self.r.overall_avg_days, (5+15+30+2+10)/5)
    def test_overall_median(self):
        self.assertAlmostEqual(self.r.overall_median, 10.0)
    def test_empty_input(self):
        r = _engine().compute([])
        self.assertEqual(r.total_trades, 0)
        self.assertIsNone(r.overall_avg_days)
    def test_no_holding_days_skipped(self):
        trades = [_s(100, 5), SimpleNamespace(gross_pnl=200.0,
                  holding_period_calendar_days=None, symbol="X")]
        r = _engine().compute(trades)
        self.assertEqual(r.trades_with_days, 1)


# ── Bucket metrics ────────────────────────────────────────────────────────────

class TestBucketMetrics(unittest.TestCase):
    def setUp(self):
        trades = (
            [_s(500,  0)] * 2 +   # 0-1: 2 wins
            [_s(-200, 3)] * 3 +   # 2-5: 3 losses
            [_s(300,  8)] * 4 +   # 6-10: 4 wins
            [_s(-100,15)] * 1     # 11-20: 1 loss
        )
        self.buckets = {b.label: b for b in _engine().compute(trades).buckets}

    def test_zero_one_bucket_count(self):  self.assertEqual(self.buckets["0-1"].count, 2)
    def test_zero_one_wins(self):          self.assertEqual(self.buckets["0-1"].wins, 2)
    def test_two_five_losses(self):        self.assertEqual(self.buckets["2-5"].losses, 3)
    def test_six_ten_win_rate(self):       self.assertAlmostEqual(self.buckets["6-10"].win_rate, 100.0)
    def test_eleven_twenty_loss_count(self):self.assertEqual(self.buckets["11-20"].losses, 1)
    def test_empty_bucket_has_none_win_rate(self):
        self.assertIsNone(self.buckets["21-30"].win_rate)
    def test_bucket_pnl_totals(self):
        self.assertAlmostEqual(self.buckets["2-5"].total_pnl, -600.0)
    def test_bucket_avg_pnl(self):
        self.assertAlmostEqual(self.buckets["6-10"].avg_pnl, 300.0)
    def test_all_buckets_present(self):
        labels = {b.label for b in self.buckets.values()}
        self.assertEqual(labels, {l for l,_,_ in BUCKETS})
    def test_to_dict_structure(self):
        d = self.buckets["6-10"].to_dict()
        for k in ("label","count","wins","losses","win_rate","total_pnl","avg_pnl"):
            self.assertIn(k, d)


# ── Winner vs loser comparison ────────────────────────────────────────────────

class TestHoldingComparison(unittest.TestCase):
    def setUp(self):
        trades = (
            [_s(500,  5)] * 5 +   # winners: avg 5d
            [_s(-300, 25)] * 5    # losers:  avg 25d
        )
        self.c = _engine().compute(trades).comparison

    def test_winner_avg(self):       self.assertAlmostEqual(self.c.winner_avg, 5.0)
    def test_loser_avg(self):        self.assertAlmostEqual(self.c.loser_avg, 25.0)
    def test_winner_count(self):     self.assertEqual(self.c.winner_count, 5)
    def test_loser_count(self):      self.assertEqual(self.c.loser_count, 5)
    def test_winner_min_max(self):
        self.assertEqual(self.c.winner_min, 5)
        self.assertEqual(self.c.winner_max, 5)
    def test_observation_set(self):  self.assertIsNotNone(self.c.observation)
    def test_observation_contains_days(self):
        self.assertIn("days", self.c.observation)
    def test_no_prescriptive_language(self):
        obs = self.c.observation.lower()
        for phrase in ["you must","you should","you need to","stop","avoid"]:
            self.assertNotIn(phrase, obs)
    def test_winners_held_longer_observation(self):
        trades = [_s(500, 20)] * 5 + [_s(-200, 5)] * 5
        c = _engine().compute(trades).comparison
        self.assertIsNotNone(c.observation)
        self.assertIn("winning", c.observation.lower())
    def test_only_winners_no_crash(self):
        r = _engine().compute([_s(100, 5)] * 5)
        self.assertIsNotNone(r.comparison)
        self.assertIsNone(r.comparison.loser_avg)
    def test_only_losers_no_crash(self):
        r = _engine().compute([_s(-100, 5)] * 5)
        self.assertIsNone(r.comparison.winner_avg)


# ── Pattern detection ─────────────────────────────────────────────────────────

class TestPatternDetection(unittest.TestCase):

    def test_best_bucket_detected(self):
        # 6-10d bucket: 4/4 wins = 100% win rate
        trades = [_s(500, 8)] * 4 + [_s(-200, 2)] * 4 + [_s(-100, 25)] * 2
        patterns = {p.category: p for p in _engine().compute(trades).patterns}
        self.assertIn("best_bucket", patterns)
        self.assertAlmostEqual(patterns["best_bucket"].metric, 100.0)

    def test_worst_bucket_detected(self):
        trades = [_s(500, 5)] * 2 + [_s(-2000, 30)] * 3
        patterns = {p.category: p for p in _engine().compute(trades).patterns}
        self.assertIn("worst_bucket", patterns)
        self.assertLess(patterns["worst_bucket"].metric, 0)

    def test_long_loser_pattern(self):
        # 4/5 losers held >20 days = 80%
        trades = (
            [_s(-300, 25)] * 4 +
            [_s(-100, 5)]  * 1 +
            [_s(500,  3)]  * 5
        )
        patterns = {p.category: p for p in _engine().compute(trades).patterns}
        self.assertIn("long_loser", patterns)
        self.assertGreaterEqual(patterns["long_loser"].metric, 30.0)

    def test_short_hold_pattern(self):
        # 5/10 trades ≤2 days = 50%
        trades = [_s(100, 1)] * 5 + [_s(-50, 15)] * 5
        patterns = {p.category: p for p in _engine().compute(trades).patterns}
        self.assertIn("short_hold", patterns)

    def test_short_hold_not_triggered_below_threshold(self):
        # Only 2/10 = 20% short → below 35% threshold
        trades = [_s(100, 1)] * 2 + [_s(-50, 15)] * 8
        patterns = {p.category: p for p in _engine().compute(trades).patterns}
        self.assertNotIn("short_hold", patterns)

    def test_repeated_loss_pattern(self):
        trades = [
            _s(-200, 5, "RELIANCE"), _s(-300, 10, "RELIANCE"),
            _s(500, 3, "TCS"),
        ]
        patterns = {p.category: p for p in _engine().compute(trades).patterns}
        self.assertIn("repeated_loss", patterns)
        self.assertEqual(patterns["repeated_loss"].metric, 2)

    def test_no_repeated_loss_when_none(self):
        # Each symbol loses only once
        trades = [_s(-100, 5, f"SYM{i}") for i in range(5)]
        patterns = {p.category: p for p in _engine().compute(trades).patterns}
        self.assertNotIn("repeated_loss", patterns)

    def test_pattern_language_neutral(self):
        trades = [_s(500, 8)] * 4 + [_s(-300, 25)] * 4 + [_s(-100, 1)] * 4
        for p in _engine().compute(trades).patterns:
            body = p.body.lower()
            for phrase in ["you must","you should","you need to","stop","avoid"]:
                self.assertNotIn(phrase, body,
                    f"Prescriptive phrase '{phrase}' in: {p.body[:60]}")

    def test_pattern_bodies_nonempty(self):
        trades = [_s(500, 8)] * 4 + [_s(-300, 25)] * 4
        for p in _engine().compute(trades).patterns:
            self.assertGreater(len(p.body), 20)


# ── to_dict structure ─────────────────────────────────────────────────────────

class TestToDictStructure(unittest.TestCase):
    def test_analysis_to_dict(self):
        trades = [_s(500, 5)] * 3 + [_s(-200, 15)] * 3
        d = _engine().compute(trades).to_dict()
        for k in ("total_trades","trades_with_days","overall_avg_days",
                  "overall_median","overall_min","overall_max",
                  "buckets","comparison","patterns"):
            self.assertIn(k, d)

    def test_bucket_to_dict(self):
        trades = [_s(100, 5)] * 3
        b = _engine().compute(trades).buckets[1]  # 2-5 bucket
        d = b.to_dict()
        for k in ("label","count","wins","losses","win_rate","total_pnl","avg_pnl"):
            self.assertIn(k, d)

    def test_comparison_to_dict(self):
        trades = [_s(100, 5)] * 3 + [_s(-50, 15)] * 3
        c = _engine().compute(trades).comparison
        d = c.to_dict()
        for k in ("winner_avg","loser_avg","winner_count","loser_count","observation"):
            self.assertIn(k, d)


# ── Edge cases ────────────────────────────────────────────────────────────────

class TestEdgeCases(unittest.TestCase):
    def test_single_trade(self):
        r = _engine().compute([_s(100, 5)])
        self.assertEqual(r.total_trades, 1)
        self.assertIsNotNone(r.overall_avg_days)

    def test_zero_pnl_trade(self):
        r = _engine().compute([_s(0, 5)] * 3 + [_s(100, 10)] * 2)
        b = {b.label: b for b in r.buckets}
        self.assertEqual(b["2-5"].breakeven, 3)

    def test_same_day_trades_in_zero_one_bucket(self):
        trades = [_s(100, 0)] * 5
        b = {b.label: b for b in _engine().compute(trades).buckets}
        self.assertEqual(b["0-1"].count, 5)

    def test_boundary_day_6_in_6_10_not_2_5(self):
        r = _engine().compute([_s(100, 6)])
        b = {b.label: b for b in r.buckets}
        self.assertEqual(b["6-10"].count, 1)
        self.assertEqual(b["2-5"].count,  0)

    def test_boundary_day_11_in_11_20_not_6_10(self):
        r = _engine().compute([_s(100, 11)])
        b = {b.label: b for b in r.buckets}
        self.assertEqual(b["11-20"].count, 1)
        self.assertEqual(b["6-10"].count,  0)

    def test_real_file_fixture(self):
        """Verify against known values from FY2024-25 Dhan Tax Report."""
        from datetime import date
        trades = [
            _s(359.5,  0,  "NACL INDUSTRIES"),
            _s(13.0,   0,  "HINDUSTAN FOODS"),
            _s(30.25,  0,  "SBI CARDS"),
            _s(570.0,  7,  "KRISHNA INSTITUTE"),
            _s(-464.0, 40, "ANANT RAJ"),
            _s(-584.0, 39, "ANANT RAJ"),
            _s(-140.0, 13, "HINDUSTAN FOODS"),
            _s(-352.0, 5,  "KALYAN JEWELLERS"),
            _s(-45.7,  1,  "KALYAN JEWELLERS"),
            _s(-309.75,37, "NALCO"),
            _s(-178.5, 20, "SUZLON ENERGY"),
            _s(1000.0, 8,  "HEG"),
            _s(-130.0, 10, "HINDUSTAN FOODS"),
            _s(-2553.5,14, "GODFREY PHILLIPS"),
            _s(-119.75,7,  "ASK AUTOMOTIVE"),
            _s(-150.0, 2,  "WELSPUN CORP"),
        ]
        r = _engine().compute(trades)
        self.assertEqual(r.total_trades, 16)
        self.assertEqual(r.trades_with_days, 16)
        # 3 intraday = 0 days → bucket 0-1
        b = {b.label: b for b in r.buckets}
        self.assertEqual(b["0-1"].count, 4)  # 3 intraday(0d) + Kalyan Jewellers(1d)
        # Anant Raj (2 losses) should trigger repeated_loss
        repeated = [p for p in r.patterns if p.category == "repeated_loss"]
        self.assertTrue(len(repeated) > 0)
        # Avg holding should be ~12.69
        self.assertAlmostEqual(r.overall_avg_days, 12.69, places=1)


if __name__ == "__main__":
    unittest.main()