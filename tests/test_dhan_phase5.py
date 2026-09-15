"""
tests/test_dhan_phase5.py
──────────────────────────
Phase 5 — Deterministic FIFO matching engine tests.

All tests use the pure-Python FifoEngine only (no DB, no Flask needed).
The engine is DB-free by design so these tests run fast and in isolation.

Run: python -m pytest tests/test_dhan_phase5.py -v
"""

import unittest
from datetime import date, datetime
from types import SimpleNamespace

from app.services.fifo_engine import FifoEngine, MatchedTrade, OpenLot, MatchingResult


# ── Fixture ───────────────────────────────────────────────────────────────────

_ID = 0

def _exe(side, qty, price, symbol="ABC", exchange="NSE",
         instrument_type="EQUITY",
         trade_date_str=None, traded_at_str=None,
         product_type="CNC", trade_id=None):
    """Build a minimal trade execution namespace."""
    global _ID
    _ID += 1
    td = date.fromisoformat(trade_date_str) if trade_date_str else None
    ta = datetime.fromisoformat(traded_at_str) if traded_at_str else (
        datetime(td.year, td.month, td.day, 10, 0, 0) if td else None
    )
    return SimpleNamespace(
        id                = _ID,
        provider_trade_id = trade_id or f"T{_ID:06d}",
        symbol            = symbol,
        exchange          = exchange,
        instrument_type   = instrument_type,
        side              = side,
        quantity          = qty,
        price             = float(price),
        trade_date        = td,
        traded_at         = ta,
        product_type      = product_type,
    )

def _engine():
    return FifoEngine()


# ── 1. One BUY / One SELL — complete match ────────────────────────────────────

class TestOneBuyOneSell(unittest.TestCase):

    def test_complete_match(self):
        exes = [
            _exe("BUY",  100, 100, trade_date_str="2026-06-10"),
            _exe("SELL", 100, 130, trade_date_str="2026-06-20"),
        ]
        r = _engine().match(exes)
        self.assertEqual(len(r.matched),   1)
        self.assertEqual(len(r.open_lots), 0)
        m = r.matched[0]
        self.assertEqual(m.quantity,   100)
        self.assertEqual(m.buy_price,  100.0)
        self.assertEqual(m.sell_price, 130.0)
        self.assertAlmostEqual(m.gross_pnl, 3000.0)
        self.assertEqual(m.holding_days, 10)

    def test_pnl_loss(self):
        exes = [
            _exe("BUY",  50, 200, trade_date_str="2026-01-01"),
            _exe("SELL", 50, 180, trade_date_str="2026-01-15"),
        ]
        r = _engine().match(exes)
        self.assertEqual(len(r.matched), 1)
        self.assertAlmostEqual(r.matched[0].gross_pnl, -1000.0)

    def test_holding_days_correct(self):
        exes = [
            _exe("BUY",  10, 100, trade_date_str="2026-06-10"),
            _exe("SELL", 10, 130, trade_date_str="2026-06-20"),
        ]
        r = _engine().match(exes)
        self.assertEqual(r.matched[0].holding_days, 10)


# ── 2. Multiple BUYs / One SELL — spec example ───────────────────────────────

