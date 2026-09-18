"""
app/services/tax_report_store.py
──────────────────────────────────
Phase 2 — Persist TaxReportRow DTOs directly into MatchedTrade records.

KEY ARCHITECTURAL DECISION:
  The Dhan Equity Tax Report contains COMPLETED, FIFO-pre-matched trade lots.
  Each row IS a matched trade — Dhan has already applied FIFO matching.
  So TaxReportRow → MatchedTrade directly, bypassing FifoEngine entirely.

DEDUP KEY:
  SHA-1 hex of: isin + buy_date + sell_date + buy_qty + sell_qty +
                avg_buy_price + avg_sell_price
  This key is deterministic — importing the same report twice produces
  the same keys and the second import finds all duplicates.

EXISTING ANALYTICS PIPELINE:
  After import, the existing pipeline works unchanged:
    MatchedTrade records
      → TradeAnalyticsEngine.compute()   (no change)
      → PeriodAnalytics.compute_*()      (no change)
      → InsightsEngine.compute()         (no change)
      → Dashboard / Charts / Exports     (no change)
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


def make_unique_key(row) -> str:
    """
    Deterministic dedup key from trade fields.
    Same trade imported twice → same key → second import skipped.
    Uses ISIN (not symbol name) as the canonical identifier.
    """
    parts = [
        str(row.isin).strip().upper(),
        str(row.buy_date),
        str(row.sell_date),
        str(row.buy_qty),
        str(row.sell_qty),
        f"{row.avg_buy_price:.4f}",
        f"{row.avg_sell_price:.4f}",
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:32]


class ImportResult:
    """Summary of one import operation."""

    def __init__(self):
        self.imported:   int = 0
        self.duplicates: int = 0
        self.rejected:   int = 0
        self.errors:     list[str] = []

    def to_dict(self) -> dict:
        return {
            "imported":   self.imported,
            "duplicates": self.duplicates,
            "rejected":   self.rejected,
            "errors":     self.errors,
        }


class TaxReportStore:
    """
    Persists TaxReportRow DTOs into MatchedTrade and TaxReportUpload tables.

    Usage:
        store  = TaxReportStore()
        result = store.import_rows(parse_result)
    """

    def import_rows(self, parse_result) -> ImportResult:
        """
        Persist all valid rows from a ParseResult.
        Creates a TaxReportUpload audit record, then upserts each row
        as a MatchedTrade. Skips duplicates by unique_key.

        Returns ImportResult with counts.
        """
        from app.extensions import db
        from app.models_analytics import MatchedTrade, TaxReportUpload

        result = ImportResult()

        # Create upload audit record
        upload = TaxReportUpload(
            filename       = parse_result.filename,
            report_period  = parse_result.report_period,
            period_from    = parse_result.period_from,
            period_to      = parse_result.period_to,
            status         = "running",
            total_rows     = parse_result.total_rows_seen,
        )
        db.session.add(upload)
        db.session.flush()  # get upload.id

        for dto in parse_result.rows:
            try:
                key = make_unique_key(dto)

                # Dedup check
                existing = MatchedTrade.query.filter_by(unique_key=key).first()
                if existing:
                    result.duplicates += 1
                    continue

                mt = self._dto_to_matched_trade(dto, key, upload.id)
                db.session.add(mt)
                result.imported += 1

                if result.imported % 50 == 0:
                    db.session.flush()

            except Exception as e:
                result.rejected += 1
                result.errors.append(
                    f"Row {dto.row_number} ({dto.security_name}): {str(e)[:80]}"
                )
                log.warning("[TaxReportStore] Row %d rejected: %s",
                            dto.row_number, e)

        # Update audit record
        upload.status         = "success" if not result.errors else "partial"
        upload.imported_rows  = result.imported
        upload.duplicate_rows = result.duplicates
        upload.rejected_rows  = result.rejected
        upload.error_message  = "; ".join(result.errors[:5]) if result.errors else None

        db.session.commit()

        log.info(
            "[TaxReportStore] Import complete: imported=%d dup=%d rejected=%d",
            result.imported, result.duplicates, result.rejected,
        )
        return result

    def _dto_to_matched_trade(self, dto, unique_key: str, upload_id: int):
        """Map one TaxReportRow DTO to a MatchedTrade DB record."""
        from app.models_analytics import MatchedTrade

        # P&L outcome
        gross_pnl = float(dto.gross_pnl)

        return MatchedTrade(
            # Instrument
            symbol          = dto.security_name.strip().upper(),
            exchange        = "NSE",        # Dhan equity is NSE/BSE; not in report
            instrument_type = "EQUITY",

            # Quantities and prices (from tax report — Dhan's FIFO-matched values)
            quantity        = dto.sell_qty,
            buy_price       = dto.avg_buy_price,
            buy_value       = round(dto.buy_value, 2),
            sell_price      = dto.avg_sell_price,
            sell_value      = round(dto.sell_value, 2),

            # Dates
            buy_date        = dto.buy_date,
            sell_date       = dto.sell_date,

            # Identifiers (no exchange trade IDs in tax report)
            buy_trade_id    = f"TAX_{unique_key[:8]}_BUY",
            sell_trade_id   = f"TAX_{unique_key[:8]}_SELL",

            # P&L from Dhan (preserved, not recalculated)
            gross_pnl       = round(gross_pnl, 2),
            holding_days    = dto.holding_period,

            # Classification
            product_type    = dto.trade_type,   # INTRADAY | SHORT_TERM | LONG_TERM

            # Source metadata
            source          = "tax_report",
            upload_id       = upload_id,
            unique_key      = unique_key,
            matched_at      = datetime.now(timezone.utc),
            run_id          = f"tax_{upload_id}",
        )

    def get_upload_history(self, limit: int = 10) -> list:
        """Return recent upload audit records, newest first."""
        from app.models_analytics import TaxReportUpload
        from sqlalchemy import desc
        return TaxReportUpload.query.order_by(
            desc(TaxReportUpload.uploaded_at)
        ).limit(limit).all()

    def count_imported(self) -> int:
        """Count MatchedTrade records from tax report source."""
        from app.models_analytics import MatchedTrade
        return MatchedTrade.query.filter_by(source='tax_report').count()