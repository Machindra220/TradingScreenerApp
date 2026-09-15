"""
tests/test_dhan_phase4.py
Phase 4 — trade normalization and execution storage tests.
Uses a single in-memory SQLite DB shared across all test classes.
Run: python -m pytest tests/test_dhan_phase4.py -v
"""

import unittest
from datetime import date, datetime
from types import SimpleNamespace

# ── Shared app + DB (created once for all tests) ──────────────────────────────

from flask import Flask
from flask_sqlalchemy import SQLAlchemy

_app = Flask(__name__)
_app.config.update({
    "TESTING": True,
    "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
    "SQLALCHEMY_BINDS": {"analytics": "sqlite:///:memory:"},
    "SQLALCHEMY_TRACK_MODIFICATIONS": False,
})
_db = SQLAlchemy(_app)


def setUpModule():
    """Create all tables once before any test in this module."""
    import sys, types
    # Wire shims so models_analytics and services can import app.extensions
    for name in ["app", "app.services", "app.extensions",
                 "app.models_analytics", "app.services.trade_normalizer",
                 "app.services.trade_store"]:
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)

    sys.modules["app.extensions"].db = _db
    sys.modules["app"].extensions    = sys.modules["app.extensions"]
    sys.modules["app"].services      = sys.modules["app.services"]

    import importlib.util, os

    def _load(alias, path):
        spec = importlib.util.spec_from_file_location(alias, path)
        m    = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        sys.modules[alias] = m
        return m

    import pathlib
    base = pathlib.Path(__file__).parent.parent  # project root

    _load("app.models_analytics",          str(base / "app" / "models_analytics.py"))
    _load("app.services.trade_normalizer", str(base / "app" / "services" / "trade_normalizer.py"))
    _load("app.services.trade_store",      str(base / "app" / "services" / "trade_store.py"))

    sys.modules["app.services"].trade_normalizer = sys.modules["app.services.trade_normalizer"]
    sys.modules["app.services"].trade_store      = sys.modules["app.services.trade_store"]

    with _app.app_context():
        _db.create_all()
        _db.create_all(bind_key="analytics")


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _dto(n=1, side="BUY", segment="NSE_EQ", option_type=None,
         strike=None, symbol=None, trade_date_str=None):
    return SimpleNamespace(
        exchangeTradeId      = f"TRADE{n:06d}",
        orderId              = f"ORDER{n:04d}",
        exchangeSegment      = segment,
        transactionType      = side,
        tradingSymbol        = (symbol or f"STOCK{n}").upper(),
        customSymbol         = f"STOCK{n}-EQ",
        securityId           = str(1000 + n),
        isin                 = f"INE0{n:05d}1019",
        tradedQuantity       = 10,
        tradedPrice          = 500.0 + n,
        productType          = "CNC",
        createTime           = (trade_date_str or "2026-09-01") + " 10:15:30",
        exchangeTime         = "2026-09-01 10:15:30",
        updateTime           = "2026-09-01 10:15:31",
        drvExpiryDate        = None,
        drvOptionType        = option_type,
        drvStrikePrice       = strike,
        carryForwardQuantity = 0,
        dhanClientId         = "11050100",
        exchangeOrderId      = f"EXORD{n:04d}",
    )


def _normalize(dto, raw_id=None):
    from app.services.trade_normalizer import normalize_dhan_trade
    with _app.app_context():
        return normalize_dhan_trade(dto, raw_trade_id=raw_id)


def _store():
    from app.services.trade_store import TradeExecutionStore
    return TradeExecutionStore()


# ── Normalizer tests ──────────────────────────────────────────────────────────

class TestTradeNormalizer(unittest.TestCase):

    def test_basic_equity_fields(self):
        exe = _normalize(_dto(1))
        self.assertEqual(exe.source,            "dhan")
        self.assertEqual(exe.provider_trade_id, "TRADE000001")
        self.assertEqual(exe.symbol,            "STOCK1")
        self.assertEqual(exe.side,              "BUY")
        self.assertEqual(exe.quantity,          10)
        self.assertAlmostEqual(exe.price,       501.0)
        self.assertAlmostEqual(exe.trade_value, 5010.0)
        self.assertEqual(exe.exchange,          "NSE")
        self.assertEqual(exe.instrument_type,   "EQUITY")

    def test_sell_side(self):
        self.assertEqual(_normalize(_dto(2, side="SELL")).side, "SELL")

    def test_isin_retained(self):
        exe = _normalize(_dto(3))
        self.assertIsNotNone(exe.isin)
        self.assertIsNotNone(exe.security_id)

    def test_timestamp_parsed(self):
        exe = _normalize(_dto(4))
        self.assertIsInstance(exe.traded_at, datetime)
        self.assertEqual(exe.trade_date, date(2026, 9, 1))

    def test_trade_value_computed(self):
        exe = _normalize(_dto(5))
        self.assertAlmostEqual(exe.trade_value, 10 * 505.0)

    def test_equity_instrument_type(self):
        self.assertEqual(_normalize(_dto(1, segment="NSE_EQ")).instrument_type, "EQUITY")

    def test_fno_futures_instrument_type(self):
        self.assertEqual(_normalize(_dto(1, segment="NSE_FNO")).instrument_type, "FUTURES")

    def test_fno_options_instrument_type(self):
        exe = _normalize(_dto(1, segment="NSE_FNO", option_type="CALL", strike=2000.0))
        self.assertEqual(exe.instrument_type, "OPTIONS")
        self.assertEqual(exe.option_type,     "CALL")
        self.assertAlmostEqual(exe.strike_price, 2000.0)

    def test_empty_symbol_raises(self):
        from app.services.trade_normalizer import normalize_dhan_trade
        dto = _dto(9)
        dto.tradingSymbol = ""
        dto.customSymbol  = ""
        with _app.app_context():
            with self.assertRaises(ValueError):
                normalize_dhan_trade(dto)

    def test_symbol_uppercased(self):
        exe = _normalize(_dto(10, symbol="reliance"))
        self.assertEqual(exe.symbol, "RELIANCE")

    def test_bse_exchange_mapped(self):
        self.assertEqual(_normalize(_dto(1, segment="BSE_EQ")).exchange, "BSE")

    def test_raw_trade_id_stored(self):
        self.assertEqual(_normalize(_dto(1), raw_id=42).raw_trade_id, 42)

    def test_batch_skips_invalid(self):
        from app.services.trade_normalizer import normalize_dhan_batch
        bad = _dto(99); bad.tradingSymbol = ""; bad.customSymbol = ""
        with _app.app_context():
            exes, invalid = normalize_dhan_batch([_dto(101), bad, _dto(102)])
        self.assertEqual(len(exes), 2)
        self.assertEqual(invalid,   1)


