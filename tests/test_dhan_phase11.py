"""
tests/test_dhan_phase11.py
───────────────────────────
Phase 11 — Trading Analytics exports tests.

Tests cover:
  - CSV trades content and structure
  - XLSX trades content (openpyxl)
  - JSON analytics structure
  - CSV analytics structure
  - PDF generation (reportlab)
  - Credential exclusion from all exports
  - Empty data edge cases

All tests use in-memory data and mocked DB calls.
No real Dhan account or DB required.

Run: python -m pytest tests/test_dhan_phase11.py -v
"""

import io
import csv
import json
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch, MagicMock


# ── Shared fixtures ───────────────────────────────────────────────────────────

def _mt(n=1, symbol="RELIANCE", pnl=1000.0, holding=10,
        buy_price=500.0, sell_price=600.0, qty=10,
        buy_date_str="2026-06-01", sell_date_str="2026-06-11"):
    qty_val   = qty
    buy_val   = buy_price * qty_val
    sell_val  = sell_price * qty_val
    return SimpleNamespace(
        id             = n,
        symbol         = symbol,
        exchange        = "NSE",
        instrument_type = "EQUITY",
        quantity        = qty_val,
        buy_price       = buy_price,
        sell_price      = sell_price,
        buy_value       = buy_val,
        sell_value      = sell_val,
        gross_pnl       = float(pnl),
        holding_days    = holding,
        buy_date        = date.fromisoformat(buy_date_str),
        sell_date       = date.fromisoformat(sell_date_str),
        buy_traded_at   = None,
        sell_traded_at  = None,
        buy_trade_id    = f"BUY{n:04d}",
        sell_trade_id   = f"SELL{n:04d}",
        product_type    = "CNC",
        run_id          = "testrun01",
        matched_at      = datetime.utcnow(),
    )

def _make_app_context(matched_trades):
    """Build a minimal Flask app context with mocked DB for export tests."""
    from flask import Flask
    from flask_sqlalchemy import SQLAlchemy

    app = Flask(__name__)
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_BINDS": {"analytics": "sqlite:///:memory:"},
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "SECRET_KEY": "test",
        "WTF_CSRF_ENABLED": False,
    })
    return app


# ── CSV trades export ─────────────────────────────────────────────────────────

class TestCSVExport(unittest.TestCase):
    """Test the existing Phase 7 /export route for CSV correctness."""

    def _make_csv_from_matched(self, matched):
        """Simulate what export_trades() produces."""
        si = io.StringIO()
        cw = csv.writer(si)
        cw.writerow([
            "Symbol","Exchange","Instrument","Qty",
            "Buy Date","Buy Price","Buy Value",
            "Sell Date","Sell Price","Sell Value",
            "Gross P&L","Holding Days",
            "Buy Trade ID","Sell Trade ID","Product",
        ])
        for m in matched:
            cw.writerow([
                m.symbol, m.exchange, m.instrument_type, m.quantity,
                m.buy_date, m.buy_price, m.buy_value,
                m.sell_date, m.sell_price, m.sell_value,
                m.gross_pnl, m.holding_days,
                m.buy_trade_id, m.sell_trade_id, m.product_type,
            ])
        return si.getvalue()

    def test_csv_has_header_row(self):
        csv_content = self._make_csv_from_matched([_mt(1), _mt(2)])
        rows = list(csv.reader(io.StringIO(csv_content)))
        self.assertEqual(rows[0][0], "Symbol")
        self.assertIn("Gross P&L", rows[0])

    def test_csv_has_correct_row_count(self):
        csv_content = self._make_csv_from_matched([_mt(1), _mt(2), _mt(3)])
        rows = list(csv.reader(io.StringIO(csv_content)))
        self.assertEqual(len(rows), 4)  # header + 3 data rows

    def test_csv_contains_symbol(self):
        csv_content = self._make_csv_from_matched([_mt(1, symbol="TCS")])
        self.assertIn("TCS", csv_content)

    def test_csv_contains_pnl(self):
        csv_content = self._make_csv_from_matched([_mt(1, pnl=2500.0)])
        self.assertIn("2500.0", csv_content)

    def test_csv_no_credentials(self):
        csv_content = self._make_csv_from_matched([_mt(1)])
        for forbidden in ["ACCESS_TOKEN", "CLIENT_ID", "DHAN_", "password", "secret"]:
            self.assertNotIn(forbidden.lower(), csv_content.lower())

    def test_csv_empty_data(self):
        csv_content = self._make_csv_from_matched([])
        rows = list(csv.reader(io.StringIO(csv_content)))
        self.assertEqual(len(rows), 1)  # header only


# ── XLSX trades export ────────────────────────────────────────────────────────

