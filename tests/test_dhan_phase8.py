"""
tests/test_dhan_phase8.py
──────────────────────────
Phase 8 — Monthly and yearly period analytics tests.

All tests use SimpleNamespace fixtures — no DB, no Flask required.
PeriodAnalytics is pure Python so tests run instantly.

Run: python -m pytest tests/test_dhan_phase8.py -v
"""

import unittest
from datetime import date
from types import SimpleNamespace

from app.services.period_analytics import PeriodAnalytics, MonthlyPeriod, YearlyPeriod


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _t(symbol, sell_date_str, gross_pnl, holding_days=5, buy_price=100.0, qty=10):
    sell_date = date.fromisoformat(sell_date_str)
    return SimpleNamespace(
        symbol       = symbol,
        sell_date    = sell_date,
        buy_date     = date(sell_date.year, sell_date.month, max(1, sell_date.day - holding_days)),
        gross_pnl    = float(gross_pnl),
        holding_days = holding_days,
        quantity     = qty,
        buy_price    = buy_price,
    )

def _engine():
    return PeriodAnalytics()


# ── Basic monthly bucketing ───────────────────────────────────────────────────

class TestMonthlyBucketing(unittest.TestCase):

    def test_single_month(self):
        trades = [_t("ABC", "2026-06-10", 1000), _t("XYZ", "2026-06-20", -500)]
        r = _engine().compute_monthly(trades, today=date(2026, 9, 1))
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0].period_key, "2026-06")
        self.assertEqual(r[0].total_trades, 2)
        self.assertAlmostEqual(r[0].gross_pnl, 500.0)

    def test_two_months_sorted(self):
        trades = [
            _t("ABC", "2026-07-05",  1000),
            _t("XYZ", "2026-06-15", -200),
        ]
        r = _engine().compute_monthly(trades, today=date(2026, 9, 1))
        self.assertEqual(len(r), 2)
        self.assertEqual(r[0].period_key, "2026-06")  # oldest first
        self.assertEqual(r[1].period_key, "2026-07")

    def test_trades_without_sell_date_skipped(self):
        trades = [
            _t("ABC", "2026-06-10", 1000),
            SimpleNamespace(symbol="BAD", sell_date=None, gross_pnl=500.0,
                            holding_days=5, quantity=10, buy_price=100.0),
        ]
        r = _engine().compute_monthly(trades, today=date(2026, 9, 1))
        self.assertEqual(r[0].total_trades, 1)

    def test_empty_input(self):
        r = _engine().compute_monthly([], today=date(2026, 9, 1))
        self.assertEqual(r, [])


# ── Monthly metrics ───────────────────────────────────────────────────────────

class TestMonthlyMetrics(unittest.TestCase):

    def setUp(self):
        trades = [
            _t("ABC", "2026-06-05",  2000, holding_days=10),
            _t("XYZ", "2026-06-10", -500,  holding_days=3),
            _t("DEF", "2026-06-20",  0,    holding_days=7),
        ]
        r = _engine().compute_monthly(trades, today=date(2026, 9, 1))
        self.m = r[0]

    def test_win_loss_breakeven_counts(self):
        self.assertEqual(self.m.winning_trades,   1)
        self.assertEqual(self.m.losing_trades,    1)
        self.assertEqual(self.m.breakeven_trades, 1)

    def test_win_rate(self):
        self.assertAlmostEqual(self.m.win_rate_pct, 33.33, places=1)

    def test_gross_pnl(self):
        self.assertAlmostEqual(self.m.gross_pnl, 1500.0)

    def test_avg_winner(self):
        self.assertAlmostEqual(self.m.avg_winner, 2000.0)

    def test_avg_loser(self):
        self.assertAlmostEqual(self.m.avg_loser, -500.0)

    def test_avg_holding_days(self):
        self.assertAlmostEqual(self.m.avg_holding_days, 6.67, places=1)

    def test_best_stock(self):
        self.assertEqual(self.m.best_stock, "ABC")
        self.assertAlmostEqual(self.m.best_stock_pnl, 2000.0)

    def test_worst_stock(self):
        self.assertEqual(self.m.worst_stock, "XYZ")
        self.assertAlmostEqual(self.m.worst_stock_pnl, -500.0)

    def test_is_complete(self):
        self.assertTrue(self.m.is_complete)

    def test_to_dict_has_required_fields(self):
        d = self.m.to_dict()
        for key in ("period_key","year","month","is_complete","total_trades",
                    "winning_trades","losing_trades","win_rate_pct","gross_pnl",
                    "avg_winner","avg_loser","best_stock","worst_stock",
                    "avg_holding_days","pnl_delta","comparison_note"):
            self.assertIn(key, d)


