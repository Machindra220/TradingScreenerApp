"""
app/services/fifo_store.py
───────────────────────────
Phase 5 — Persist and retrieve FIFO matching results.

Each run clears previous MatchedTrade and OpenLot records for the
affected symbols before writing the new results — guarantees idempotent
re-matching. Running FIFO twice produces the same result, not duplicates.

Scope (Phase 5):
  ✅ save_matching_result(result, run_id) — persist MatchedTrade + OpenLot
  ✅ get_matched_trades(symbol, from_date, to_date) — filtered retrieval
  ✅ get_open_lots(symbol) — current open positions
  ✅ get_all_matched(limit) — all matched trades newest first
  ✅ run_and_save(executions) — convenience: engine + store in one call

NOT here:
  ❌ Analytics/aggregations (Phase 6+)
  ❌ P&L summaries
  ❌ UI routes
"""

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


def run_and_save(executions: list, run_id: Optional[str] = None) -> dict:
    """
    Convenience: run FIFO engine on executions, persist results, return summary.

    Args:
        executions: list of TradeExecution DB records (or any objects with
                    the attributes FifoEngine.match() expects)
        run_id:     optional run identifier; auto-generated if not supplied

    Returns:
        summary dict from MatchingResult.summary() plus run_id
    """
    from app.services.fifo_engine import FifoEngine

    if not run_id:
        run_id = uuid.uuid4().hex[:12]

    log.info("[FifoStore] Starting FIFO run %s on %d executions", run_id, len(executions))

    engine = FifoEngine()
    result = engine.match(executions)

    store  = FifoStore()
    store.save_matching_result(result, run_id=run_id)

    summary = result.summary()
    summary["run_id"] = run_id
    return summary


class FifoStore:
    """
    Persistence layer for FIFO matching results.

    Uses the analytics DB bind (MatchedTrade.__bind_key__ = 'analytics').
    Re-run safe: clears affected symbols before writing new results.
    """

    # ── Write ─────────────────────────────────────────────────────────────────

    def save_matching_result(self, result, run_id: Optional[str] = None) -> None:
        """
        Persist a MatchingResult (from FifoEngine.match()) to the DB.

        Clears existing MatchedTrade and OpenLot rows for every symbol
        present in result before writing the new records. This ensures
        re-running FIFO after adding new trades produces correct results
        without stale lots from previous runs.

        Args:
            result: MatchingResult dataclass from FifoEngine
            run_id: optional tag; auto-generated if not supplied
        """
        from app.extensions import db
        from app.models_analytics import MatchedTrade as MatchedTradeModel
        from app.models_analytics import OpenLot as OpenLotModel

        if run_id is None:
            run_id = uuid.uuid4().hex[:12]

        now = datetime.now(timezone.utc)

        # Collect all symbols touched in this run
        touched_symbols = set()
        for m in result.matched:
            touched_symbols.add((m.symbol, m.exchange, m.instrument_type))
        for o in result.open_lots:
            touched_symbols.add((o.symbol, o.exchange, o.instrument_type))

        # Clear previous results for affected symbols
        for sym, exch, itype in touched_symbols:
            MatchedTradeModel.query.filter_by(
                symbol=sym, exchange=exch, instrument_type=itype
            ).delete(synchronize_session=False)
            OpenLotModel.query.filter_by(
                symbol=sym, exchange=exch, instrument_type=itype
            ).delete(synchronize_session=False)

        db.session.flush()

        # Write new MatchedTrade records
        for m in result.matched:
            qty = m.quantity
            db.session.add(MatchedTradeModel(
                symbol          = m.symbol,
                exchange        = m.exchange,
                instrument_type = m.instrument_type,
                quantity        = qty,
                buy_price       = m.buy_price,
                buy_value       = round(m.buy_price * qty, 2),
                buy_date        = m.buy_date,
                buy_traded_at   = m.buy_traded_at,
                buy_trade_id    = m.buy_trade_id,
                sell_price      = m.sell_price,
                sell_value      = round(m.sell_price * qty, 2),
                sell_date       = m.sell_date,
                sell_traded_at  = m.sell_traded_at,
                sell_trade_id   = m.sell_trade_id,
                gross_pnl       = m.gross_pnl,
                holding_days    = m.holding_days,
                product_type    = m.product_type,
                matched_at      = now,
                run_id          = run_id,
            ))

        # Write new OpenLot records
        for o in result.open_lots:
            db.session.add(OpenLotModel(
                symbol          = o.symbol,
                exchange        = o.exchange,
                instrument_type = o.instrument_type,
                quantity        = o.quantity,
                buy_price       = o.buy_price,
                cost_basis      = o.cost_basis,
                buy_date        = o.buy_date,
                buy_traded_at   = o.buy_traded_at,
                buy_trade_id    = o.buy_trade_id,
                product_type    = o.product_type,
                matched_at      = now,
                run_id          = run_id,
            ))

        db.session.commit()
        log.info(
            "[FifoStore] Run %s saved: %d matched, %d open lots",
            run_id, len(result.matched), len(result.open_lots)
        )

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_matched_trades(
        self,
        symbol:    Optional[str]  = None,
        from_date: Optional[date] = None,
        to_date:   Optional[date] = None,
    ) -> list:
        """
        Retrieve MatchedTrade records.
        All filters are optional — omit to get all matched trades.
        Results ordered by sell_date ASC, buy_date ASC.
        """
        from app.models_analytics import MatchedTrade as M

        q = M.query
        if symbol:
            q = q.filter(M.symbol == symbol.upper().strip())
        if from_date:
            q = q.filter(M.sell_date >= from_date)
        if to_date:
            q = q.filter(M.sell_date <= to_date)
        return q.order_by(M.sell_date.asc(), M.buy_date.asc()).all()

    def get_open_lots(self, symbol: Optional[str] = None) -> list:
        """
        Retrieve OpenLot records (current open positions).
        Results ordered by buy_date ASC (oldest first).
        """
        from app.models_analytics import OpenLot as O

        q = O.query
        if symbol:
            q = q.filter(O.symbol == symbol.upper().strip())
        return q.order_by(O.buy_date.asc()).all()

    def get_all_matched(self, limit: Optional[int] = None) -> list:
        """All matched trades, newest sell first."""
        from app.models_analytics import MatchedTrade as M

        q = M.query.order_by(M.sell_date.desc(), M.matched_at.desc())
        if limit:
            q = q.limit(limit)
        return q.all()

    def count_matched(self) -> int:
        from app.models_analytics import MatchedTrade as M
        return M.query.count()

    def count_open(self) -> int:
        from app.models_analytics import OpenLot as O
        return O.query.count()