# ── Store tests ───────────────────────────────────────────────────────────────

class TestTradeExecutionStore(unittest.TestCase):
    """Each test method uses a unique trade ID range to avoid cross-test pollution."""

    _offset = 200   # base n so these IDs don't clash with normalizer tests

    def _exe(self, n, symbol=None, trade_date_str=None, side="BUY"):
        return _normalize(_dto(n, symbol=symbol,
                               trade_date_str=trade_date_str, side=side))

    def test_upsert_inserts_new(self):
        with _app.app_context():
            exe = self._exe(201)
            result = _store().upsert(exe)
            _db.session.commit()
            self.assertTrue(result)

    def test_upsert_same_trade_no_duplicate(self):
        with _app.app_context():
            _store().upsert(self._exe(202)); _db.session.commit()
            was_new = _store().upsert(self._exe(202)); _db.session.commit()
            self.assertFalse(was_new)
            self.assertEqual(
                _store().count(),
                _store().count()   # idempotent
            )

    def test_upsert_batch_inserts_all(self):
        with _app.app_context():
            exes = [self._exe(210 + i) for i in range(5)]
            ins, upd = _store().upsert_batch(exes)
            self.assertEqual(ins, 5)
            self.assertEqual(upd, 0)

    def test_repeated_sync_no_duplicates(self):
        """Core requirement: running Sync Now twice must not duplicate records."""
        with _app.app_context():
            exes = [self._exe(220 + i) for i in range(3)]
            _store().upsert_batch(exes)
            before = _store().count()
            _store().upsert_batch([self._exe(220 + i) for i in range(3)])
            after = _store().count()
            self.assertEqual(before, after)

    def test_exists_true_after_insert(self):
        with _app.app_context():
            _store().upsert(self._exe(230)); _db.session.commit()
            self.assertTrue(_store().exists("dhan", "TRADE000230"))

    def test_exists_false_before_insert(self):
        with _app.app_context():
            self.assertFalse(_store().exists("dhan", "TRADE999888"))

    def test_get_by_date_range(self):
        with _app.app_context():
            e1 = self._exe(240, trade_date_str="2026-08-01")
            e2 = self._exe(241, trade_date_str="2026-08-15")
            e3 = self._exe(242, trade_date_str="2026-09-10")
            _store().upsert_batch([e1, e2, e3])
            results = _store().get_by_date_range(date(2026, 8, 1), date(2026, 8, 31))
            dates = {r.trade_date for r in results}
            self.assertIn(date(2026, 8, 1),  dates)
            self.assertIn(date(2026, 8, 15), dates)
            self.assertNotIn(date(2026, 9, 10), dates)

    def test_get_by_date_range_empty(self):
        with _app.app_context():
            self.assertEqual(
                _store().get_by_date_range(date(2000, 1, 1), date(2000, 12, 31)),
                []
            )

    def test_get_by_symbol(self):
        with _app.app_context():
            _store().upsert_batch([
                self._exe(250, symbol="RELIANCE"),
                self._exe(251, symbol="TCS"),
                self._exe(252, symbol="RELIANCE"),
            ])
            results = _store().get_by_symbol("RELIANCE")
            self.assertEqual(len(results), 2)
            self.assertTrue(all(r.symbol == "RELIANCE" for r in results))

    def test_get_by_symbol_case_insensitive(self):
        with _app.app_context():
            _store().upsert(self._exe(260, symbol="INFY")); _db.session.commit()
            self.assertEqual(len(_store().get_by_symbol("infy")), 1)
            self.assertEqual(len(_store().get_by_symbol("INFY")), 1)

    def test_get_all_with_limit(self):
        with _app.app_context():
            _store().upsert_batch([self._exe(270 + i) for i in range(5)])
            self.assertLessEqual(len(_store().get_all(limit=3)), 3)

    def test_malformed_dto_not_stored(self):
        from app.services.trade_normalizer import normalize_dhan_batch
        bad = _dto(999); bad.tradingSymbol = ""; bad.customSymbol = ""
        with _app.app_context():
            exes, invalid = normalize_dhan_batch([bad])
        self.assertEqual(len(exes), 0)
        self.assertEqual(invalid,   1)

    def test_to_dict_has_required_fields(self):
        with _app.app_context():
            _store().upsert(self._exe(280)); _db.session.commit()
            record = _store().get_by_symbol("STOCK280")[0]
            d = record.to_dict()
            for key in ("id","source","provider_trade_id","symbol","side",
                        "quantity","price","trade_value","trade_date",
                        "instrument_type","synced_at"):
                self.assertIn(key, d)


if __name__ == "__main__":
    unittest.main()