class TestMultipleBuysOneSell(unittest.TestCase):

    def test_spec_example(self):
        """Exact example from the Phase 5 specification."""
        exes = [
            _exe("BUY",  100, 100, trade_date_str="2026-06-10"),
            _exe("BUY",  100, 110, trade_date_str="2026-06-15"),
            _exe("SELL", 150, 130, trade_date_str="2026-06-20"),
        ]
        r = _engine().match(exes)

        self.assertEqual(len(r.matched),   2)
        self.assertEqual(len(r.open_lots), 1)

        # Matched lot 1: 100 shares from 10 Jun buy
        m1 = r.matched[0]
        self.assertEqual(m1.quantity,      100)
        self.assertEqual(m1.buy_price,     100.0)
        self.assertEqual(m1.sell_price,    130.0)
        self.assertAlmostEqual(m1.gross_pnl, 3000.0)
        self.assertEqual(m1.holding_days,  10)
        self.assertEqual(m1.buy_date,  date(2026, 6, 10))
        self.assertEqual(m1.sell_date, date(2026, 6, 20))

        # Matched lot 2: 50 shares from 15 Jun buy
        m2 = r.matched[1]
        self.assertEqual(m2.quantity,      50)
        self.assertEqual(m2.buy_price,     110.0)
        self.assertEqual(m2.sell_price,    130.0)
        self.assertAlmostEqual(m2.gross_pnl, 1000.0)
        self.assertEqual(m2.holding_days,  5)
        self.assertEqual(m2.buy_date, date(2026, 6, 15))

        # Open lot: 50 shares from 15 Jun buy
        o = r.open_lots[0]
        self.assertEqual(o.quantity,   50)
        self.assertEqual(o.buy_price,  110.0)
        self.assertEqual(o.buy_date,   date(2026, 6, 15))
        self.assertAlmostEqual(o.cost_basis, 5500.0)

    def test_total_pnl(self):
        exes = [
            _exe("BUY",  100, 100, trade_date_str="2026-06-10"),
            _exe("BUY",  100, 110, trade_date_str="2026-06-15"),
            _exe("SELL", 150, 130, trade_date_str="2026-06-20"),
        ]
        r = _engine().match(exes)
        self.assertAlmostEqual(r.total_gross_pnl, 4000.0)


# ── 3. One BUY / Multiple SELLs ──────────────────────────────────────────────

class TestOneBuyMultipleSells(unittest.TestCase):

    def test_two_sells_from_one_buy(self):
        exes = [
            _exe("BUY",  100, 100, trade_date_str="2026-01-01"),
            _exe("SELL",  60, 120, trade_date_str="2026-01-10"),
            _exe("SELL",  40, 140, trade_date_str="2026-01-20"),
        ]
        r = _engine().match(exes)
        self.assertEqual(len(r.matched),   2)
        self.assertEqual(len(r.open_lots), 0)
        self.assertEqual(r.matched[0].quantity, 60)
        self.assertEqual(r.matched[1].quantity, 40)
        self.assertAlmostEqual(r.total_gross_pnl, 60*20 + 40*40)

    def test_three_sells(self):
        exes = [
            _exe("BUY",  90, 100, trade_date_str="2026-02-01"),
            _exe("SELL", 30, 110, trade_date_str="2026-02-05"),
            _exe("SELL", 30, 120, trade_date_str="2026-02-10"),
            _exe("SELL", 30, 130, trade_date_str="2026-02-15"),
        ]
        r = _engine().match(exes)
        self.assertEqual(len(r.matched),   3)
        self.assertEqual(len(r.open_lots), 0)
        self.assertAlmostEqual(r.total_gross_pnl, 30*10 + 30*20 + 30*30)


# ── 4. Partial sell ───────────────────────────────────────────────────────────

class TestPartialSell(unittest.TestCase):

    def test_sell_less_than_buy(self):
        exes = [
            _exe("BUY",  200, 50, trade_date_str="2026-03-01"),
            _exe("SELL",  75, 60, trade_date_str="2026-03-10"),
        ]
        r = _engine().match(exes)
        self.assertEqual(len(r.matched),   1)
        self.assertEqual(len(r.open_lots), 1)
        self.assertEqual(r.matched[0].quantity,   75)
        self.assertEqual(r.open_lots[0].quantity, 125)
        self.assertAlmostEqual(r.open_lots[0].cost_basis, 125 * 50)

    def test_open_lot_buy_price_unchanged(self):
        """Open lot must retain original buy price, not an average."""
        exes = [
            _exe("BUY",  100, 80, trade_date_str="2026-04-01"),
            _exe("SELL",  30, 90, trade_date_str="2026-04-10"),
        ]
        r = _engine().match(exes)
        self.assertEqual(r.open_lots[0].buy_price, 80.0)


# ── 5. Multiple BUYs / Multiple SELLs ────────────────────────────────────────