# ── Month boundary ────────────────────────────────────────────────────────────

class TestMonthBoundary(unittest.TestCase):

    def test_last_day_of_month_in_correct_bucket(self):
        trades = [
            _t("A", "2026-06-30",  500),
            _t("B", "2026-07-01", -200),
        ]
        r = _engine().compute_monthly(trades, today=date(2026, 9, 1))
        self.assertEqual(r[0].period_key, "2026-06")
        self.assertEqual(r[0].total_trades, 1)
        self.assertEqual(r[1].period_key, "2026-07")
        self.assertEqual(r[1].total_trades, 1)

    def test_first_day_of_month_in_correct_bucket(self):
        trades = [
            _t("A", "2026-08-01", 300),
            _t("B", "2026-07-31", 400),
        ]
        r = _engine().compute_monthly(trades, today=date(2026, 9, 1))
        self.assertEqual(r[0].period_key, "2026-07")
        self.assertEqual(r[1].period_key, "2026-08")

    def test_year_boundary_dec_jan(self):
        trades = [
            _t("A", "2025-12-31", 1000),
            _t("B", "2026-01-01",  500),
        ]
        r = _engine().compute_monthly(trades, today=date(2026, 9, 1))
        self.assertEqual(r[0].period_key, "2025-12")
        self.assertEqual(r[1].period_key, "2026-01")


# ── Incomplete current month ──────────────────────────────────────────────────

class TestIncompleteCurrentMonth(unittest.TestCase):

    def test_current_month_labeled_incomplete(self):
        today  = date(2026, 9, 16)   # mid-September
        trades = [
            _t("A", "2026-09-05", 1000),
            _t("B", "2026-08-20",  500),
        ]
        r = _engine().compute_monthly(trades, today=today)
        aug = next(p for p in r if p.period_key == "2026-08")
        sep = next(p for p in r if p.period_key == "2026-09")
        self.assertTrue(aug.is_complete)
        self.assertFalse(sep.is_complete)

    def test_incomplete_month_comparison_note_labeled(self):
        today  = date(2026, 9, 16)
        trades = [
            _t("A", "2026-08-10", 500),
            _t("B", "2026-09-05", 800),
        ]
        r = _engine().compute_monthly(trades, today=today)
        sep = r[1]
        # Sep is current (incomplete) — comparison note should reference Aug
        self.assertIsNotNone(sep.comparison_note)
        self.assertIn("Aug", sep.comparison_note)

    def test_incomplete_does_not_contaminate_complete(self):
        """A partial current month should not affect previous months' is_complete."""
        today  = date(2026, 9, 16)
        trades = [
            _t("A", "2026-06-10", 1000),
            _t("B", "2026-07-15",  500),
            _t("C", "2026-09-05",  200),
        ]
        r = _engine().compute_monthly(trades, today=today)
        jun = next(p for p in r if p.period_key == "2026-06")
        jul = next(p for p in r if p.period_key == "2026-07")
        self.assertTrue(jun.is_complete)
        self.assertTrue(jul.is_complete)


# ── Month-over-month comparisons ──────────────────────────────────────────────

