"""
app/services/tax_report_parser.py
───────────────────────────────────
Parses Dhan Equity Tax Report (CSV / XLS / XLSX) into normalized
TaxReportRow DTOs, ready to be stored as MatchedTrade records.

ACTUAL REPORT STRUCTURE (inspected from real Dhan file):
  Sheet name : "Equity (2)"  (or similar "Equity" sheet)
  Report header rows: 1–6  (name, UCC, email, period string)
  Summary block: rows 7–10 (Intraday/Short/Long totals — NOT trade rows)

  SECTIONS with trade data (rows identified by integer Sr. in col[0]):
    "Equity Segment - Intraday / Speculation"
    "Equity Segment - Short Term"
    "Equity Segment - Long Term"

  SECTIONS skipped (not completed trades):
    "Equity Segment - Open Sell"          — orphan sells
    "Equity Segment - Free Holdings..."   — unrealized positions

  COLUMN MAP (col indices, 0-based):
    0   Sr.
    1   Security Name
    2   (always blank — merged cell artifact)
    3   ISIN
    4   Buy Date          (str "DD-MM-YYYY")
    5   Buy Qty.          (int)
    6   Avg. Buy Price    (float)
    7   Buy Value         (float)
    8   Sell Date         (str "DD-MM-YYYY")
    9   Sell Qty.         (int)
    10  Avg. Sell Price   (float)
    11  Sell Value        (float)
    12  Holding Period    (int, calendar days)
    13  Gross P&L         (float)
    14  Total Charges     (float — brokerage + exchange + SEBI + GST + STT + stamp)
    15  Net P&L           (float = Gross P&L - Total Charges)

  DATE FORMAT: "DD-MM-YYYY" strings (not datetime objects)
  REPORT PERIOD: "Tax Report from YYYY-MM-DD to YYYY-MM-DD" (row 5, col 12)
"""

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
MAX_FILE_SIZE_MB    = 10
ALLOWED_EXTENSIONS  = {'.csv', '.xls', '.xlsx'}

# Sections that contain completed (matched) trade rows
COMPLETED_SECTIONS  = {
    'Equity Segment - Intraday / Speculation',
    'Equity Segment - Short Term',
    'Equity Segment - Long Term',
}

# Section → trade_type label stored on TaxReportRow
SECTION_TYPE_MAP = {
    'Equity Segment - Intraday / Speculation': 'INTRADAY',
    'Equity Segment - Short Term':             'SHORT_TERM',
    'Equity Segment - Long Term':              'LONG_TERM',
}

# Required columns in header row (normalized)
REQUIRED_COLS = {
    'security name', 'isin', 'buy date', 'buy qty.',
    'avg. buy price', 'buy value', 'sell date', 'sell qty.',
    'avg. sell price', 'sell value', 'holding period',
    'gross p&l', 'total charges', 'net p&l',
}


# ── DTO ────────────────────────────────────────────────────────────────────────

@dataclass
class TaxReportRow:
    """
    One completed trade lot from the Dhan Equity Tax Report.
    Field names mirror the actual report column names (normalized).
    Source values are preserved — not recalculated.
    """
    # Identity
    security_name:   str
    isin:            str
    trade_type:      str            # INTRADAY | SHORT_TERM | LONG_TERM

    # Buy leg
    buy_date:        date
    buy_qty:         int
    avg_buy_price:   float
    buy_value:       float

    # Sell leg
    sell_date:       date
    sell_qty:        int
    avg_sell_price:  float
    sell_value:      float

    # Result (source values from Dhan — preserved as-is)
    holding_period:  int            # calendar days
    gross_pnl:       float
    total_charges:   float          # sum of all charges from report
    net_pnl:         float          # gross_pnl - total_charges

    # Import metadata (set by parser, not from row)
    row_number:      int = 0
    source_file:     str = ""


@dataclass
class ParseResult:
    """Result of parsing one file."""
    rows:            list[TaxReportRow] = field(default_factory=list)
    errors:          list[str]          = field(default_factory=list)
    warnings:        list[str]          = field(default_factory=list)
    report_period:   Optional[str]      = None   # "2024-04-01 to 2025-03-31"
    period_from:     Optional[date]     = None
    period_to:       Optional[date]     = None
    total_rows_seen: int                = 0
    valid_rows:      int                = 0
    invalid_rows:    int                = 0
    filename:        str                = ""

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0 and len(self.rows) > 0


# ── Main parser ────────────────────────────────────────────────────────────────