class TestMultipleBuysMultipleSells(unittest.TestCase):

    def test_four_buys_three_sells(self):
        exes = [
            _exe("BUY",  50, 100, trade_date_str="2026-01-01"),
            _exe("BUY",  50, 110, trade_date_str="2026-01-05"),
            _exe("BUY",  50, 120, trade_date_str="2026-01-10"),
            _exe("BUY",  50, 130, trade_date_str="2026-01-15"),
            _exe("SELL", 80, 150, trade_date_str="2026-02-01"),
            _exe("SELL", 70, 160, trade_date_str="2026-02-10"),
            _exe("SELL", 30, 170, trade_date_str="2026-02-20"),
        ]
        r = _engine().match(exes)
        total_bought = 200
        total_sold   = 180
        total_open   = total_bought - total_sold
        self.assertEqual(r.total_matched_qty, total_sold)
        self.assertEqual(r.total_open_qty,    total_open)
        self.assertEqual(len(r.open_lots),    1)

    def test_fifo_order_respected(self):
        """FIFO: oldest buy consumed first."""
        exes = [
            _exe("BUY",  10, 50,  trade_date_str="2026-01-01", trade_id="BUY1"),
            _exe("BUY",  10, 200, trade_date_str="2026-01-10", trade_id="BUY2"),
            _exe("SELL", 10, 100, trade_date_str="2026-02-01", trade_id="SELL1"),
        ]
        r = _engine().match(exes)
        # The SELL should consume the first BUY (price=50), not the second (price=200)
        self.assertEqual(r.matched[0].buy_price,  50.0)
        self.assertEqual(r.matched[0].buy_trade_id, "BUY1")
        self.assertEqual(r.open_lots[0].buy_price, 200.0)


# ── 6. Complete close ─────────────────────────────────────────────────────────

class TestCompleteClose(unittest.TestCase):

    def test_exact_close(self):
        exes = [
            _exe("BUY",  100, 100, trade_date_str="2026-05-01"),
            _exe("BUY",  200, 110, trade_date_str="2026-05-05"),
            _exe("SELL", 300, 120, trade_date_str="2026-05-20"),
        ]
        r = _engine().match(exes)
        self.assertEqual(len(r.open_lots), 0)
        self.assertEqual(r.total_matched_qty, 300)
        self.assertEqual(r.total_open_qty,    0)


# ── 7. Remaining open position ────────────────────────────────────────────────

class TestRemainingOpen(unittest.TestCase):

    def test_buy_only_is_all_open(self):
        exes = [
            _exe("BUY", 100, 50, trade_date_str="2026-06-01"),
            _exe("BUY",  50, 60, trade_date_str="2026-06-05"),
        ]
        r = _engine().match(exes)
        self.assertEqual(len(r.matched),   0)
        self.assertEqual(len(r.open_lots), 2)
        self.assertEqual(r.total_open_qty, 150)

    def test_partial_close_leaves_open(self):
        exes = [
            _exe("BUY",  300, 100, trade_date_str="2026-07-01"),
            _exe("SELL", 100, 120, trade_date_str="2026-07-15"),
        ]
        r = _engine().match(exes)
        self.assertEqual(r.total_open_qty,    200)
        self.assertEqual(r.open_lots[0].buy_price, 100.0)


# ── 8. Same-day trades ────────────────────────────────────────────────────────

class TestSameDayTrades(unittest.TestCase):

    def test_intraday_buy_then_sell(self):
        exes = [
            _exe("BUY",  100, 100,
                 trade_date_str="2026-08-01",
                 traded_at_str="2026-08-01T09:30:00"),
            _exe("SELL", 100, 105,
                 trade_date_str="2026-08-01",
                 traded_at_str="2026-08-01T14:00:00"),
        ]
        r = _engine().match(exes)
        self.assertEqual(len(r.matched),   1)
        self.assertEqual(len(r.open_lots), 0)
        self.assertEqual(r.matched[0].holding_days, 0)

    def test_same_day_multiple_buys_and_sells(self):
        exes = [
            _exe("BUY",  50, 100, trade_date_str="2026-08-02",
                 traded_at_str="2026-08-02T09:30:00"),
            _exe("BUY",  50, 102, trade_date_str="2026-08-02",
                 traded_at_str="2026-08-02T10:00:00"),
            _exe("SELL", 80, 110, trade_date_str="2026-08-02",
                 traded_at_str="2026-08-02T15:00:00"),
        ]
        r = _engine().match(exes)
        self.assertEqual(r.total_matched_qty, 80)
        self.assertEqual(r.total_open_qty,    20)
        # FIFO: first 50 from BUY@100, then 30 from BUY@102
        self.assertEqual(r.matched[0].buy_price, 100.0)
        self.assertEqual(r.matched[0].quantity,   50)
        self.assertEqual(r.matched[1].buy_price, 102.0)
        self.assertEqual(r.matched[1].quantity,   30)