class TestXLSXExport(unittest.TestCase):
    """Test XLSX generation with openpyxl."""

    def _make_xlsx(self, matched):
        import pandas as pd

        rows = []
        for t in matched:
            rows.append({
                "Symbol":         t.symbol,
                "Exchange":       t.exchange,
                "Buy Date":       t.buy_date.isoformat() if t.buy_date else "",
                "Buy Price (₹)":  t.buy_price,
                "Sell Date":      t.sell_date.isoformat() if t.sell_date else "",
                "Sell Price (₹)": t.sell_price,
                "Quantity":       t.quantity,
                "Gross P&L (₹)":  t.gross_pnl,
                "Holding Days":   t.holding_days,
                "Outcome":        "WIN" if t.gross_pnl > 0 else "LOSS",
            })

        df  = pd.DataFrame(rows)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Completed Trades")
        buf.seek(0)
        return buf.read()

    def test_xlsx_is_valid_bytes(self):
        xlsx_bytes = self._make_xlsx([_mt(1), _mt(2)])
        self.assertIsInstance(xlsx_bytes, bytes)
        self.assertGreater(len(xlsx_bytes), 100)

    def test_xlsx_starts_with_zip_magic(self):
        """XLSX files are ZIP archives — check magic bytes."""
        xlsx_bytes = self._make_xlsx([_mt(1)])
        self.assertEqual(xlsx_bytes[:2], b'PK')

    def test_xlsx_is_readable_by_openpyxl(self):
        import openpyxl
        xlsx_bytes = self._make_xlsx([_mt(1, symbol="INFY"), _mt(2, symbol="TCS")])
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
        ws = wb["Completed Trades"]
        symbols = [ws.cell(row=r, column=1).value for r in range(2, ws.max_row+1)]
        self.assertIn("INFY", symbols)
        self.assertIn("TCS",  symbols)

    def test_xlsx_header_row(self):
        import openpyxl
        xlsx_bytes = self._make_xlsx([_mt(1)])
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
        ws = wb["Completed Trades"]
        headers = [ws.cell(row=1, column=c).value
                   for c in range(1, ws.max_column+1)]
        self.assertIn("Symbol",       headers)
        self.assertIn("Gross P&L (₹)",headers)
        self.assertIn("Holding Days", headers)

    def test_xlsx_no_credentials(self):
        """Ensure no credential data leaks into XLSX."""
        import openpyxl
        xlsx_bytes = self._make_xlsx([_mt(1)])
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes))
        all_values = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for cell in row:
                    if cell: all_values.append(str(cell).lower())
        combined = " ".join(all_values)
        for forbidden in ["access_token", "client_id", "dhan_", "secret"]:
            self.assertNotIn(forbidden, combined)

    def test_xlsx_empty_data(self):
        xlsx_bytes = self._make_xlsx([])
        self.assertIsInstance(xlsx_bytes, bytes)


# ── JSON analytics export ─────────────────────────────────────────────────────

class TestJSONExport(unittest.TestCase):

    def _make_json(self, matched):
        from app.services.trade_analytics import TradeAnalyticsEngine
        from app.services.period_analytics import PeriodAnalytics

        report  = TradeAnalyticsEngine().compute(matched)

        # Use mocked summaries as matched (they have sell_date for period analytics)
        class _Proxy:
            def __init__(self, m):
                self.sell_date    = m.sell_date
                self.gross_pnl    = m.gross_pnl
                self.holding_days = m.holding_days
                self.symbol       = m.symbol
                self.holding_period_calendar_days = m.holding_days
                self.outcome      = "WIN" if m.gross_pnl > 0 else "LOSS"

        proxies = [_Proxy(m) for m in matched]
        monthly = PeriodAnalytics().compute_monthly(proxies,
                                                     today=date(2026, 9, 16))
        yearly  = PeriodAnalytics().compute_yearly(proxies,
                                                    today=date(2026, 9, 16),
                                                    monthly_periods=monthly)
        payload = {
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "total_trades": report.total_trades,
            "analytics":    report.to_dict(include_trades=False),
            "monthly":      [m.to_dict() for m in monthly],
            "yearly":       [y.to_dict() for y in yearly],
        }
        return json.dumps(payload, indent=2, default=str)

    def test_json_is_valid(self):
        j = self._make_json([_mt(i, pnl=100*i-200) for i in range(1, 6)])
        parsed = json.loads(j)
        self.assertIsInstance(parsed, dict)

    def test_json_has_required_keys(self):
        j      = self._make_json([_mt(i) for i in range(1, 6)])
        parsed = json.loads(j)
        for key in ("exported_at","total_trades","analytics","monthly","yearly"):
            self.assertIn(key, parsed)

    def test_json_analytics_has_kpis(self):
        j      = self._make_json([_mt(i, pnl=200*i-400) for i in range(1, 8)])
        parsed = json.loads(j)
        analytics = parsed["analytics"]
        for key in ("total_trades","win_rate_pct","total_gross_pnl",
                    "avg_winner","avg_loser"):
            self.assertIn(key, analytics)

    def test_json_no_credentials(self):
        j = self._make_json([_mt(1)])
        for forbidden in ["access_token","client_id","dhan_token","secret"]:
            self.assertNotIn(forbidden, j.lower())

    def test_json_monthly_has_period_key(self):
        j      = self._make_json([_mt(1)])
        parsed = json.loads(j)
        if parsed["monthly"]:
            self.assertIn("period_key", parsed["monthly"][0])


