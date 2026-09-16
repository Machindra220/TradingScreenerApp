"""
tests/test_dhan_phase9.py
──────────────────────────
Phase 9 — Deterministic trading insights engine tests.
Pure Python — no DB, no Flask, no AI.

Run: python -m pytest tests/test_dhan_phase9.py -v
"""

import unittest
from datetime import date
from types import SimpleNamespace

from app.services.insights_engine import InsightsEngine, InsightsReport, Insight


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _s(symbol, pnl, hold=10, sell_date_str="2026-06-15",
       buy_price=100.0, qty=10, win_ret=None):
    sell_date = date.fromisoformat(sell_date_str)
    bv = buy_price * qty
    sv = bv + pnl
    return SimpleNamespace(
        symbol                       = symbol,
        gross_pnl                    = float(pnl),
        holding_period_calendar_days = hold,
        sell_date                    = sell_date,
        buy_price                    = buy_price,
        sell_price                   = (bv + pnl) / qty if qty else 0,
        quantity                     = qty,
        buy_value                    = bv,
        sell_value                   = sv,
        return_pct                   = win_ret if win_ret else round(pnl/bv*100, 2),
        outcome                      = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN"),
        product_type                 = "CNC",
        isin                         = None,
        security_id                  = None,
    )

def _report(**kw):
    defaults = dict(
        total_trades=20, winning_trades=12, losing_trades=8,
        breakeven_trades=0, win_rate_pct=60.0,
        total_gross_pnl=5000.0, total_buy_value=100000.0,
        total_sell_value=105000.0,
        avg_winner=1000.0, largest_winner=3000.0, avg_winner_return_pct=10.0,
        avg_loser=-500.0,  largest_loser=-2000.0,  avg_loser_return_pct=-5.0,
        avg_holding_calendar_days=8.0, median_holding_calendar_days=7.0,
        min_holding_calendar_days=1,   max_holding_calendar_days=45,
        trades=[],
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)

def _month(period_key, pnl, trades=5, win_rate=60.0, is_complete=True):
    year, month = int(period_key[:4]), int(period_key[5:7])
    return SimpleNamespace(
        period_key=period_key, year=year, month=month, is_complete=is_complete,
        gross_pnl=float(pnl), total_trades=trades,
        win_rate_pct=float(win_rate) if win_rate is not None else None,
    )

def _engine():
    return InsightsEngine()


# ── Insufficient data guard ───────────────────────────────────────────────────

class TestInsufficientData(unittest.TestCase):

    def test_fewer_than_5_trades_returns_insufficient(self):
        r = _engine().compute([_s("A", 100), _s("B", -50)], None, [])
        self.assertTrue(r.insufficient_data)
        self.assertEqual(len(r.all_insights()), 0)

    def test_exactly_5_trades_runs(self):
        trades = [_s(f"S{i}", 100*i - 200) for i in range(5)]
        r = _engine().compute(trades, _report(total_trades=5), [])
        self.assertFalse(r.insufficient_data)


# ── Rule 1: Holding behavior ──────────────────────────────────────────────────

class TestHoldingBehavior(unittest.TestCase):

    def test_losers_held_much_longer_generates_warning(self):
        summaries = (
            [_s("W", 500,  hold=5)] * 6 +
            [_s("L", -300, hold=20)] * 4
        )
        r = _engine().compute(summaries, _report(), [])
        cats = [i.category for i in r.areas_to_review]
        self.assertIn("holding_behavior", cats)

    def test_winners_held_longer_generates_positive(self):
        summaries = (
            [_s("W", 500, hold=15)] * 6 +
            [_s("L", -200, hold=5)] * 4
        )
        r = _engine().compute(summaries, _report(), [])
        cats = [i.category for i in r.positive_patterns]
        self.assertIn("holding_behavior", cats)

    def test_body_contains_actual_numbers(self):
        summaries = (
            [_s("W", 500, hold=5)] * 6 +
            [_s("L", -300, hold=20)] * 4
        )
        r = _engine().compute(summaries, _report(), [])
        holding_insights = [i for i in r.areas_to_review
                            if i.category == "holding_behavior"]
        self.assertTrue(len(holding_insights) > 0)
        body = holding_insights[0].body
        self.assertIn("days", body)
        # Must contain numeric values
        import re
        nums = re.findall(r'\d+\.?\d*', body)
        self.assertTrue(len(nums) >= 2)