# ── 9. Out-of-order API records ───────────────────────────────────────────────

class TestOutOfOrder(unittest.TestCase):

    def test_api_returns_sell_before_buy(self):
        """Engine must sort chronologically regardless of list order."""
        exes = [
            _exe("SELL", 100, 130, trade_date_str="2026-06-20", trade_id="SELL1"),
            _exe("BUY",  100, 100, trade_date_str="2026-06-10", trade_id="BUY1"),
        ]
        r = _engine().match(exes)
        # After sorting: BUY on 10 Jun comes first, SELL on 20 Jun second
        self.assertEqual(len(r.matched),   1)
        self.assertEqual(len(r.open_lots), 0)
        self.assertEqual(r.matched[0].buy_price,  100.0)
        self.assertEqual(r.matched[0].sell_price, 130.0)

    def test_three_buys_returned_out_of_order(self):
        exes = [
            _exe("BUY",  50, 120, trade_date_str="2026-01-15", trade_id="BUY3"),
            _exe("BUY",  50, 100, trade_date_str="2026-01-01", trade_id="BUY1"),
            _exe("BUY",  50, 110, trade_date_str="2026-01-10", trade_id="BUY2"),
            _exe("SELL", 60, 150, trade_date_str="2026-02-01", trade_id="SELL1"),
        ]
        r = _engine().match(exes)
        # FIFO order: BUY1(100), BUY2(110), BUY3(120)
        # SELL consumes 50 from BUY1 and 10 from BUY2
        self.assertEqual(r.matched[0].buy_price, 100.0)
        self.assertEqual(r.matched[0].quantity,   50)
        self.assertEqual(r.matched[1].buy_price, 110.0)
        self.assertEqual(r.matched[1].quantity,   10)
        # Open: 40 from BUY2, 50 from BUY3
        self.assertEqual(r.total_open_qty, 90)


# ── 10. Duplicate records ─────────────────────────────────────────────────────

class TestDuplicateRecords(unittest.TestCase):

    def test_duplicate_buy_skipped(self):
        exes = [
            _exe("BUY",  100, 100, trade_date_str="2026-06-10", trade_id="DUP1"),
            _exe("BUY",  100, 100, trade_date_str="2026-06-10", trade_id="DUP1"),  # dup
            _exe("SELL", 100, 130, trade_date_str="2026-06-20"),
        ]
        r = _engine().match(exes)
        # Only one BUY counts — one SELL matches it completely
        self.assertEqual(len(r.matched),   1)
        self.assertEqual(len(r.open_lots), 0)
        self.assertEqual(r.matched[0].quantity, 100)

    def test_duplicate_sell_skipped(self):
        exes = [
            _exe("BUY",  200, 100, trade_date_str="2026-06-10"),
            _exe("SELL", 100, 130, trade_date_str="2026-06-20", trade_id="DSELL1"),
            _exe("SELL", 100, 130, trade_date_str="2026-06-20", trade_id="DSELL1"),  # dup
        ]
        r = _engine().match(exes)
        # Only one SELL counts
        self.assertEqual(r.total_matched_qty, 100)
        self.assertEqual(r.total_open_qty,    100)


# ── 11. Multiple symbols isolated ────────────────────────────────────────────

