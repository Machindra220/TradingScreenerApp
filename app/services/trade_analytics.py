"""
app/services/trade_analytics.py
─────────────────────────────────
Phase 6 — Completed-trade analytics layer.

INPUT:  list of MatchedTrade objects (DB model rows or dataclasses — any
        object with the attributes listed in _REQUIRED_ATTRS below).

OUTPUT:
  CompletedTradeSummary — per-trade computed view (dataclass)
  AnalyticsReport       — all aggregates (dataclass)

HOLDING PERIOD:
  Uses calendar days only (holding_period_calendar_days).
  Trading-day calculation requires a market calendar library
  (exchange_calendars, pandas_market_calendars) which is not installed.
  Trading days are NOT calculated — labeled clearly so callers know
  what they are getting. Install a calendar lib and add a separate
  method if trading-day accuracy is later required.

P&L:
  Gross only: sell_value - buy_value.
  No brokerage, STT, GST, or exchange charges — source data does not
  contain charge allocation per lot. Do not invent these.

AGGREGATIONS INCLUDED (deterministic, computable from available data):
  ✅ total_trades, winning, losing, breakeven, win_rate
  ✅ avg_winner, avg_loser, largest_winner, largest_loser
  ✅ avg_holding_calendar_days, median_holding_calendar_days
  ✅ min_holding_calendar_days, max_holding_calendar_days
  ✅ total_gross_pnl, total_buy_value, total_sell_value
  ✅ return_pct per trade

AGGREGATIONS EXPLICITLY EXCLUDED (data not available):
  ❌ true R-multiple (no stop-loss data)
  ❌ MAE / MFE (no intraday high/low per trade)
  ❌ strategy expectancy (no strategy tag per trade)
  ❌ trading days (no market calendar installed)
  ❌ net P&L after charges (no per-lot charge data)
"""

import logging
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

log = logging.getLogger(__name__)

# Required attributes on each matched trade object passed to compute()
_REQUIRED_ATTRS = (
    'symbol', 'exchange', 'instrument_type',
    'quantity', 'buy_price', 'sell_price',
    'buy_date', 'sell_date', 'holding_days',
    'gross_pnl', 'buy_trade_id', 'sell_trade_id',
)


# ── Per-trade summary ─────────────────────────────────────────────────────────

@dataclass
class CompletedTradeSummary:
    """
    Enriched view of a single completed (FIFO-matched) trade lot.

    All fields that exist on the underlying MatchedTrade DB record are
    carried through unchanged. Additional computed fields (return_pct,
    outcome) are added here.

    holding_period_calendar_days is an alias for holding_days to make
    the calendar-day nature explicit. Trading days are NOT provided.
    """
    # Instrument
    symbol:                       str
    exchange:                     str
    instrument_type:              str

    # Execution
    buy_trade_id:                 str
    sell_trade_id:                str
    quantity:                     int
    buy_price:                    float
    sell_price:                   float
    buy_value:                    float           # qty × buy_price
    sell_value:                   float           # qty × sell_price

    # Timestamps
    buy_date:                     Optional[date]
    sell_date:                    Optional[date]
    buy_traded_at:                Optional[datetime]
    sell_traded_at:               Optional[datetime]

    # Result
    gross_pnl:                    float           # sell_value - buy_value (gross only)
    return_pct:                   Optional[float] # gross_pnl / buy_value × 100
    holding_period_calendar_days: Optional[int]   # calendar days; trading days N/A

    # Classification
    outcome:                      str             # 'WIN' | 'LOSS' | 'BREAKEVEN'
    product_type:                 Optional[str]
    isin:                         Optional[str] = None
    security_id:                  Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "symbol":                       self.symbol,
            "exchange":                     self.exchange,
            "instrument_type":              self.instrument_type,
            "buy_trade_id":                 self.buy_trade_id,
            "sell_trade_id":                self.sell_trade_id,
            "quantity":                     self.quantity,
            "buy_price":                    self.buy_price,
            "sell_price":                   self.sell_price,
            "buy_value":                    self.buy_value,
            "sell_value":                   self.sell_value,
            "buy_date":                     self.buy_date.isoformat()       if self.buy_date       else None,
            "sell_date":                    self.sell_date.isoformat()      if self.sell_date      else None,
            "buy_traded_at":                self.buy_traded_at.isoformat()  if self.buy_traded_at  else None,
            "sell_traded_at":               self.sell_traded_at.isoformat() if self.sell_traded_at else None,
            "gross_pnl":                    self.gross_pnl,
            "return_pct":                   self.return_pct,
            "holding_period_calendar_days": self.holding_period_calendar_days,
            "holding_period_trading_days":  None,   # not available — no calendar lib
            "outcome":                      self.outcome,
            "product_type":                 self.product_type,
            "isin":                         self.isin,
        }


# ── Aggregates ────────────────────────────────────────────────────────────────

