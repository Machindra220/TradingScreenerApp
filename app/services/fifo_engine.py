"""
app/services/fifo_engine.py
─────────────────────────────
Phase 5 — Deterministic FIFO trade matching engine.

INPUT:  list of TradeExecution-like objects (any object with the fields
        listed in _ExecutionProtocol below).
OUTPUT: (list[MatchedTrade], list[OpenLot])

RULES:
  • Group by (symbol, exchange, instrument_type) — each group is matched
    independently. F&O contracts on NSE and equity on BSE never cross-match.
  • Within each group, sort ALL executions by (traded_at, id) ascending
    before matching — guarantees determinism regardless of API fetch order.
  • BUY lots enter a FIFO deque (oldest first).
  • SELL quantities consume the oldest available BUY lots first.
  • Partial consumption splits a BUY lot: the consumed portion becomes a
    MatchedTrade, the remainder stays in the deque as the new front lot.
  • P&L = gross only: (sell_price - buy_price) × qty, no charges.
  • Duplicate executions: caller must deduplicate before calling match().
    The engine processes whatever it receives.

This module is intentionally DB-free so it can be unit-tested without Flask
or SQLAlchemy. The fifo_store module handles persistence.
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional, Any

log = logging.getLogger(__name__)


# ── Output dataclasses ────────────────────────────────────────────────────────

@dataclass
class MatchedTrade:
    """
    A single FIFO-matched buy→sell lot pair.
    One MatchedTrade corresponds to ONE split of a BUY lot consumed by ONE SELL.

    gross_pnl = (sell_price - buy_price) × quantity
    holding_days = (sell_date - buy_date).days  (calendar days)
    """
    # Instrument identity
    symbol:          str
    exchange:        str
    instrument_type: str

    # Buy side
    buy_quantity:    int
    buy_price:       float
    buy_date:        Optional[date]
    buy_traded_at:   Optional[datetime]
    buy_trade_id:    str               # provider_trade_id of the buy execution

    # Sell side
    sell_quantity:   int               # == buy_quantity (always equal in one match)
    sell_price:      float
    sell_date:       Optional[date]
    sell_traded_at:  Optional[datetime]
    sell_trade_id:   str               # provider_trade_id of the sell execution

    # Computed
    quantity:        int               # matched qty (== buy_quantity == sell_quantity)
    gross_pnl:       float             # (sell_price - buy_price) × quantity
    holding_days:    Optional[int]     # calendar days; None if dates missing

    # Classification
    product_type:    Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "symbol":          self.symbol,
            "exchange":        self.exchange,
            "instrument_type": self.instrument_type,
            "quantity":        self.quantity,
            "buy_price":       self.buy_price,
            "buy_date":        self.buy_date.isoformat()     if self.buy_date  else None,
            "sell_price":      self.sell_price,
            "sell_date":       self.sell_date.isoformat()    if self.sell_date else None,
            "gross_pnl":       self.gross_pnl,
            "holding_days":    self.holding_days,
            "buy_trade_id":    self.buy_trade_id,
            "sell_trade_id":   self.sell_trade_id,
            "product_type":    self.product_type,
        }


@dataclass
class OpenLot:
    """
    A remaining unmatched BUY quantity after all available SELLs have been
    applied. Represents an open position still held by the trader.
    """
    symbol:          str
    exchange:        str
    instrument_type: str

    quantity:        int
    buy_price:       float
    buy_date:        Optional[date]
    buy_traded_at:   Optional[datetime]
    buy_trade_id:    str

    product_type:    Optional[str] = None

    @property
    def cost_basis(self) -> float:
        """Total cost of the open lot."""
        return round(self.buy_price * self.quantity, 2)

    def to_dict(self) -> dict:
        return {
            "symbol":          self.symbol,
            "exchange":        self.exchange,
            "instrument_type": self.instrument_type,
            "quantity":        self.quantity,
            "buy_price":       self.buy_price,
            "buy_date":        self.buy_date.isoformat() if self.buy_date else None,
            "cost_basis":      self.cost_basis,
            "buy_trade_id":    self.buy_trade_id,
            "product_type":    self.product_type,
        }


# ── Internal lot used inside the deque ───────────────────────────────────────

@dataclass
class _BuyLot:
    """Mutable working copy of a BUY execution inside the FIFO deque."""
    symbol:        str
    exchange:      str
    instrument_type: str
    remaining_qty: int             # decremented as sells consume it
    price:         float
    trade_date:    Optional[date]
    traded_at:     Optional[datetime]
    trade_id:      str
    product_type:  Optional[str]


# ── Matching result container ─────────────────────────────────────────────────

@dataclass
class MatchingResult:
    """
    Returned by FifoEngine.match().
    Contains all matched lots and all remaining open lots.
    """
    matched:       list[MatchedTrade] = field(default_factory=list)
    open_lots:     list[OpenLot]      = field(default_factory=list)
    unmatched_sells: list[Any]        = field(default_factory=list)  # orphan SELLs

    @property
    def total_gross_pnl(self) -> float:
        return round(sum(m.gross_pnl for m in self.matched), 2)

    @property
    def total_matched_qty(self) -> int:
        return sum(m.quantity for m in self.matched)

    @property
    def total_open_qty(self) -> int:
        return sum(o.quantity for o in self.open_lots)

    def summary(self) -> dict:
        return {
            "matched_lots":      len(self.matched),
            "open_lots":         len(self.open_lots),
            "unmatched_sells":   len(self.unmatched_sells),
            "total_matched_qty": self.total_matched_qty,
            "total_open_qty":    self.total_open_qty,
            "total_gross_pnl":   self.total_gross_pnl,
        }


# ── FIFO engine ───────────────────────────────────────────────────────────────

class FifoEngine:
    """
    Deterministic FIFO matching engine.

    Usage:
        engine = FifoEngine()
        result = engine.match(trade_executions)

    trade_executions: list of objects with attributes:
        provider_trade_id, symbol, exchange, instrument_type,
        side ('BUY'|'SELL'), quantity, price,
        traded_at (datetime|None), trade_date (date|None),
        product_type (str|None), id (int, for sort stability)
    """

    def match(self, executions: list) -> MatchingResult:
        """
        Run FIFO matching on a list of trade executions.

        Steps:
          1. Group by (symbol, exchange, instrument_type)
          2. Within each group, sort by (traded_at, id) — deterministic order
          3. Feed BUYs into a deque; consume deque with SELLs
          4. Collect MatchedTrade and remaining OpenLot records
        """
        if not executions:
            return MatchingResult()

        # Deduplicate by provider_trade_id (safety net — store should already do this)
        seen_ids: set[str] = set()
        deduped  = []
        for exe in executions:
            tid = getattr(exe, 'provider_trade_id', None) or str(id(exe))
            if tid in seen_ids:
                log.debug("[FifoEngine] Skipping duplicate trade_id: %s", tid)
                continue
            seen_ids.add(tid)
            deduped.append(exe)

        # Group
        groups: dict[tuple, list] = {}
        for exe in deduped:
            key = (
                (exe.symbol          or "").upper().strip(),
                (exe.exchange        or "").upper().strip(),
                (exe.instrument_type or "EQUITY").upper().strip(),
            )
            groups.setdefault(key, []).append(exe)

        result = MatchingResult()

        for (symbol, exchange, instrument_type), group in groups.items():
            matched, open_lots, orphan_sells = self._match_group(
                symbol, exchange, instrument_type, group
            )
            result.matched.extend(matched)
            result.open_lots.extend(open_lots)
            result.unmatched_sells.extend(orphan_sells)

        log.info(
            "[FifoEngine] Match complete: %d matched lots, %d open lots, "
            "%d orphan sells, gross P&L=%.2f",
            len(result.matched), len(result.open_lots),
            len(result.unmatched_sells), result.total_gross_pnl,
        )
        return result

    def _match_group(
        self,
        symbol: str,
        exchange: str,
        instrument_type: str,
        executions: list,
    ) -> tuple[list[MatchedTrade], list[OpenLot], list]:
        """
        FIFO match a single instrument group.

        Sort chronologically first — ensures determinism even when the API
        returns records out of order (e.g. same-day trades in arbitrary order).
        Uses (traded_at, id) as the sort key; falls back to trade_date then
        id so missing timestamps don't crash the engine.
        """

        def _sort_key(exe):
            ts = getattr(exe, 'traded_at', None)
            if ts is None:
                td = getattr(exe, 'trade_date', None)
                ts = datetime(td.year, td.month, td.day) if td else datetime.min
            return (ts, getattr(exe, 'id', 0) or 0)

        ordered = sorted(executions, key=_sort_key)

        buy_queue:     deque[_BuyLot] = deque()
        matched:       list[MatchedTrade] = []
        orphan_sells:  list = []

        for exe in ordered:
            side = (getattr(exe, 'side', '') or '').upper().strip()

            if side == 'BUY':
                qty = getattr(exe, 'quantity', 0) or 0
                if qty <= 0:
                    log.warning("[FifoEngine] BUY with qty<=0 skipped: %s",
                                getattr(exe, 'provider_trade_id', '?'))
                    continue
                buy_queue.append(_BuyLot(
                    symbol         = symbol,
                    exchange       = exchange,
                    instrument_type= instrument_type,
                    remaining_qty  = qty,
                    price          = getattr(exe, 'price', 0.0) or 0.0,
                    trade_date     = getattr(exe, 'trade_date', None),
                    traded_at      = getattr(exe, 'traded_at',  None),
                    trade_id       = getattr(exe, 'provider_trade_id', '') or '',
                    product_type   = getattr(exe, 'product_type', None),
                ))

            elif side == 'SELL':
                sell_qty   = getattr(exe, 'quantity', 0) or 0
                sell_price = getattr(exe, 'price',    0.0) or 0.0
                sell_date  = getattr(exe, 'trade_date', None)
                sell_at    = getattr(exe, 'traded_at',  None)
                sell_tid   = getattr(exe, 'provider_trade_id', '') or ''

                if sell_qty <= 0:
                    log.warning("[FifoEngine] SELL with qty<=0 skipped: %s", sell_tid)
                    continue

                remaining_sell = sell_qty

                while remaining_sell > 0 and buy_queue:
                    lot = buy_queue[0]
                    consume = min(lot.remaining_qty, remaining_sell)

                    # Holding period
                    holding_days = None
                    if lot.trade_date and sell_date:
                        holding_days = (sell_date - lot.trade_date).days

                    gross_pnl = round((sell_price - lot.price) * consume, 2)

                    matched.append(MatchedTrade(
                        symbol          = symbol,
                        exchange        = exchange,
                        instrument_type = instrument_type,
                        buy_quantity    = consume,
                        buy_price       = lot.price,
                        buy_date        = lot.trade_date,
                        buy_traded_at   = lot.traded_at,
                        buy_trade_id    = lot.trade_id,
                        sell_quantity   = consume,
                        sell_price      = sell_price,
                        sell_date       = sell_date,
                        sell_traded_at  = sell_at,
                        sell_trade_id   = sell_tid,
                        quantity        = consume,
                        gross_pnl       = gross_pnl,
                        holding_days    = holding_days,
                        product_type    = lot.product_type,
                    ))

                    lot.remaining_qty -= consume
                    remaining_sell    -= consume

                    if lot.remaining_qty == 0:
                        buy_queue.popleft()

                if remaining_sell > 0:
                    log.warning(
                        "[FifoEngine] %s: orphan SELL %d qty (no buy lot available) "
                        "trade_id=%s — short-selling not supported, qty ignored",
                        symbol, remaining_sell, sell_tid,
                    )
                    orphan_sells.append(exe)

            else:
                log.warning("[FifoEngine] Unknown side %r for trade %s — skipped",
                            side, getattr(exe, 'provider_trade_id', '?'))

        # Remaining buy_queue items are open lots
        open_lots = [
            OpenLot(
                symbol          = lot.symbol,
                exchange        = lot.exchange,
                instrument_type = lot.instrument_type,
                quantity        = lot.remaining_qty,
                buy_price       = lot.price,
                buy_date        = lot.trade_date,
                buy_traded_at   = lot.traded_at,
                buy_trade_id    = lot.trade_id,
                product_type    = lot.product_type,
            )
            for lot in buy_queue
            if lot.remaining_qty > 0
        ]

        return matched, open_lots, orphan_sells