class TestMultipleSymbols(unittest.TestCase):

    def test_two_symbols_independent(self):
        exes = [
            _exe("BUY",  100, 100, symbol="ABC", trade_date_str="2026-01-01"),
            _exe("BUY",  200, 50,  symbol="XYZ", trade_date_str="2026-01-01"),
            _exe("SELL", 100, 120, symbol="ABC", trade_date_str="2026-02-01"),
            _exe("SELL", 100, 60,  symbol="XYZ", trade_date_str="2026-02-01"),
        ]
        r = _engine().match(exes)
        abc_matched = [m for m in r.matched   if m.symbol == "ABC"]
        xyz_matched = [m for m in r.matched   if m.symbol == "XYZ"]
        xyz_open    = [o for o in r.open_lots if o.symbol == "XYZ"]

        self.assertEqual(len(abc_matched), 1)
        self.assertAlmostEqual(abc_matched[0].gross_pnl, 2000.0)
        self.assertEqual(len(xyz_matched), 1)
        self.assertAlmostEqual(xyz_matched[0].gross_pnl, 1000.0)
        self.assertEqual(xyz_open[0].quantity, 100)

    def test_sell_does_not_cross_symbols(self):
        exes = [
            _exe("BUY",  100, 100, symbol="RELIANCE", trade_date_str="2026-01-01"),
            _exe("SELL", 100, 120, symbol="TCS",      trade_date_str="2026-02-01"),
        ]
        r = _engine().match(exes)
        self.assertEqual(len(r.matched),         0)
        self.assertEqual(len(r.open_lots),        1)
        self.assertEqual(len(r.unmatched_sells),  1)


# ── 12. Edge cases ────────────────────────────────────────────────────────────

class TestEdgeCases(unittest.TestCase):

    def test_empty_input(self):
        r = _engine().match([])
        self.assertEqual(r.matched,   [])
        self.assertEqual(r.open_lots, [])

    def test_buy_only(self):
        exes = [_exe("BUY", 50, 100, trade_date_str="2026-01-01")]
        r = _engine().match(exes)
        self.assertEqual(len(r.matched),   0)
        self.assertEqual(len(r.open_lots), 1)

    def test_sell_with_no_buy_is_orphan(self):
        exes = [_exe("SELL", 100, 130, trade_date_str="2026-06-20")]
        r = _engine().match(exes)
        self.assertEqual(len(r.matched),         0)
        self.assertEqual(len(r.unmatched_sells),  1)

    def test_summary_dict(self):
        exes = [
            _exe("BUY",  100, 100, trade_date_str="2026-01-01"),
            _exe("SELL",  60, 120, trade_date_str="2026-02-01"),
        ]
        r  = _engine().match(exes)
        s  = r.summary()
        self.assertIn("matched_lots",      s)
        self.assertIn("open_lots",         s)
        self.assertIn("total_gross_pnl",   s)
        self.assertIn("total_matched_qty", s)
        self.assertIn("total_open_qty",    s)

    def test_to_dict_matched(self):
        exes = [
            _exe("BUY",  10, 100, trade_date_str="2026-01-01"),
            _exe("SELL", 10, 120, trade_date_str="2026-02-01"),
        ]
        r = _engine().match(exes)
        d = r.matched[0].to_dict()
        for key in ("symbol","quantity","buy_price","sell_price",
                    "gross_pnl","holding_days","buy_trade_id","sell_trade_id"):
            self.assertIn(key, d)

    def test_to_dict_open_lot(self):
        exes = [_exe("BUY", 10, 100, trade_date_str="2026-01-01")]
        r = _engine().match(exes)
        d = r.open_lots[0].to_dict()
        for key in ("symbol","quantity","buy_price","cost_basis","buy_date"):
            self.assertIn(key, d)

    def test_zero_qty_buy_skipped(self):
        exes = [
            _exe("BUY",  0, 100, trade_date_str="2026-01-01"),
            _exe("BUY", 10, 100, trade_date_str="2026-01-02"),
        ]
        r = _engine().match(exes)
        self.assertEqual(r.total_open_qty, 10)


if __name__ == "__main__":
    unittest.main()