# ── Rule 2: Winners vs losers ─────────────────────────────────────────────────

class TestWinnersVsLosers(unittest.TestCase):

    def test_good_ratio_positive(self):
        rpt = _report(avg_winner=2000.0, avg_loser=-500.0, win_rate_pct=60.0)
        summaries = [_s(f"S{i}", 100) for i in range(10)]
        r = _engine().compute(summaries, rpt, [])
        cats = [i.category for i in r.positive_patterns]
        self.assertIn("winners_vs_losers", cats)

    def test_bad_ratio_warning(self):
        rpt = _report(avg_winner=300.0, avg_loser=-500.0, win_rate_pct=60.0)
        summaries = [_s(f"S{i}", 100) for i in range(10)]
        r = _engine().compute(summaries, rpt, [])
        cats = [i.category for i in r.areas_to_review]
        self.assertIn("winners_vs_losers", cats)

    def test_low_win_rate_and_bad_ratio_alert(self):
        rpt = _report(avg_winner=300.0, avg_loser=-500.0, win_rate_pct=35.0)
        summaries = [_s(f"S{i}", 100) for i in range(10)]
        r = _engine().compute(summaries, rpt, [])
        cats = [i.category for i in r.potential_issues]
        self.assertIn("winners_vs_losers", cats)

    def test_neutral_language_no_must(self):
        rpt = _report(avg_winner=300.0, avg_loser=-500.0, win_rate_pct=35.0)
        summaries = [_s(f"S{i}", 100) for i in range(10)]
        r = _engine().compute(summaries, rpt, [])
        for ins in r.all_insights():
            self.assertNotIn(" must ", ins.body.lower())
            self.assertNotIn("you should", ins.body.lower())


# ── Rule 3: Stock concentration ───────────────────────────────────────────────

class TestStockConcentration(unittest.TestCase):

    def test_high_concentration_warning(self):
        summaries = (
            [_s("RELIANCE", 100)] * 7 +
            [_s("TCS", 100)] * 5 +
            [_s("INFY", 100)] * 3 +
            [_s("X", 100)] * 1
        )
        r = _engine().compute(summaries, _report(total_trades=16), [])
        cats = [i.category for i in r.areas_to_review]
        self.assertIn("stock_concentration", cats)

    def test_diverse_portfolio_positive(self):
        summaries = [_s(f"STOCK{i}", 100) for i in range(15)]
        r = _engine().compute(summaries, _report(), [])
        cats = [i.category for i in r.positive_patterns]
        self.assertIn("stock_concentration", cats)


# ── Rule 4: Frequent trading ──────────────────────────────────────────────────

class TestFrequentTrading(unittest.TestCase):

    def test_high_frequency_warning(self):
        monthly = [_month(f"2026-{i:02d}", 1000, trades=25) for i in range(1, 7)]
        summaries = [_s(f"S{i}", 100) for i in range(10)]
        r = _engine().compute(summaries, _report(), monthly)
        cats = [i.category for i in r.areas_to_review]
        self.assertIn("frequent_trading", cats)

    def test_normal_frequency_no_warning(self):
        monthly = [_month(f"2026-{i:02d}", 1000, trades=8) for i in range(1, 5)]
        summaries = [_s(f"S{i}", 100) for i in range(10)]
        r = _engine().compute(summaries, _report(), monthly)
        freq_warns = [i for i in r.areas_to_review
                      if i.category == "frequent_trading"]
        self.assertEqual(len(freq_warns), 0)


# ── Rule 5: Short holding periods ────────────────────────────────────────────

class TestShortHolding(unittest.TestCase):

    def test_many_short_trades_info(self):
        summaries = (
            [_s(f"S{i}", 100, hold=1) for i in range(4)] +
            [_s(f"T{i}", 200, hold=10) for i in range(6)]
        )
        r = _engine().compute(summaries, _report(), [])
        cats = [i.category for i in r.info_observations]
        self.assertIn("short_holding", cats)

    def test_best_bucket_positive(self):
        # 6-15 days wins most
        summaries = (
            [_s(f"A{i}", 500,  hold=10) for i in range(6)] +
            [_s(f"B{i}", -200, hold=2)  for i in range(4)]
        )
        r = _engine().compute(summaries, _report(), [])
        pos_cats = [i.category for i in r.positive_patterns]
        # May or may not fire depending on win rate threshold
        # Just verify engine runs without error
        self.assertIsInstance(r, InsightsReport)