class TaxReportParser:
    """
    Parses Dhan Equity Tax Report files (CSV / XLS / XLSX).

    Usage:
        parser = TaxReportParser()
        result = parser.parse(file_bytes, filename="Tax-Report.xlsx")
        if result.ok:
            for row in result.rows: ...
    """

    def parse(
        self,
        file_bytes: bytes,
        filename:   str,
    ) -> ParseResult:
        """
        Entry point. Detects format, validates, parses.

        Args:
            file_bytes: raw file content
            filename:   original filename (used for extension detection)

        Returns:
            ParseResult — always returns, never raises.
        """
        result = ParseResult(filename=filename)

        # Step 1: file size
        size_mb = len(file_bytes) / 1_048_576
        if size_mb > MAX_FILE_SIZE_MB:
            result.errors.append(
                f"File too large: {size_mb:.1f} MB (max {MAX_FILE_SIZE_MB} MB)."
            )
            return result

        # Step 2: extension
        ext = _ext(filename)
        if ext not in ALLOWED_EXTENSIONS:
            result.errors.append(
                f"Unsupported file type: '{ext}'. "
                f"Supported: {', '.join(sorted(ALLOWED_EXTENSIONS))}."
            )
            return result

        # Step 3: load into rows
        try:
            raw_rows = _load_rows(file_bytes, filename)
        except Exception as e:
            result.errors.append(f"Could not read file: {e}")
            return result

        if not raw_rows:
            result.errors.append("File appears to be empty.")
            return result

        # Step 4: detect Dhan Tax Report structure
        period_str = _detect_report_period(raw_rows)
        if not period_str:
            result.errors.append(
                "This does not appear to be a Dhan Equity Tax Report. "
                "Expected to find 'Tax Report from ... to ...' header."
            )
            return result

        result.report_period = period_str
        result.period_from, result.period_to = _parse_period(period_str)

        # Step 5: parse trade rows section by section
        self._extract_trades(raw_rows, result, filename)

        log.info(
            "[TaxReportParser] %s → period=%s valid=%d invalid=%d errors=%d",
            filename, result.report_period,
            result.valid_rows, result.invalid_rows, len(result.errors),
        )
        return result

    def _extract_trades(
        self,
        rows:     list[tuple],
        result:   ParseResult,
        filename: str,
    ) -> None:
        """Walk rows, find sections, parse trade rows within completed sections."""
        in_completed_section = False
        current_type         = None
        row_num              = 0

        for i, row in enumerate(rows):
            first = row[0] if row else None

            # Section header detection
            if first and isinstance(first, str):
                sec = first.strip()
                if sec in COMPLETED_SECTIONS:
                    in_completed_section = True
                    current_type = SECTION_TYPE_MAP[sec]
                    continue
                elif sec.startswith('Equity Segment'):
                    in_completed_section = False
                    current_type = None
                    continue

            # Trade row: Sr. column is an integer
            if in_completed_section and first and isinstance(first, int):
                result.total_rows_seen += 1
                row_num = first

                dto, err = _parse_trade_row(row, current_type, row_num, filename)
                if dto:
                    result.rows.append(dto)
                    result.valid_rows += 1
                else:
                    result.invalid_rows += 1
                    if err:
                        result.warnings.append(
                            f"Row {i+1} (Sr.{row_num}): {err}"
                        )


# ── Row-level parser ───────────────────────────────────────────────────────────

def _parse_trade_row(
    row:        tuple,
    trade_type: str,
    row_num:    int,
    filename:   str,
) -> tuple[Optional[TaxReportRow], Optional[str]]:
    """
    Parse one trade row. Returns (TaxReportRow, None) on success,
    (None, error_message) on failure.
    Column indices based on actual Dhan report structure.
    """
    try:
        security_name  = _str_val(row, 1)
        isin           = _str_val(row, 3)
        buy_date_raw   = _str_val(row, 4)
        buy_qty        = _int_val(row, 5)
        avg_buy_price  = _float_val(row, 6)
        buy_value      = _float_val(row, 7)
        sell_date_raw  = _str_val(row, 8)
        sell_qty       = _int_val(row, 9)
        avg_sell_price = _float_val(row, 10)
        sell_value     = _float_val(row, 11)
        holding_period = _int_val(row, 12)
        gross_pnl      = _float_val(row, 13)
        total_charges  = _float_val(row, 14)
        net_pnl        = _float_val(row, 15)

        # Validate required fields
        if not security_name:
            return None, "Missing Security Name"
        if not isin:
            return None, "Missing ISIN"

        buy_date  = _parse_date(buy_date_raw)
        sell_date = _parse_date(sell_date_raw)

        if buy_date is None:
            return None, f"Invalid Buy Date: {buy_date_raw!r}"
        if sell_date is None:
            return None, f"Invalid Sell Date: {sell_date_raw!r}"
        if buy_qty <= 0:
            return None, f"Invalid Buy Qty: {buy_qty}"
        if sell_qty <= 0:
            return None, f"Invalid Sell Qty: {sell_qty}"

        return TaxReportRow(
            security_name  = security_name,
            isin           = isin,
            trade_type     = trade_type,
            buy_date       = buy_date,
            buy_qty        = buy_qty,
            avg_buy_price  = avg_buy_price,
            buy_value      = buy_value,
            sell_date      = sell_date,
            sell_qty       = sell_qty,
            avg_sell_price = avg_sell_price,
            sell_value     = sell_value,
            holding_period = holding_period,
            gross_pnl      = round(gross_pnl, 4),
            total_charges  = round(total_charges, 4),
            net_pnl        = round(net_pnl, 4),
            row_number     = row_num,
            source_file    = filename,
        ), None

    except Exception as e:
        return None, str(e)[:100]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ext(filename: str) -> str:
    import os
    return os.path.splitext(filename.lower())[1]