class TestMoMComparisons(unittest.TestCase):

    def setUp(self):
        trades = [
            _t("A", "2026-06-10", 1000),
            _t("B", "2026-07-10", 1500),
            _t("C", "2026-08-10",  800),
        ]
        self.r = _engine().compute_monthly(trades, today=date(2026, 9, 1))

    def test_first_period_no_delta(self):
        self.assertIsNone(self.r[0].pnl_delta)
        self.assertIsNone(self.r[0].comparison_note)

    def test_second_period_delta(self):
        jul = self.r[1]
        self.assertAlmostEqual(jul.pnl_delta, 500.0)   # 1500 - 1000
        self.assertIsNotNone(jul.comparison_note)
        self.assertIn("Jun", jul.comparison_note)

    def test_pnl_delta_pct(self):
        jul = self.r[1]
        self.assertAlmostEqual(jul.pnl_delta_pct, 50.0)  # +50% vs 1000

    def test_negative_delta(self):
        aug = self.r[2]
        self.assertAlmostEqual(aug.pnl_delta, -700.0)  # 800 - 1500

    def test_trades_delta(self):
        # Each month has exactly 1 trade
        self.assertEqual(self.r[1].trades_delta, 0)


# ── Yearly bucketing ──────────────────────────────────────────────────────────

class TestYearlyBucketing(unittest.TestCase):

    def test_two_years(self):
        trades = [
            _t("A", "2025-06-10", 1000),
            _t("B", "2026-03-15",  500),
        ]
        r = _engine().compute_yearly(trades, today=date(2026, 9, 1))
        self.assertEqual(len(r), 2)
        self.assertEqual(r[0].period_key, "2025")
        self.assertEqual(r[1].period_key, "2026")

    def test_current_year_labeled_incomplete(self):
        trades = [
            _t("A", "2025-06-10", 1000),
            _t("B", "2026-03-15",  500),
        ]
        r = _engine().compute_yearly(trades, today=date(2026, 9, 1))
        self.assertTrue(r[0].is_complete)    # 2025
        self.assertFalse(r[1].is_complete)   # 2026

    def test_past_year_labeled_complete(self):
        trades = [_t("A", "2024-06-10", 1000)]
        r = _engine().compute_yearly(trades, today=date(2026, 9, 1))
        self.assertTrue(r[0].is_complete)


# ── Yearly metrics ────────────────────────────────────────────────────────────

class TestYearlyMetrics(unittest.TestCase):

    def setUp(self):
        trades = [
            _t("ABC", "2026-01-10",  3000, holding_days=20),
            _t("XYZ", "2026-03-15", -1000, holding_days=5),
            _t("DEF", "2026-06-20",  2000, holding_days=15),
            _t("GHI", "2026-09-05", -500,  holding_days=3),
        ]
        self.r = _engine().compute_yearly(trades, today=date(2026, 9, 16))[0]

    def test_total_pnl(self):
        self.assertAlmostEqual(self.r.gross_pnl, 3500.0)

    def test_win_rate(self):
        self.assertAlmostEqual(self.r.win_rate_pct, 50.0)

    def test_best_stock(self):
        self.assertEqual(self.r.best_stock, "ABC")

    def test_worst_stock(self):
        # XYZ=-1000, GHI=-500 → worst is XYZ
        self.assertEqual(self.r.worst_stock, "XYZ")

    def test_avg_holding_days(self):
        self.assertAlmostEqual(self.r.avg_holding_days, (20+5+15+3)/4)

    def test_best_month(self):
        # Jan: +3000, Mar: -1000, Jun: +2000, Sep: -500
        self.assertEqual(self.r.best_month, "2026-01")
        self.assertAlmostEqual(self.r.best_month_pnl, 3000.0)

    def test_worst_month(self):
        self.assertEqual(self.r.worst_month, "2026-03")
        self.assertAlmostEqual(self.r.worst_month_pnl, -1000.0)

    def test_to_dict_has_required_fields(self):
        d = self.r.to_dict()
        for key in ("period_key","year","is_complete","total_trades",
                    "win_rate_pct","gross_pnl","best_month","worst_month",
                    "best_stock","worst_stock","pnl_delta","comparison_note"):
            self.assertIn(key, d)


