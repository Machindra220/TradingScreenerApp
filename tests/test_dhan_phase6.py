"""
tests/test_dhan_phase6.py
──────────────────────────
Phase 6 — Trade analytics tests.

All tests use SimpleNamespace fixtures — no DB, no Flask required.
The analytics engine is pure Python so these run instantly.

Run: python -m pytest tests/test_dhan_phase6.py -v
"""

import unittest
import statistics
from datetime import date, datetime
from types import SimpleNamespace

from app.services.trade_analytics import (
    TradeAnalyticsEngine,
    CompletedTradeSummary,
    AnalyticsReport,
)


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _mt(
    symbol="ABC", qty=100, buy_price=100.0, sell_price=130.0,
    buy_date_str="2026-01-01", sell_date_str="2026-01-11",
    holding_days=10, exchange="NSE", instrument_type="EQUITY",
    product_type="CNC", buy_tid="B001", sell_tid="S001",
    isin=None,
):
    """Build a minimal MatchedTrade-like namespace."""
    gross_pnl = round((sell_price - buy_price) * qty, 2)
    return SimpleNamespace(
        symbol          = symbol,
        exchange        = exchange,
        instrument_type = instrument_type,
        quantity        = qty,
        buy_price       = buy_price,
        sell_price      = sell_price,
        buy_value       = round(qty * buy_price, 2),
        sell_value      = round(qty * sell_price, 2),
        gross_pnl       = gross_pnl,
        holding_days    = holding_days,
        buy_date        = date.fromisoformat(buy_date_str)  if buy_date_str  else None,
        sell_date       = date.fromisoformat(sell_date_str) if sell_date_str else None,
        buy_traded_at   = None,
        sell_traded_at  = None,
        product_type    = product_type,
        buy_trade_id    = buy_tid,
        sell_trade_id   = sell_tid,
        isin            = isin,
        security_id     = None,
    )

def _engine():
    return TradeAnalyticsEngine()


# ── Per-trade summary ─────────────────────────────────────────────────────────

class TestCompletedTradeSummary(unittest.TestCase):

    def setUp(self):
        self.mt     = _mt(qty=100, buy_price=100.0, sell_price=130.0,
                          holding_days=10)
        self.engine = _engine()
        self.s      = self.engine._summarise_trade(self.mt)

    def test_symbol_carried_through(self):
        self.assertEqual(self.s.symbol, "ABC")

    def test_buy_value_computed(self):
        self.assertAlmostEqual(self.s.buy_value,  10000.0)

    def test_sell_value_computed(self):
        self.assertAlmostEqual(self.s.sell_value, 13000.0)

    def test_gross_pnl(self):
        self.assertAlmostEqual(self.s.gross_pnl, 3000.0)

    def test_return_pct(self):
        # 3000 / 10000 × 100 = 30%
        self.assertAlmostEqual(self.s.return_pct, 30.0)

    def test_holding_period_calendar_days(self):
        self.assertEqual(self.s.holding_period_calendar_days, 10)

    def test_holding_period_trading_days_not_provided(self):
        d = self.s.to_dict()
        self.assertIn("holding_period_trading_days", d)
        self.assertIsNone(d["holding_period_trading_days"])

    def test_outcome_win(self):
        self.assertEqual(self.s.outcome, "WIN")

    def test_outcome_loss(self):
        s = self.engine._summarise_trade(
            _mt(buy_price=130.0, sell_price=100.0))
        self.assertEqual(s.outcome, "LOSS")
        self.assertLess(s.gross_pnl, 0)

    def test_outcome_breakeven(self):
        s = self.engine._summarise_trade(
            _mt(buy_price=100.0, sell_price=100.0))
        self.assertEqual(s.outcome, "BREAKEVEN")
        self.assertEqual(s.gross_pnl, 0.0)

    def test_return_pct_none_when_buy_value_zero(self):
        s = self.engine._summarise_trade(_mt(qty=0, buy_price=100.0))
        self.assertIsNone(s.return_pct)

    def test_to_dict_has_all_required_fields(self):
        d = self.s.to_dict()
        for key in (
            "symbol", "exchange", "instrument_type",
            "quantity", "buy_price", "sell_price",
            "buy_value", "sell_value", "gross_pnl",
            "return_pct", "holding_period_calendar_days",
            "holding_period_trading_days",
            "outcome", "buy_date", "sell_date",
            "buy_trade_id", "sell_trade_id",
        ):
            self.assertIn(key, d, f"Missing field: {key}")

    def test_dates_in_iso_format(self):
        d = self.s.to_dict()
        self.assertEqual(d["buy_date"],  "2026-01-01")
        self.assertEqual(d["sell_date"], "2026-01-11")

    def test_isin_optional(self):
        s = self.engine._summarise_trade(_mt(isin="INE0001019"))
        self.assertEqual(s.isin, "INE0001019")

    def test_negative_return_pct(self):
        s = self.engine._summarise_trade(
            _mt(qty=100, buy_price=200.0, sell_price=150.0))
        self.assertAlmostEqual(s.return_pct, -25.0)