def _load_rows(file_bytes: bytes, filename: str) -> list[tuple]:
    """Load raw rows from CSV, XLS, or XLSX."""
    import pandas as pd

    ext  = _ext(filename)
    buf  = io.BytesIO(file_bytes)

    if ext == '.csv':
        df = pd.read_csv(buf, header=None, dtype=str)
    elif ext == '.xls':
        df = pd.read_excel(buf, header=None, engine='xlrd', sheet_name=0)
    else:  # .xlsx
        # Find the equity sheet
        xl   = pd.ExcelFile(buf, engine='openpyxl')
        sheet = next(
            (s for s in xl.sheet_names if 'equity' in s.lower()),
            xl.sheet_names[0]
        )
        df = pd.read_excel(buf, header=None, sheet_name=sheet, engine='openpyxl')

    # Convert to list of tuples, replacing NaN with None
    import numpy as np
    rows = []
    for row in df.itertuples(index=False, name=None):
        rows.append(tuple(None if (v is None or (isinstance(v, float) and np.isnan(v)))
                          else v for v in row))
    return rows


def _detect_report_period(rows: list[tuple]) -> Optional[str]:
    """Find 'Tax Report from YYYY-MM-DD to YYYY-MM-DD' in first 10 rows."""
    for row in rows[:10]:
        for cell in row:
            if cell and isinstance(cell, str) and 'Tax Report from' in cell:
                m = re.search(
                    r'Tax Report from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})',
                    cell
                )
                if m:
                    return f"{m.group(1)} to {m.group(2)}"
    return None


def _parse_period(period_str: str) -> tuple[Optional[date], Optional[date]]:
    """Parse "YYYY-MM-DD to YYYY-MM-DD" into two date objects."""
    try:
        parts = period_str.split(' to ')
        return (
            datetime.strptime(parts[0].strip(), '%Y-%m-%d').date(),
            datetime.strptime(parts[1].strip(), '%Y-%m-%d').date(),
        )
    except Exception:
        return None, None


def _parse_date(raw) -> Optional[date]:
    """Parse 'DD-MM-YYYY' string. Returns None on failure."""
    if raw is None:
        return None
    s = str(raw).strip()
    for fmt in ('%d-%m-%Y', '%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _str_val(row, idx) -> str:
    try:
        v = row[idx]
        return str(v).strip() if v is not None else ""
    except IndexError:
        return ""


def _float_val(row, idx) -> float:
    try:
        v = row[idx]
        if v is None:
            return 0.0
        return float(v)
    except (IndexError, TypeError, ValueError):
        return 0.0


def _int_val(row, idx) -> int:
    try:
        v = row[idx]
        if v is None:
            return 0
        return int(float(str(v)))
    except (IndexError, TypeError, ValueError):
        return 0


# Alias used by tests (convenience wrapper around store.make_unique_key)
def make_unique_key_from_fields(isin, buy_date, sell_date, buy_qty, sell_qty,
                                 avg_buy_price, avg_sell_price) -> str:
    """Convenience function for tests — mirrors TaxReportStore.make_unique_key()."""
    from types import SimpleNamespace
    return __import__('app.services.tax_report_store',
                      fromlist=['make_unique_key']).make_unique_key(
        SimpleNamespace(
            isin=isin, buy_date=buy_date, sell_date=sell_date,
            buy_qty=buy_qty, sell_qty=sell_qty,
            avg_buy_price=avg_buy_price, avg_sell_price=avg_sell_price,
        )
    )