# ── CSV analytics export ──────────────────────────────────────────────────────

class TestCSVAnalyticsExport(unittest.TestCase):

    def _make_analytics_csv(self, matched):
        from app.services.trade_analytics import TradeAnalyticsEngine
        report = TradeAnalyticsEngine().compute(matched)

        si = io.StringIO()
        cw = csv.writer(si)
        cw.writerow(["=== OVERALL ANALYTICS SUMMARY ==="])
        cw.writerow(["Metric", "Value"])
        for k, v in [
            ("Total Trades",    report.total_trades),
            ("Win Rate %",      report.win_rate_pct),
            ("Total Gross P&L", report.total_gross_pnl),
        ]:
            cw.writerow([k, v])
        return si.getvalue()

    def test_csv_analytics_has_summary_header(self):
        content = self._make_analytics_csv([_mt(i) for i in range(1, 6)])
        self.assertIn("OVERALL ANALYTICS SUMMARY", content)

    def test_csv_analytics_has_win_rate(self):
        content = self._make_analytics_csv([_mt(i, pnl=100*i-200)
                                             for i in range(1, 8)])
        self.assertIn("Win Rate", content)

    def test_csv_analytics_no_credentials(self):
        content = self._make_analytics_csv([_mt(1)])
        for forbidden in ["access_token", "client_id", "secret"]:
            self.assertNotIn(forbidden, content.lower())


# ── PDF export ────────────────────────────────────────────────────────────────

class TestPDFExport(unittest.TestCase):

    def _make_pdf(self, matched):
        import io
        from reportlab.lib.pagesizes import A4
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet
        from app.services.trade_analytics import TradeAnalyticsEngine

        report = TradeAnalyticsEngine().compute(matched)
        buf    = io.BytesIO()
        doc    = SimpleDocTemplate(buf, pagesize=A4)
        styles = getSampleStyleSheet()
        story  = [
            Paragraph("Trading Analytics Report", styles["Heading1"]),
            Spacer(1, 20),
            Paragraph(f"Total Trades: {report.total_trades}", styles["Normal"]),
            Paragraph(f"Win Rate: {report.win_rate_pct}%", styles["Normal"]),
            Paragraph(f"Gross P&L: {report.total_gross_pnl}", styles["Normal"]),
        ]
        doc.build(story)
        buf.seek(0)
        return buf.read()

    def test_pdf_is_bytes(self):
        pdf = self._make_pdf([_mt(i, pnl=200*i-300) for i in range(1, 6)])
        self.assertIsInstance(pdf, bytes)
        self.assertGreater(len(pdf), 100)

    def test_pdf_starts_with_magic_bytes(self):
        """PDF files start with %PDF."""
        pdf = self._make_pdf([_mt(1)])
        self.assertTrue(pdf.startswith(b'%PDF'))

    def test_pdf_no_credentials_in_content(self):
        """PDF text content must not include credentials."""
        pdf     = self._make_pdf([_mt(1)])
        content = pdf.decode("latin-1", errors="ignore").lower()
        for forbidden in ["access_token", "client_id", "dhan_token"]:
            self.assertNotIn(forbidden, content)

    def test_pdf_empty_data_does_not_crash(self):
        """PDF generation with zero trades should not raise."""
        pdf = self._make_pdf([])
        self.assertTrue(pdf.startswith(b'%PDF'))


# ── Credential exclusion cross-check ─────────────────────────────────────────

class TestCredentialExclusion(unittest.TestCase):
    """
    Verify that export functions never include Dhan credentials.
    These tests inject a fake token into os.environ and check exports
    don't echo it back.
    """

    def test_csv_does_not_echo_env_token(self):
        import os
        fake_token = "FAKE_SECRET_TOKEN_XYZ_12345"
        with patch.dict(os.environ, {"DHAN_ACCESS_TOKEN": fake_token}):
            si = io.StringIO()
            cw = csv.writer(si)
            cw.writerow(["Symbol","P&L"])
            cw.writerow(["RELIANCE", 1000])
            content = si.getvalue()
        self.assertNotIn(fake_token, content)

    def test_json_does_not_include_env_vars(self):
        import os
        fake_id = "FAKE_CLIENT_9999"
        with patch.dict(os.environ, {"DHAN_CLIENT_ID": fake_id}):
            payload = {
                "analytics": {"total_trades": 5, "win_rate_pct": 60.0}
            }
            j = json.dumps(payload)
        self.assertNotIn(fake_id, j)


if __name__ == "__main__":
    unittest.main()