# ── Empty input ───────────────────────────────────────────────────────────────

class TestEmptyInput(unittest.TestCase):

    def test_empty_returns_default_report(self):
        r = _engine().compute([])
        self.assertEqual(r.total_trades,   0)
        self.assertIsNone(r.win_rate_pct)
        self.assertIsNone(r.avg_winner)
        self.assertIsNone(r.avg_loser)
        self.assertEqual(r.total_gross_pnl, 0.0)
        self.assertEqual(r.trades, [])


# ── Win/loss/breakeven counts ─────────────────────────────────────────────────

class TestOutcomeCounts(unittest.TestCase):

    def setUp(self):
        trades = [
            _mt(buy_price=100, sell_price=130, buy_tid="B1", sell_tid="S1"),  # WIN
            _mt(buy_price=100, sell_price=130, buy_tid="B2", sell_tid="S2"),  # WIN
            _mt(buy_price=130, sell_price=100, buy_tid="B3", sell_tid="S3"),  # LOSS
            _mt(buy_price=100, sell_price=100, buy_tid="B4", sell_tid="S4"),  # BE
        ]
        self.report = _engine().compute(trades)

    def test_total_trades(self):
        self.assertEqual(self.report.total_trades, 4)

    def test_winning_trades(self):
        self.assertEqual(self.report.winning_trades, 2)

    def test_losing_trades(self):
        self.assertEqual(self.report.losing_trades, 1)

    def test_breakeven_trades(self):
        self.assertEqual(self.report.breakeven_trades, 1)

    def test_win_rate(self):
        self.assertAlmostEqual(self.report.win_rate_pct, 50.0)


# ── P&L aggregates ────────────────────────────────────────────────────────────

class TestPnLAggregates(unittest.TestCase):

    def setUp(self):
        self.trades = [
            _mt(qty=100, buy_price=100, sell_price=130, buy_tid="B1", sell_tid="S1"),
            _mt(qty=50,  buy_price=200, sell_price=160, buy_tid="B2", sell_tid="S2"),
            _mt(qty=200, buy_price=50,  sell_price=60,  buy_tid="B3", sell_tid="S3"),
        ]
        self.report = _engine().compute(self.trades)

    def test_total_gross_pnl(self):
        # 100×30 + 50×(-40) + 200×10 = 3000 - 2000 + 2000 = 3000
        self.assertAlmostEqual(self.report.total_gross_pnl, 3000.0)

    def test_total_buy_value(self):
        # 100×100 + 50×200 + 200×50 = 10000+10000+10000 = 30000
        self.assertAlmostEqual(self.report.total_buy_value, 30000.0)

    def test_total_sell_value(self):
        # 100×130 + 50×160 + 200×60 = 13000+8000+12000 = 33000
        self.assertAlmostEqual(self.report.total_sell_value, 33000.0)

    def test_largest_winner(self):
        # B1: +3000, B3: +2000 → largest=3000
        self.assertAlmostEqual(self.report.largest_winner, 3000.0)

    def test_largest_loser(self):
        # B2: -2000
        self.assertAlmostEqual(self.report.largest_loser, -2000.0)

    def test_avg_winner(self):
        # Winners: B1=3000, B3=2000 → avg=2500
        self.assertAlmostEqual(self.report.avg_winner, 2500.0)

    def test_avg_loser(self):
        # Losers: B2=-2000 → avg=-2000
        self.assertAlmostEqual(self.report.avg_loser, -2000.0)


# ── Winner / loser return % ───────────────────────────────────────────────────

class TestReturnPct(unittest.TestCase):

    def test_winner_return_pct(self):
        # 3000/10000×100 = 30%
        s = _engine()._summarise_trade(
            _mt(qty=100, buy_price=100, sell_price=130))
        self.assertAlmostEqual(s.return_pct, 30.0)

    def test_avg_winner_return_pct(self):
        trades = [
            _mt(qty=100, buy_price=100, sell_price=130, buy_tid="B1", sell_tid="S1"),  # +30%
            _mt(qty=100, buy_price=100, sell_price=120, buy_tid="B2", sell_tid="S2"),  # +20%
        ]
        r = _engine().compute(trades)
        self.assertAlmostEqual(r.avg_winner_return_pct, 25.0)

    def test_avg_loser_return_pct(self):
        trades = [
            _mt(qty=100, buy_price=100, sell_price=80, buy_tid="B1", sell_tid="S1"),  # -20%
            _mt(qty=100, buy_price=100, sell_price=90, buy_tid="B2", sell_tid="S2"),  # -10%
        ]
        r = _engine().compute(trades)
        self.assertAlmostEqual(r.avg_loser_return_pct, -15.0)


# ── Holding period stats ──────────────────────────────────────────────────────