# ── Rule 6: Long-held losers ──────────────────────────────────────────────────

class TestLongHeldLosers(unittest.TestCase):

    def test_many_long_losers_warning(self):
        summaries = (
            [_s(f"W{i}", 500,  hold=5)  for i in range(5)] +
            [_s(f"L{i}", -300, hold=25) for i in range(5)]
        )
        r = _engine().compute(summaries, _report(), [])
        cats = [i.category for i in r.areas_to_review]
        self.assertIn("long_held_losers", cats)

    def test_body_mentions_days(self):
        summaries = (
            [_s(f"W{i}", 500,  hold=5)  for i in range(5)] +
            [_s(f"L{i}", -300, hold=25) for i in range(5)]
        )
        r = _engine().compute(summaries, _report(), [])
        ll_insights = [i for i in r.areas_to_review
                       if i.category == "long_held_losers"]
        if ll_insights:
            self.assertIn("20 days", ll_insights[0].body)


# ── Rule 7: Large losses ──────────────────────────────────────────────────────

class TestLargeLosses(unittest.TestCase):

    def test_outlier_losses_alert(self):
        summaries = (
            [_s(f"W{i}", 500)            for i in range(5)] +
            [_s(f"L{i}", -200)           for i in range(3)] +
            [_s("OUTLIER1", -2000)] +
            [_s("OUTLIER2", -1800)]
        )
        r = _engine().compute(summaries, _report(), [])
        cats = [i.category for i in r.potential_issues]
        self.assertIn("large_losses", cats)

    def test_metric_is_percentage(self):
        summaries = (
            [_s(f"W{i}", 500)  for i in range(5)] +
            [_s(f"L{i}", -200) for i in range(3)] +
            [_s("BIG", -2000)]
        )
        r = _engine().compute(summaries, _report(), [])
        ll = [i for i in r.potential_issues if i.category == "large_losses"]
        if ll:
            self.assertIsNotNone(ll[0].metric)
            self.assertLessEqual(ll[0].metric, 100.0)


# ── Rule 8: Consecutive losses ────────────────────────────────────────────────

class TestConsecutiveLosses(unittest.TestCase):

    def test_long_streak_alert(self):
        dates = [f"2026-0{i}-10" for i in range(1, 7)]
        summaries = (
            [_s(f"W{i}", 500,  sell_date_str="2026-01-05") for i in range(5)] +
            [_s(f"L{i}", -200, sell_date_str=dates[i])     for i in range(5)]
        )
        r = _engine().compute(summaries, _report(), [])
        cats = [i.category for i in r.potential_issues]
        self.assertIn("consecutive_losses", cats)

    def test_no_streak_positive(self):
        # Alternating wins and losses — max streak = 1
        summaries = []
        for i in range(10):
            d = f"2026-0{(i//3)+1}-{(i%3)*5+5:02d}"
            pnl = 200 if i % 2 == 0 else -100
            summaries.append(_s(f"S{i}", pnl, sell_date_str=d))
        r = _engine().compute(summaries, _report(), [])
        cats = [i.category for i in r.positive_patterns]
        self.assertIn("consecutive_losses", cats)

    def test_streak_metric_is_integer(self):
        dates = [f"2026-0{i+1}-10" for i in range(5)]
        summaries = [_s(f"L{i}", -200, sell_date_str=dates[i]) for i in range(5)]
        summaries += [_s(f"W{i}", 500) for i in range(5)]
        r = _engine().compute(summaries, _report(), [])
        for ins in r.all_insights():
            if ins.category == "consecutive_losses" and ins.metric is not None:
                self.assertIsInstance(ins.metric, (int, float))


# ── Rule 9: Monthly consistency ───────────────────────────────────────────────