# ── Year boundary ─────────────────────────────────────────────────────────────

class TestYearBoundary(unittest.TestCase):

    def test_dec_31_in_2025(self):
        trades = [
            _t("A", "2025-12-31", 500),
            _t("B", "2026-01-01", 300),
        ]
        r = _engine().compute_yearly(trades, today=date(2026, 9, 1))
        self.assertEqual(r[0].period_key, "2025")
        self.assertEqual(r[1].period_key, "2026")
        self.assertAlmostEqual(r[0].gross_pnl, 500.0)
        self.assertAlmostEqual(r[1].gross_pnl, 300.0)


# ── Year-over-year comparisons ────────────────────────────────────────────────

class TestYoYComparisons(unittest.TestCase):

    def setUp(self):
        trades = [
            _t("A", "2024-06-10", 10000),
            _t("B", "2025-06-10", 12000),
            _t("C", "2026-03-15",  8000),
        ]
        self.r = _engine().compute_yearly(trades, today=date(2026, 9, 1))

    def test_first_year_no_delta(self):
        self.assertIsNone(self.r[0].pnl_delta)

    def test_second_year_delta(self):
        self.assertAlmostEqual(self.r[1].pnl_delta, 2000.0)
        self.assertAlmostEqual(self.r[1].pnl_delta_pct, 20.0)

    def test_third_year_negative_delta(self):
        self.assertAlmostEqual(self.r[2].pnl_delta, -4000.0)

    def test_comparison_note_references_prior_year(self):
        self.assertIn("2025", self.r[2].comparison_note)


# ── Timezone / naive date safety ──────────────────────────────────────────────

class TestNaiveDates(unittest.TestCase):

    def test_naive_date_bucketed_correctly(self):
        """Naive date.fromisoformat() should bucket without TZ issues."""
        # IST is UTC+5:30; a trade sold at 15:30 IST on Dec 31 is still
        # Dec 31 in the local (IST) calendar — no shifting should occur.
        trades = [_t("A", "2025-12-31", 1000)]
        r = _engine().compute_monthly(trades, today=date(2026, 9, 1))
        self.assertEqual(r[0].period_key, "2025-12")

    def test_multiple_same_day_different_symbols(self):
        """Multiple trades same sell_date go into the same monthly bucket."""
        trades = [
            _t("A", "2026-06-15", 500),
            _t("B", "2026-06-15", -200),
            _t("C", "2026-06-15", 800),
        ]
        r = _engine().compute_monthly(trades, today=date(2026, 9, 1))
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0].total_trades, 3)
        self.assertAlmostEqual(r[0].gross_pnl, 1100.0)


# ── Same symbol multiple trades in a month ────────────────────────────────────

class TestSameSymbolMultipleTrades(unittest.TestCase):

    def test_best_stock_aggregates_by_symbol(self):
        """Best/worst stock is by total P&L per symbol, not per trade."""
        trades = [
            _t("RELIANCE", "2026-06-05",  1000),
            _t("RELIANCE", "2026-06-20",  2000),   # total RELIANCE = +3000
            _t("TCS",      "2026-06-10", -500),
            _t("TCS",      "2026-06-25",  -200),   # total TCS = -700
        ]
        r = _engine().compute_monthly(trades, today=date(2026, 9, 1))
        m = r[0]
        self.assertEqual(m.best_stock,  "RELIANCE")
        self.assertAlmostEqual(m.best_stock_pnl, 3000.0)
        self.assertEqual(m.worst_stock, "TCS")
        self.assertAlmostEqual(m.worst_stock_pnl, -700.0)


if __name__ == "__main__":
    unittest.main()