class TestHoldingPeriod(unittest.TestCase):

    def setUp(self):
        trades = [
            _mt(holding_days=5,  buy_tid="B1", sell_tid="S1"),
            _mt(holding_days=10, buy_tid="B2", sell_tid="S2"),
            _mt(holding_days=15, buy_tid="B3", sell_tid="S3"),
            _mt(holding_days=20, buy_tid="B4", sell_tid="S4"),
        ]
        self.report = _engine().compute(trades)

    def test_avg_holding_days(self):
        self.assertAlmostEqual(self.report.avg_holding_calendar_days, 12.5)

    def test_median_holding_days(self):
        self.assertAlmostEqual(self.report.median_holding_calendar_days, 12.5)

    def test_min_holding_days(self):
        self.assertEqual(self.report.min_holding_calendar_days, 5)

    def test_max_holding_days(self):
        self.assertEqual(self.report.max_holding_calendar_days, 20)

    def test_zero_holding_days_intraday(self):
        trades = [_mt(holding_days=0, buy_tid="B1", sell_tid="S1")]
        r = _engine().compute(trades)
        self.assertEqual(r.min_holding_calendar_days, 0)

    def test_none_holding_days_excluded(self):
        trades = [
            _mt(holding_days=10, buy_tid="B1", sell_tid="S1"),
            _mt(holding_days=None, buy_tid="B2", sell_tid="S2"),
        ]
        r = _engine().compute(trades)
        self.assertAlmostEqual(r.avg_holding_calendar_days, 10.0)
        self.assertEqual(r.min_holding_calendar_days, 10)


# ── No winners / no losers edge cases ────────────────────────────────────────

class TestEdgeCases(unittest.TestCase):

    def test_all_winners_no_avg_loser(self):
        trades = [
            _mt(buy_price=100, sell_price=130, buy_tid="B1", sell_tid="S1"),
            _mt(buy_price=100, sell_price=120, buy_tid="B2", sell_tid="S2"),
        ]
        r = _engine().compute(trades)
        self.assertIsNone(r.avg_loser)
        self.assertIsNone(r.largest_loser)
        self.assertEqual(r.win_rate_pct, 100.0)

    def test_all_losers_no_avg_winner(self):
        trades = [
            _mt(buy_price=130, sell_price=100, buy_tid="B1", sell_tid="S1"),
        ]
        r = _engine().compute(trades)
        self.assertIsNone(r.avg_winner)
        self.assertIsNone(r.largest_winner)
        self.assertEqual(r.win_rate_pct, 0.0)

    def test_single_trade_win(self):
        r = _engine().compute([_mt()])
        self.assertEqual(r.total_trades,    1)
        self.assertEqual(r.winning_trades,  1)
        self.assertEqual(r.win_rate_pct,    100.0)
        self.assertAlmostEqual(r.total_gross_pnl, 3000.0)

    def test_report_trades_list_populated(self):
        r = _engine().compute([_mt()])
        self.assertEqual(len(r.trades), 1)
        self.assertIsInstance(r.trades[0], CompletedTradeSummary)

    def test_to_dict_without_trades(self):
        r = _engine().compute([_mt()])
        d = r.to_dict(include_trades=False)
        self.assertNotIn("trades", d)

    def test_to_dict_with_trades(self):
        r = _engine().compute([_mt()])
        d = r.to_dict(include_trades=True)
        self.assertIn("trades", d)
        self.assertEqual(len(d["trades"]), 1)

    def test_holding_period_note_in_dict(self):
        r = _engine().compute([_mt()])
        d = r.to_dict(include_trades=False)
        self.assertIn("holding_period_note", d)
        self.assertIn("calendar", d["holding_period_note"])

    def test_malformed_trade_skipped_or_degraded(self):
        """
        A trade with missing attributes either gets skipped (if an exception
        fires) or degrades gracefully to a zero-qty breakeven trade.
        Either way the good trade is still counted.
        """
        from types import SimpleNamespace
        bad  = SimpleNamespace()  # no attributes at all
        good = _mt()
        r    = _engine().compute([good, bad])
        # The good trade must appear — total is 1 or 2 depending on degradation
        self.assertGreaterEqual(r.total_trades, 1)
        # gross P&L from the good trade must be present
        self.assertAlmostEqual(r.total_gross_pnl, 3000.0, places=0)


# ── Multiple symbols ──────────────────────────────────────────────────────────

class TestMultipleSymbols(unittest.TestCase):

    def test_aggregates_across_symbols(self):
        trades = [
            _mt(symbol="RELIANCE", qty=10, buy_price=2000, sell_price=2500,
                buy_tid="B1", sell_tid="S1"),
            _mt(symbol="TCS",      qty=5,  buy_price=3000, sell_price=2800,
                buy_tid="B2", sell_tid="S2"),
        ]
        r = _engine().compute(trades)
        # RELIANCE: +5000, TCS: -1000 → total=+4000
        self.assertAlmostEqual(r.total_gross_pnl, 4000.0)
        self.assertEqual(r.winning_trades, 1)
        self.assertEqual(r.losing_trades,  1)


if __name__ == "__main__":
    unittest.main()