@dataclass
class AnalyticsReport:
    """
    Aggregate analytics across a set of completed trades.

    All monetary values are gross (before brokerage/taxes).
    All holding periods are calendar days.

    Fields with None indicate insufficient data (e.g. no winning trades,
    so avg_winner cannot be computed).
    """
    # Trade counts
    total_trades:    int   = 0
    winning_trades:  int   = 0
    losing_trades:   int   = 0
    breakeven_trades:int   = 0

    # Win rate — None if no trades
    win_rate_pct:    Optional[float] = None   # winning / total × 100

    # P&L aggregates
    total_gross_pnl: float = 0.0
    total_buy_value: float = 0.0
    total_sell_value:float = 0.0

    # Winner stats (None if no winning trades)
    avg_winner:      Optional[float] = None   # avg gross_pnl of winning trades
    largest_winner:  Optional[float] = None
    avg_winner_return_pct: Optional[float] = None

    # Loser stats (None if no losing trades)
    avg_loser:       Optional[float] = None   # avg gross_pnl of losing trades (negative)
    largest_loser:   Optional[float] = None
    avg_loser_return_pct:  Optional[float] = None

    # Holding period — calendar days only
    avg_holding_calendar_days:    Optional[float] = None
    median_holding_calendar_days: Optional[float] = None
    min_holding_calendar_days:    Optional[int]   = None
    max_holding_calendar_days:    Optional[int]   = None

    # Note: trading-day holding period not calculated (no calendar lib)
    holding_period_note: str = (
        "Holding period in calendar days. "
        "Trading-day calculation not available: "
        "install exchange_calendars and add a calendar adapter."
    )

    # Per-trade summaries
    trades: list[CompletedTradeSummary] = field(default_factory=list)

    def to_dict(self, include_trades: bool = True) -> dict:
        d = {
            "total_trades":                   self.total_trades,
            "winning_trades":                 self.winning_trades,
            "losing_trades":                  self.losing_trades,
            "breakeven_trades":               self.breakeven_trades,
            "win_rate_pct":                   self.win_rate_pct,
            "total_gross_pnl":                self.total_gross_pnl,
            "total_buy_value":                self.total_buy_value,
            "total_sell_value":               self.total_sell_value,
            "avg_winner":                     self.avg_winner,
            "largest_winner":                 self.largest_winner,
            "avg_winner_return_pct":          self.avg_winner_return_pct,
            "avg_loser":                      self.avg_loser,
            "largest_loser":                  self.largest_loser,
            "avg_loser_return_pct":           self.avg_loser_return_pct,
            "avg_holding_calendar_days":      self.avg_holding_calendar_days,
            "median_holding_calendar_days":   self.median_holding_calendar_days,
            "min_holding_calendar_days":      self.min_holding_calendar_days,
            "max_holding_calendar_days":      self.max_holding_calendar_days,
            "holding_period_note":            self.holding_period_note,
        }
        if include_trades:
            d["trades"] = [t.to_dict() for t in self.trades]
        return d


# ── Analytics engine ──────────────────────────────────────────────────────────

