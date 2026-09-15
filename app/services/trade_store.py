"""
app/services/trade_store.py
─────────────────────────────
Phase 4 — Persist and retrieve TradeExecution records.

All reads/writes go through this module. The rest of the application
imports TradeExecutionStore and TradeExecution — never DhanRawTradeRecord
or any Dhan-specific model.

Dedup guarantee:
    (source, provider_trade_id) is a DB-level UNIQUE constraint.
    upsert() uses INSERT OR IGNORE (SQLite) / ON CONFLICT DO UPDATE
    so repeated "Sync Now" calls are always safe.

Scope (Phase 4):
  ✅ upsert(execution)         — insert or update single record
  ✅ upsert_batch(executions)  — bulk upsert, returns (stored, skipped)
  ✅ get_by_date_range(from, to) — filter by trade_date
  ✅ get_by_symbol(symbol)       — filter by symbol (case-insensitive)
  ✅ get_all(limit)              — all records, newest first
  ✅ count()                     — total record count
  ✅ exists(source, trade_id)    — fast dedup check

NOT here:
  ❌ FIFO
  ❌ P&L
  ❌ Aggregations / analytics
"""

import logging
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError

log = logging.getLogger(__name__)


class TradeExecutionStore:
    """
    Storage layer for provider-independent TradeExecution records.

    All methods use the analytics DB bind (via TradeExecution.__bind_key__).
    Callers never interact with SQLAlchemy sessions directly.

    Usage:
        store = TradeExecutionStore()
        store.upsert(execution)
        trades = store.get_by_symbol("RELIANCE")
    """

    # ── Write operations ──────────────────────────────────────────────────────

    def upsert(self, execution) -> bool:
        """
        Insert a TradeExecution, or update it if (source, provider_trade_id)
        already exists.

        Returns True if inserted, False if updated (already existed).
        Never raises IntegrityError — handles the unique-constraint conflict.
        """
        from app.extensions import db
        from app.models_analytics import TradeExecution

        existing = TradeExecution.query.filter_by(
            source            = execution.source,
            provider_trade_id = execution.provider_trade_id,
        ).first()

        if existing:
            # Update mutable fields (price/qty may be corrected by exchange)
            existing.quantity      = execution.quantity
            existing.price         = execution.price
            existing.trade_value   = execution.trade_value
            existing.traded_at     = execution.traded_at
            existing.trade_date    = execution.trade_date
            existing.product_type  = execution.product_type
            existing.synced_at     = datetime.now(timezone.utc)
            db.session.flush()
            return False   # updated

        db.session.add(execution)
        try:
            db.session.flush()
        except IntegrityError:
            # Race condition: another thread inserted the same record
            db.session.rollback()
            log.debug(
                "[TradeStore] Race-condition duplicate skipped: %s/%s",
                execution.source, execution.provider_trade_id
            )
            return False
        return True   # inserted

    def upsert_batch(self, executions: list) -> tuple[int, int]:
        """
        Bulk upsert a list of TradeExecution records.
        Commits once at the end for efficiency.

        Returns:
            (inserted, updated) counts
        """
        from app.extensions import db

        inserted = 0
        updated  = 0

        for exe in executions:
            try:
                was_new = self.upsert(exe)
                if was_new:
                    inserted += 1
                else:
                    updated  += 1
            except Exception as e:
                log.error(
                    "[TradeStore] upsert failed for %s/%s: %s",
                    getattr(exe, 'source', '?'),
                    getattr(exe, 'provider_trade_id', '?'),
                    e,
                )

            # Flush in batches of 50 to avoid large in-memory accumulation
            if (inserted + updated) % 50 == 0:
                db.session.flush()

        db.session.commit()
        log.info(
            "[TradeStore] Batch upsert: %d inserted, %d updated",
            inserted, updated
        )
        return inserted, updated

    # ── Read operations ───────────────────────────────────────────────────────

    def get_by_date_range(
        self,
        from_date: date,
        to_date:   date,
        source:    Optional[str] = None,
    ) -> list:
        """
        Return TradeExecution records where trade_date is in [from_date, to_date].
        Optionally filter by source (e.g. 'dhan').
        Results ordered by trade_date ASC, traded_at ASC.
        """
        from app.models_analytics import TradeExecution

        q = TradeExecution.query.filter(
            TradeExecution.trade_date >= from_date,
            TradeExecution.trade_date <= to_date,
        )
        if source:
            q = q.filter(TradeExecution.source == source)
        return q.order_by(
            TradeExecution.trade_date.asc(),
            TradeExecution.traded_at.asc(),
        ).all()

    def get_by_symbol(
        self,
        symbol: str,
        source: Optional[str] = None,
    ) -> list:
        """
        Return all TradeExecution records for a given symbol (case-insensitive).
        Results ordered newest first.
        """
        from app.models_analytics import TradeExecution

        q = TradeExecution.query.filter(
            TradeExecution.symbol == symbol.strip().upper()
        )
        if source:
            q = q.filter(TradeExecution.source == source)
        return q.order_by(TradeExecution.trade_date.desc()).all()

    def get_all(
        self,
        limit:  Optional[int] = None,
        source: Optional[str] = None,
    ) -> list:
        """
        Return all TradeExecution records, newest first.
        Optional limit and source filter.
        """
        from app.models_analytics import TradeExecution

        q = TradeExecution.query
        if source:
            q = q.filter(TradeExecution.source == source)
        q = q.order_by(TradeExecution.trade_date.desc(), TradeExecution.traded_at.desc())
        if limit:
            q = q.limit(limit)
        return q.all()

    def count(self, source: Optional[str] = None) -> int:
        """Return count of stored trade executions."""
        from app.models_analytics import TradeExecution

        q = TradeExecution.query
        if source:
            q = q.filter(TradeExecution.source == source)
        return q.count()

    def exists(self, source: str, provider_trade_id: str) -> bool:
        """
        Fast existence check by (source, provider_trade_id).
        Cheaper than a full upsert when pre-screening for duplicates.
        """
        from app.models_analytics import TradeExecution

        return TradeExecution.query.filter_by(
            source            = source,
            provider_trade_id = provider_trade_id,
        ).first() is not None