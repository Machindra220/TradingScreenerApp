"""
tests/test_dhan_phase3.py
──────────────────────────
Phase 3 focused tests — historical trade fetch, pagination, dedup.

All tests use mocked HTTP responses — no real Dhan account needed.

Run with:
    python -m pytest tests/test_dhan_phase3.py -v
"""

import os
import unittest
from datetime import date, timedelta
from dataclasses import asdict
from unittest.mock import patch, MagicMock, call

from app.services.dhan_client import DhanClient, DhanAPIError, DhanAuthError
from app.services.dhan_trade_sync import (
    DhanRawTrade,
    DhanTradeSync,
    SyncResult,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_client():
    return DhanClient(client_id="11050100", access_token="test_token")

def _make_syncer():
    return DhanTradeSync(_make_client())

def _raw_trade(n=1, side="BUY") -> dict:
    """Build a minimal valid Dhan API trade dict."""
    return {
        "dhanClientId":          "11050100",
        "orderId":               f"ORDER{n:04d}",
        "exchangeOrderId":       f"EXORD{n:04d}",
        "exchangeTradeId":       f"TRADE{n:06d}",
        "transactionType":       side,
        "exchangeSegment":       "NSE_EQ",
        "productType":           "CNC",
        "orderType":             "LIMIT",
        "tradingSymbol":         f"STOCK{n}",
        "customSymbol":          f"STOCK{n}-EQ",
        "securityId":            str(1000 + n),
        "tradedQuantity":        10,
        "tradedPrice":           500.0 + n,
        "isin":                  f"INE00{n:04d}01019",
        "carryForwardQuantity":  0,
        "createTime":            "2026-09-01 10:15:30",
        "updateTime":            "2026-09-01 10:15:31",
        "exchangeTime":          "2026-09-01 10:15:30",
        "drvExpiryDate":         None,
        "drvOptionType":         None,
        "drvStrikePrice":        None,
    }

FROM_DATE = date(2026, 4, 1)
TO_DATE   = date(2026, 6, 29)   # 89 days — within 90-day limit


# ── DhanRawTrade DTO ──────────────────────────────────────────────────────────

class TestDhanRawTradeDTO(unittest.TestCase):

    def test_from_api_dict_valid(self):
        dto = DhanRawTrade.from_api_dict(_raw_trade(1))
        self.assertEqual(dto.exchangeTradeId, "TRADE000001")
        self.assertEqual(dto.transactionType, "BUY")
        self.assertEqual(dto.tradedQuantity,  10)
        self.assertAlmostEqual(dto.tradedPrice, 501.0)
        self.assertEqual(dto.tradingSymbol,   "STOCK1")

    def test_from_api_dict_sell(self):
        dto = DhanRawTrade.from_api_dict(_raw_trade(2, "SELL"))
        self.assertEqual(dto.transactionType, "SELL")

    def test_from_api_dict_missing_trade_id_raises(self):
        raw = _raw_trade(1)
        raw["exchangeTradeId"] = ""
        with self.assertRaises(ValueError) as ctx:
            DhanRawTrade.from_api_dict(raw)
        self.assertIn("exchangeTradeId", str(ctx.exception))

    def test_from_api_dict_missing_symbol_raises(self):
        raw = _raw_trade(1)
        raw["tradingSymbol"] = ""
        with self.assertRaises(ValueError):
            DhanRawTrade.from_api_dict(raw)

    def test_from_api_dict_invalid_side_raises(self):
        raw = _raw_trade(1)
        raw["transactionType"] = "UNKNOWN"
        with self.assertRaises(ValueError) as ctx:
            DhanRawTrade.from_api_dict(raw)
        self.assertIn("transactionType", str(ctx.exception))

    def test_from_api_dict_handles_null_optional_fields(self):
        raw = _raw_trade(1)
        raw["isin"]             = None
        raw["drvExpiryDate"]    = None
        raw["drvOptionType"]    = None
        raw["drvStrikePrice"]   = None
        raw["carryForwardQuantity"] = None
        dto = DhanRawTrade.from_api_dict(raw)
        self.assertIsNone(dto.isin)
        self.assertIsNone(dto.drvStrikePrice)

    def test_from_api_dict_handles_bad_numeric_types(self):
        raw = _raw_trade(1)
        raw["tradedQuantity"] = "not_a_number"
        raw["tradedPrice"]    = None
        dto = DhanRawTrade.from_api_dict(raw)
        self.assertEqual(dto.tradedQuantity, 0)
        self.assertEqual(dto.tradedPrice,    0.0)

    def test_all_required_fields_retained(self):
        dto = DhanRawTrade.from_api_dict(_raw_trade(1))
        self.assertIsNotNone(dto.dhanClientId)
        self.assertIsNotNone(dto.orderId)
        self.assertIsNotNone(dto.exchangeSegment)
        self.assertIsNotNone(dto.productType)
        self.assertIsNotNone(dto.securityId)
        self.assertIsNotNone(dto.isin)
        self.assertIsNotNone(dto.createTime)


# ── Date validation ───────────────────────────────────────────────────────────

class TestDateValidation(unittest.TestCase):

    def test_valid_range_passes(self):
        DhanTradeSync.validate_date_range(FROM_DATE, TO_DATE)  # no exception

    def test_from_after_to_raises(self):
        with self.assertRaises(ValueError) as ctx:
            DhanTradeSync.validate_date_range(
                date(2026, 6, 1), date(2026, 4, 1)
            )
        self.assertIn("before", str(ctx.exception))

    def test_future_to_date_raises(self):
        future = date.today() + timedelta(days=5)
        with self.assertRaises(ValueError) as ctx:
            DhanTradeSync.validate_date_range(date(2026, 1, 1), future)
        self.assertIn("future", str(ctx.exception))

    def test_range_over_90_days_raises(self):
        with self.assertRaises(ValueError) as ctx:
            DhanTradeSync.validate_date_range(
                date(2026, 1, 1), date(2026, 5, 1)  # 120 days
            )
        self.assertIn("90", str(ctx.exception))

    def test_exactly_90_days_passes(self):
        to = date.today() - timedelta(days=1)
        frm = to - timedelta(days=90)
        DhanTradeSync.validate_date_range(frm, to)  # no exception

    def test_non_date_objects_raise(self):
        with self.assertRaises(ValueError):
            DhanTradeSync.validate_date_range("2026-01-01", "2026-02-01")


# ── Single page fetch ─────────────────────────────────────────────────────────

class TestSinglePageFetch(unittest.TestCase):

    def _mock_get(self, syncer, pages: list[list[dict]]):
        """
        Patch syncer._client._get to return pages in sequence.
        Final call returns [] to signal end of pagination.
        """
        returns = pages + [[]]   # trailing empty page
        syncer._client._get = MagicMock(side_effect=returns)

    def test_single_page_returns_all_trades(self):
        syncer = _make_syncer()
        page1  = [_raw_trade(i) for i in range(1, 6)]   # 5 trades
        self._mock_get(syncer, [page1])

        with patch('app.services.dhan_trade_sync._load_existing_trade_ids',
                   return_value=set()):
            trades, result = syncer.fetch_range(FROM_DATE, TO_DATE)

        self.assertEqual(len(trades), 5)
        self.assertEqual(result.records_fetched, 5)
        self.assertEqual(result.records_stored,  5)
        self.assertEqual(result.records_skipped, 0)
        self.assertEqual(result.pages_fetched,   1)
        self.assertEqual(result.status, "success")

    def test_empty_first_page_returns_zero(self):
        syncer = _make_syncer()
        syncer._client._get = MagicMock(return_value=[])

        with patch('app.services.dhan_trade_sync._load_existing_trade_ids',
                   return_value=set()):
            trades, result = syncer.fetch_range(FROM_DATE, TO_DATE)

        self.assertEqual(len(trades), 0)
        self.assertEqual(result.records_fetched, 0)
        self.assertEqual(result.pages_fetched,   0)
        self.assertEqual(result.status, "success")


# ── Multi-page pagination ─────────────────────────────────────────────────────

class TestMultiPagePagination(unittest.TestCase):

    def _mock_get(self, syncer, pages):
        returns = pages + [[]]
        syncer._client._get = MagicMock(side_effect=returns)

    def test_two_pages_fetched_correctly(self):
        syncer = _make_syncer()
        # PAGE_SIZE=50, so 50 records on page 0 triggers page 1
        page0 = [_raw_trade(i) for i in range(1,  51)]   # 50 trades
        page1 = [_raw_trade(i) for i in range(51, 56)]   # 5 trades

        self._mock_get(syncer, [page0, page1])
        with patch('app.services.dhan_trade_sync._load_existing_trade_ids',
                   return_value=set()):
            trades, result = syncer.fetch_range(FROM_DATE, TO_DATE)

        self.assertEqual(len(trades), 55)
        self.assertEqual(result.pages_fetched,   2)
        self.assertEqual(result.records_fetched, 55)

    def test_page_traversal_stops_on_empty(self):
        syncer = _make_syncer()
        page0 = [_raw_trade(i) for i in range(1, 51)]
        page1 = [_raw_trade(i) for i in range(51, 101)]
        # page 2 is empty → stop

        syncer._client._get = MagicMock(side_effect=[page0, page1, []])
        with patch('app.services.dhan_trade_sync._load_existing_trade_ids',
                   return_value=set()):
            trades, result = syncer.fetch_range(FROM_DATE, TO_DATE)

        self.assertEqual(result.pages_fetched, 2)
        self.assertEqual(len(trades), 100)
        self.assertEqual(syncer._client._get.call_count, 3)  # pages 0,1,2

    def test_correct_page_numbers_in_api_calls(self):
        """Verify page 0, 1, 2 are passed in sequence."""
        syncer = _make_syncer()
        page0  = [_raw_trade(i) for i in range(1, 51)]
        syncer._client._get = MagicMock(side_effect=[page0, []])

        with patch('app.services.dhan_trade_sync._load_existing_trade_ids',
                   return_value=set()):
            syncer.fetch_range(FROM_DATE, TO_DATE)

        calls = syncer._client._get.call_args_list
        self.assertIn("/0", calls[0][0][0])   # page 0 in URL path
        self.assertIn("/1", calls[1][0][0])   # page 1 in URL path


# ── Duplicate protection ──────────────────────────────────────────────────────

class TestDuplicateProtection(unittest.TestCase):

    def test_same_trade_id_on_two_pages_skipped(self):
        syncer = _make_syncer()
        # Page 0 must have PAGE_SIZE (50) records to trigger page 1 fetch.
        # The duplicate trade appears on page 1.
        page0 = [_raw_trade(i) for i in range(1, 51)]   # 50 unique trades
        dup   = _raw_trade(1)                             # TRADE000001 again on page 1
        syncer._client._get = MagicMock(side_effect=[page0, [dup], []])

        with patch('app.services.dhan_trade_sync._load_existing_trade_ids',
                   return_value=set()):
            trades, result = syncer.fetch_range(FROM_DATE, TO_DATE)

        self.assertEqual(len(trades), 50)           # 50 unique trades returned
        self.assertEqual(result.records_stored,  50)
        self.assertEqual(result.records_skipped, 1)  # dup on page 1 skipped

    def test_existing_db_ids_excluded(self):
        syncer = _make_syncer()
        page   = [_raw_trade(i) for i in range(1, 4)]  # IDs: TRADE000001,2,3
        syncer._client._get = MagicMock(side_effect=[page, []])

        # Pre-load IDs 1 and 3 as already in DB
        existing = {"TRADE000001", "TRADE000003"}
        with patch('app.services.dhan_trade_sync._load_existing_trade_ids',
                   return_value=existing):
            trades, result = syncer.fetch_range(FROM_DATE, TO_DATE)

        self.assertEqual(len(trades), 1)             # only TRADE000002 is new
        self.assertEqual(result.records_stored,  1)
        self.assertEqual(result.records_skipped, 2)

    def test_all_trades_already_in_db_returns_zero_stored(self):
        syncer = _make_syncer()
        page   = [_raw_trade(1)]
        syncer._client._get = MagicMock(side_effect=[page, []])

        with patch('app.services.dhan_trade_sync._load_existing_trade_ids',
                   return_value={"TRADE000001"}):
            trades, result = syncer.fetch_range(FROM_DATE, TO_DATE)

        self.assertEqual(len(trades), 0)
        self.assertEqual(result.records_skipped, 1)
        self.assertEqual(result.records_stored,  0)


# ── API failure handling ──────────────────────────────────────────────────────

class TestAPIFailure(unittest.TestCase):

    def test_api_error_sets_error_status(self):
        syncer = _make_syncer()
        syncer._client._get = MagicMock(
            side_effect=DhanAPIError("Dhan API returned HTTP 502 for /trades/...")
        )
        with patch('app.services.dhan_trade_sync._load_existing_trade_ids',
                   return_value=set()):
            trades, result = syncer.fetch_range(FROM_DATE, TO_DATE)

        self.assertEqual(result.status, "error")
        self.assertIsNotNone(result.error)
        self.assertEqual(len(trades), 0)

    def test_auth_error_sets_error_status(self):
        syncer = _make_syncer()
        syncer._client._get = MagicMock(
            side_effect=DhanAuthError("Token expired")
        )
        with patch('app.services.dhan_trade_sync._load_existing_trade_ids',
                   return_value=set()):
            trades, result = syncer.fetch_range(FROM_DATE, TO_DATE)

        self.assertEqual(result.status, "error")
        self.assertIsNotNone(result.error)

    def test_error_message_contains_no_token(self):
        syncer = _make_syncer()
        syncer._client._get = MagicMock(
            side_effect=DhanAPIError("Dhan API returned HTTP 500")
        )
        with patch('app.services.dhan_trade_sync._load_existing_trade_ids',
                   return_value=set()):
            _, result = syncer.fetch_range(FROM_DATE, TO_DATE)

        self.assertNotIn("test_token", result.error or "")


# ── Invalid response handling ─────────────────────────────────────────────────

class TestInvalidResponse(unittest.TestCase):

    def test_invalid_trade_counted_not_raised(self):
        syncer = _make_syncer()
        good  = _raw_trade(1)
        bad   = {"orderId": "X", "tradingSymbol": "Y"}  # missing exchangeTradeId
        syncer._client._get = MagicMock(side_effect=[[good, bad], []])

        with patch('app.services.dhan_trade_sync._load_existing_trade_ids',
                   return_value=set()):
            trades, result = syncer.fetch_range(FROM_DATE, TO_DATE)

        self.assertEqual(len(trades),            1)   # only good trade returned
        self.assertEqual(result.records_invalid, 1)
        self.assertEqual(result.records_stored,  1)

    def test_unexpected_response_shape_raises_api_error(self):
        syncer = _make_syncer()
        syncer._client._get = MagicMock(return_value={"unexpected": "dict"})

        with patch('app.services.dhan_trade_sync._load_existing_trade_ids',
                   return_value=set()):
            _, result = syncer.fetch_range(FROM_DATE, TO_DATE)

        self.assertEqual(result.status, "error")


# ── SyncResult metadata ───────────────────────────────────────────────────────

class TestSyncResultMetadata(unittest.TestCase):

    def test_result_to_dict_has_all_fields(self):
        result = SyncResult(from_date=FROM_DATE, to_date=TO_DATE)
        d = result.to_dict()
        for key in ("from_date","to_date","pages_fetched","records_fetched",
                    "records_stored","records_skipped","records_invalid",
                    "status","error"):
            self.assertIn(key, d)

    def test_successful_sync_metadata(self):
        syncer = _make_syncer()
        page   = [_raw_trade(i) for i in range(1, 6)]
        syncer._client._get = MagicMock(side_effect=[page, []])

        with patch('app.services.dhan_trade_sync._load_existing_trade_ids',
                   return_value=set()):
            _, result = syncer.fetch_range(FROM_DATE, TO_DATE)

        self.assertEqual(result.pages_fetched,   1)
        self.assertEqual(result.records_fetched, 5)
        self.assertEqual(result.records_stored,  5)
        self.assertEqual(result.records_skipped, 0)
        self.assertEqual(result.records_invalid, 0)
        self.assertEqual(result.status,          "success")
        self.assertIsNone(result.error)
        self.assertTrue(result.ok)

    def test_error_result_not_ok(self):
        syncer = _make_syncer()
        syncer._client._get = MagicMock(side_effect=DhanAPIError("fail"))
        with patch('app.services.dhan_trade_sync._load_existing_trade_ids',
                   return_value=set()):
            _, result = syncer.fetch_range(FROM_DATE, TO_DATE)
        self.assertFalse(result.ok)

    def test_progress_callback_called(self):
        syncer   = _make_syncer()
        page     = [_raw_trade(i) for i in range(1, 4)]
        syncer._client._get = MagicMock(side_effect=[page, []])
        calls    = []

        with patch('app.services.dhan_trade_sync._load_existing_trade_ids',
                   return_value=set()):
            syncer.fetch_range(FROM_DATE, TO_DATE,
                               progress_callback=lambda p, s: calls.append((p, s)))

        self.assertTrue(len(calls) >= 1)


if __name__ == "__main__":
    unittest.main()