class TradeAnalyticsEngine:
    """
    Computes CompletedTradeSummary and AnalyticsReport from a list of
    MatchedTrade-like objects (DB rows or dataclasses).

    Usage:
        engine = TradeAnalyticsEngine()
        report = engine.compute(matched_trades)

    The engine is stateless — call compute() as many times as needed.
    It never writes to the DB.
    """

    def compute(self, matched_trades: list) -> AnalyticsReport:
        """
        Main entry point: compute full analytics from a list of matched trades.

        Args:
            matched_trades: list of MatchedTrade DB records or dataclasses.
                            Must have the attributes in _REQUIRED_ATTRS.

        Returns:
            AnalyticsReport with per-trade summaries and all aggregates.
        """
        if not matched_trades:
            return AnalyticsReport()

        summaries = []
        for mt in matched_trades:
            try:
                s = self._summarise_trade(mt)
                summaries.append(s)
            except Exception as e:
                log.warning(
                    "[TradeAnalytics] Skipping trade %s/%s: %s",
                    getattr(mt, 'buy_trade_id', '?'),
                    getattr(mt, 'sell_trade_id', '?'),
                    e,
                )

        report = self._aggregate(summaries)
        log.info(
            "[TradeAnalytics] Computed %d trades: "
            "W=%d L=%d BE=%d win_rate=%.1f%% gross_pnl=%.2f",
            report.total_trades, report.winning_trades,
            report.losing_trades, report.breakeven_trades,
            report.win_rate_pct or 0, report.total_gross_pnl,
        )
        return report

    # ── Per-trade ─────────────────────────────────────────────────────────────

    def _summarise_trade(self, mt) -> CompletedTradeSummary:
        """Convert one MatchedTrade record into a CompletedTradeSummary."""
        qty        = int(getattr(mt, 'quantity',   0)   or 0)
        buy_price  = float(getattr(mt, 'buy_price',  0.0) or 0.0)
        sell_price = float(getattr(mt, 'sell_price', 0.0) or 0.0)
        gross_pnl  = float(getattr(mt, 'gross_pnl', 0.0) or 0.0)

        buy_value  = round(qty * buy_price,  2)
        sell_value = round(qty * sell_price, 2)

        # Return % = gross_pnl / buy_value × 100
        return_pct: Optional[float] = None
        if buy_value != 0:
            return_pct = round(gross_pnl / buy_value * 100, 4)

        # Holding period — calendar days only
        holding_days: Optional[int] = getattr(mt, 'holding_days', None)

        # Outcome classification
        if gross_pnl > 0:
            outcome = "WIN"
        elif gross_pnl < 0:
            outcome = "LOSS"
        else:
            outcome = "BREAKEVEN"

        return CompletedTradeSummary(
            symbol                       = str(getattr(mt, 'symbol',          '') or ''),
            exchange                     = str(getattr(mt, 'exchange',         '') or ''),
            instrument_type              = str(getattr(mt, 'instrument_type',  'EQUITY') or 'EQUITY'),
            buy_trade_id                 = str(getattr(mt, 'buy_trade_id',     '') or ''),
            sell_trade_id                = str(getattr(mt, 'sell_trade_id',    '') or ''),
            quantity                     = qty,
            buy_price                    = buy_price,
            sell_price                   = sell_price,
            buy_value                    = buy_value,
            sell_value                   = sell_value,
            buy_date                     = getattr(mt, 'buy_date',      None),
            sell_date                    = getattr(mt, 'sell_date',     None),
            buy_traded_at                = getattr(mt, 'buy_traded_at', None),
            sell_traded_at               = getattr(mt, 'sell_traded_at',None),
            gross_pnl                    = round(gross_pnl, 2),
            return_pct                   = return_pct,
            holding_period_calendar_days = holding_days,
            outcome                      = outcome,
            product_type                 = getattr(mt, 'product_type', None),
            isin                         = getattr(mt, 'isin',        None),
            security_id                  = getattr(mt, 'security_id', None),
        )

    # ── Aggregates ────────────────────────────────────────────────────────────

    def _aggregate(self, summaries: list[CompletedTradeSummary]) -> AnalyticsReport:
        """Compute all aggregate metrics from a list of CompletedTradeSummary."""
        if not summaries:
            return AnalyticsReport()

        winners   = [s for s in summaries if s.outcome == "WIN"]
        losers    = [s for s in summaries if s.outcome == "LOSS"]
        breakevens= [s for s in summaries if s.outcome == "BREAKEVEN"]

        total = len(summaries)
        win_rate = round(len(winners) / total * 100, 2) if total else None

        # P&L totals
        total_pnl       = round(sum(s.gross_pnl  for s in summaries), 2)
        total_buy_val   = round(sum(s.buy_value   for s in summaries), 2)
        total_sell_val  = round(sum(s.sell_value  for s in summaries), 2)

        # Winner stats
        avg_winner = largest_winner = avg_winner_ret = None
        if winners:
            w_pnls        = [s.gross_pnl  for s in winners]
            w_rets        = [s.return_pct for s in winners if s.return_pct is not None]
            avg_winner    = round(statistics.mean(w_pnls), 2)
            largest_winner= round(max(w_pnls), 2)
            avg_winner_ret= round(statistics.mean(w_rets), 4) if w_rets else None

        # Loser stats
        avg_loser = largest_loser = avg_loser_ret = None
        if losers:
            l_pnls       = [s.gross_pnl  for s in losers]
            l_rets       = [s.return_pct for s in losers if s.return_pct is not None]
            avg_loser    = round(statistics.mean(l_pnls), 2)
            largest_loser= round(min(l_pnls), 2)    # most negative = largest loss
            avg_loser_ret= round(statistics.mean(l_rets), 4) if l_rets else None

        # Holding period — calendar days only, skip None
        holding_days = [
            s.holding_period_calendar_days
            for s in summaries
            if s.holding_period_calendar_days is not None
        ]
        avg_hold    = round(statistics.mean(holding_days),   2) if holding_days else None
        med_hold    = round(statistics.median(holding_days), 2) if holding_days else None
        min_hold    = min(holding_days) if holding_days else None
        max_hold    = max(holding_days) if holding_days else None

        return AnalyticsReport(
            total_trades                  = total,
            winning_trades                = len(winners),
            losing_trades                 = len(losers),
            breakeven_trades              = len(breakevens),
            win_rate_pct                  = win_rate,
            total_gross_pnl               = total_pnl,
            total_buy_value               = total_buy_val,
            total_sell_value              = total_sell_val,
            avg_winner                    = avg_winner,
            largest_winner                = largest_winner,
            avg_winner_return_pct         = avg_winner_ret,
            avg_loser                     = avg_loser,
            largest_loser                 = largest_loser,
            avg_loser_return_pct          = avg_loser_ret,
            avg_holding_calendar_days     = avg_hold,
            median_holding_calendar_days  = med_hold,
            min_holding_calendar_days     = min_hold,
            max_holding_calendar_days     = max_hold,
            trades                        = summaries,
        )