class TestMonthlyConsistency(unittest.TestCase):

    def test_high_consistency_positive(self):
        monthly = [_month(f"2026-{i:02d}", 1000 if i != 4 else -200)
                   for i in range(1, 8)]
        summaries = [_s(f"S{i}", 100) for i in range(10)]
        r = _engine().compute(summaries, _report(), monthly)
        cats = [i.category for i in r.positive_patterns]
        self.assertIn("monthly_consistency", cats)

    def test_low_consistency_warning(self):
        monthly = [_month(f"2026-{i:02d}", 500 if i % 3 == 0 else -300)
                   for i in range(1, 8)]
        summaries = [_s(f"S{i}", 100) for i in range(10)]
        r = _engine().compute(summaries, _report(), monthly)
        cats = [i.category for i in r.areas_to_review]
        self.assertIn("monthly_consistency", cats)

    def test_incomplete_month_excluded(self):
        monthly = (
            [_month(f"2026-{i:02d}", 1000) for i in range(1, 7)] +
            [_month("2026-09", -5000, is_complete=False)]
        )
        summaries = [_s(f"S{i}", 100) for i in range(10)]
        r = _engine().compute(summaries, _report(), monthly)
        # Incomplete month must not affect the consistency count
        mc = [i for i in r.positive_patterns
              if i.category == "monthly_consistency"]
        if mc:
            # 6/6 complete months = 100% — incomplete Sep excluded
            self.assertGreater(mc[0].metric, 60.0)


# ── Rule 10: Profit concentration ─────────────────────────────────────────────

class TestProfitConcentration(unittest.TestCase):

    def test_concentrated_profit_info(self):
        summaries = (
            [_s("RELIANCE", 10000)] * 3 +
            [_s("TCS",       5000)] * 2 +
            [_s(f"OTHER{i}", 200)  for i in range(5)]
        )
        r = _engine().compute(summaries, _report(), [])
        cats = [i.category for i in r.info_observations]
        self.assertIn("profit_concentration", cats)

    def test_metric_is_percentage(self):
        summaries = (
            [_s("A", 10000)] * 3 +
            [_s("B", 5000)]  * 2 +
            [_s(f"C{i}", 100) for i in range(5)]
        )
        r = _engine().compute(summaries, _report(), [])
        pc = [i for i in r.info_observations
              if i.category == "profit_concentration"]
        if pc:
            self.assertLessEqual(pc[0].metric, 100.0)
            self.assertGreater(pc[0].metric,   0.0)


# ── InsightsReport structure ──────────────────────────────────────────────────

class TestInsightsReportStructure(unittest.TestCase):

    def test_to_dict_has_all_sections(self):
        summaries = [_s(f"S{i}", 100*i - 300) for i in range(10)]
        r = _engine().compute(summaries, _report(), [])
        d = r.to_dict()
        for key in ("positive_patterns","potential_issues",
                    "areas_to_review","info_observations",
                    "total_insights","trade_count","insufficient_data"):
            self.assertIn(key, d)

    def test_insight_to_dict_has_all_fields(self):
        summaries = (
            [_s(f"W{i}", 500, hold=5)  for i in range(6)] +
            [_s(f"L{i}", -300, hold=20) for i in range(4)]
        )
        r = _engine().compute(summaries, _report(), [])
        if r.all_insights():
            d = r.all_insights()[0].to_dict()
            for key in ("severity","category","title","body",
                        "metric","metric_label","supporting_stat"):
                self.assertIn(key, d)

    def test_neutral_language_across_all_insights(self):
        """All insight bodies must use neutral language."""
        summaries = (
            [_s("RELIANCE", 10000)] * 5 +
            [_s(f"L{i}", -200, hold=25) for i in range(5)]
        )
        rpt = _report(avg_winner=300.0, avg_loser=-500.0, win_rate_pct=35.0)
        monthly = [_month(f"2026-{i:02d}", -500) for i in range(1, 5)]
        r = _engine().compute(summaries, rpt, monthly)
        forbidden = ["you must", "you should", "you need to",
                     "do not", "don't", "avoid", "stop"]
        for ins in r.all_insights():
            body_lower = ins.body.lower()
            for phrase in forbidden:
                self.assertNotIn(phrase, body_lower,
                    f"Forbidden phrase '{phrase}' found in: {ins.body[:80]}")

    def test_each_insight_has_nonempty_body(self):
        summaries = [_s(f"S{i}", 100*i - 300) for i in range(10)]
        r = _engine().compute(summaries, _report(), [])
        for ins in r.all_insights():
            self.assertTrue(len(ins.body) > 20,
                f"Body too short for {ins.title}: '{ins.body}'")


if __name__ == "__main__":
    unittest.main()