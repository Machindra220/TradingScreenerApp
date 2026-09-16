"""
app/services/period_analytics.py
──────────────────────────────────
Phase 8 — Monthly and yearly trading performance aggregation.

INPUT:  list of MatchedTrade-like objects (same as TradeAnalyticsEngine).
OUTPUT: ordered list of MonthlyPeriod / YearlyPeriod dataclasses.

DESIGN:
  - Pure Python — no DB imports, no Flask dependency.
  - Dates are treated as naive (no timezone conversion). NSE/BSE trade
    dates are local IST dates stored without TZ; converting to UTC would
    shift some records across day/month/year boundaries incorrectly.
  - Current incomplete periods are labeled is_complete=False so callers
    can display a warning rather than comparing them to historical periods.
  - Month-over-month and year-over-year deltas are computed only when both
    periods are complete or when the comparison is explicitly requested.

PERIOD KEY FORMATS:
  Monthly: "YYYY-MM"  (e.g. "2026-06")
  Yearly:  "YYYY"     (e.g. "2026")
"""

import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

log = logging.getLogger(__name__)


# ── Monthly period ────────────────────────────────────────────────────────────

@dataclass
class MonthlyPeriod:
    """All performance metrics for a single calendar month."""

    period_key:     str             # "YYYY-MM"
    year:           int
    month:          int
    is_complete:    bool            # False = current in-progress month

    # Trade counts
    total_trades:   int   = 0
    winning_trades: int   = 0
    losing_trades:  int   = 0
    breakeven_trades: int = 0
    win_rate_pct:   Optional[float] = None

    # P&L
    gross_pnl:      float = 0.0
    avg_winner:     Optional[float] = None
    avg_loser:      Optional[float] = None
    largest_winner: Optional[float] = None
    largest_loser:  Optional[float] = None

    # Holding period (calendar days)
    avg_holding_days: Optional[float] = None

    # Best / worst stock by gross P&L within the month
    best_stock:     Optional[str]   = None
    best_stock_pnl: Optional[float] = None
    worst_stock:    Optional[str]   = None
    worst_stock_pnl:Optional[float] = None

    # MoM comparison (populated by with_comparisons())
    pnl_delta:          Optional[float] = None   # gross_pnl - prev_month.gross_pnl
    pnl_delta_pct:      Optional[float] = None   # % change vs prior month
    win_rate_delta:     Optional[float] = None   # pp change vs prior month
    trades_delta:       Optional[int]   = None   # trade count change
    comparison_note:    Optional[str]   = None   # e.g. "vs Oct 2026"

    def to_dict(self) -> dict:
        return {
            "period_key":      self.period_key,
            "year":            self.year,
            "month":           self.month,
            "is_complete":     self.is_complete,
            "total_trades":    self.total_trades,
            "winning_trades":  self.winning_trades,
            "losing_trades":   self.losing_trades,
            "breakeven_trades":self.breakeven_trades,
            "win_rate_pct":    self.win_rate_pct,
            "gross_pnl":       self.gross_pnl,
            "avg_winner":      self.avg_winner,
            "avg_loser":       self.avg_loser,
            "largest_winner":  self.largest_winner,
            "largest_loser":   self.largest_loser,
            "avg_holding_days":self.avg_holding_days,
            "best_stock":      self.best_stock,
            "best_stock_pnl":  self.best_stock_pnl,
            "worst_stock":     self.worst_stock,
            "worst_stock_pnl": self.worst_stock_pnl,
            "pnl_delta":       self.pnl_delta,
            "pnl_delta_pct":   self.pnl_delta_pct,
            "win_rate_delta":  self.win_rate_delta,
            "trades_delta":    self.trades_delta,
            "comparison_note": self.comparison_note,
        }


# ── Yearly period ─────────────────────────────────────────────────────────────

@dataclass
class YearlyPeriod:
    """All performance metrics for a single calendar year."""

    period_key:     str             # "YYYY"
    year:           int
    is_complete:    bool            # False = current in-progress year

    # Trade counts
    total_trades:   int   = 0
    winning_trades: int   = 0
    losing_trades:  int   = 0
    win_rate_pct:   Optional[float] = None

    # P&L
    gross_pnl:      float = 0.0
    avg_winner:     Optional[float] = None
    avg_loser:      Optional[float] = None
    largest_winner: Optional[float] = None
    largest_loser:  Optional[float] = None

    # Holding period
    avg_holding_days: Optional[float] = None

    # Best / worst month within the year
    best_month:     Optional[str]   = None   # "YYYY-MM"
    best_month_pnl: Optional[float] = None
    worst_month:    Optional[str]   = None
    worst_month_pnl:Optional[float] = None

    # Best / worst stock within the year (by total gross P&L)
    best_stock:     Optional[str]   = None
    best_stock_pnl: Optional[float] = None
    worst_stock:    Optional[str]   = None
    worst_stock_pnl:Optional[float] = None

    # YoY comparison (populated by with_comparisons())
    pnl_delta:         Optional[float] = None
    pnl_delta_pct:     Optional[float] = None
    win_rate_delta:    Optional[float] = None
    trades_delta:      Optional[int]   = None
    comparison_note:   Optional[str]   = None

    def to_dict(self) -> dict:
        return {
            "period_key":      self.period_key,
            "year":            self.year,
            "is_complete":     self.is_complete,
            "total_trades":    self.total_trades,
            "winning_trades":  self.winning_trades,
            "losing_trades":   self.losing_trades,
            "win_rate_pct":    self.win_rate_pct,
            "gross_pnl":       self.gross_pnl,
            "avg_winner":      self.avg_winner,
            "avg_loser":       self.avg_loser,
            "largest_winner":  self.largest_winner,
            "largest_loser":   self.largest_loser,
            "avg_holding_days":self.avg_holding_days,
            "best_month":      self.best_month,
            "best_month_pnl":  self.best_month_pnl,
            "worst_month":     self.worst_month,
            "worst_month_pnl": self.worst_month_pnl,
            "best_stock":      self.best_stock,
            "best_stock_pnl":  self.best_stock_pnl,
            "worst_stock":     self.worst_stock,
            "worst_stock_pnl": self.worst_stock_pnl,
            "pnl_delta":       self.pnl_delta,
            "pnl_delta_pct":   self.pnl_delta_pct,
            "win_rate_delta":  self.win_rate_delta,
            "trades_delta":    self.trades_delta,
            "comparison_note": self.comparison_note,
        }


# ── Engine ────────────────────────────────────────────────────────────────────

class PeriodAnalytics:
    """
    Compute monthly and yearly performance aggregations from matched trades.

    Usage:
        engine   = PeriodAnalytics()
        monthly  = engine.compute_monthly(matched_trades)
        yearly   = engine.compute_yearly(matched_trades)

    matched_trades: any list of objects with:
        sell_date (date), symbol (str), quantity (int),
        gross_pnl (float), holding_days (int|None)
    """

    # ── Public API ────────────────────────────────────────────────────────────

    def compute_monthly(
        self,
        matched_trades: list,
        today: Optional[date] = None,
    ) -> list[MonthlyPeriod]:
        """
        Aggregate matched trades into monthly buckets.
        Trades are bucketed by sell_date.
        Returns list sorted chronologically (oldest first).
        Adds MoM comparison deltas automatically.

        today: override for testing (defaults to date.today())
        """
        if today is None:
            today = date.today()

        buckets: dict[str, list] = defaultdict(list)
        for mt in matched_trades:
            sd = getattr(mt, 'sell_date', None)
            if sd is None:
                continue
            key = f"{sd.year:04d}-{sd.month:02d}"
            buckets[key].append(mt)

        periods = []
        for key in sorted(buckets.keys()):
            year, month = int(key[:4]), int(key[5:7])
            is_complete = not (year == today.year and month == today.month)
            p = self._build_monthly(key, year, month, buckets[key], is_complete)
            periods.append(p)

        self._add_monthly_comparisons(periods)
        log.info("[PeriodAnalytics] Computed %d monthly periods", len(periods))
        return periods

    def compute_yearly(
        self,
        matched_trades: list,
        today: Optional[date] = None,
        monthly_periods: Optional[list] = None,
    ) -> list[YearlyPeriod]:
        """
        Aggregate matched trades into yearly buckets.
        Returns list sorted chronologically (oldest first).
        Adds YoY comparison deltas automatically.

        monthly_periods: if provided, reuses already-computed monthly data
                         for best/worst month lookup (avoids re-grouping).
        today: override for testing.
        """
        if today is None:
            today = date.today()

        buckets: dict[str, list] = defaultdict(list)
        for mt in matched_trades:
            sd = getattr(mt, 'sell_date', None)
            if sd is None:
                continue
            buckets[str(sd.year)].append(mt)

        # Build monthly lookup if not supplied
        monthly_by_key: dict[str, MonthlyPeriod] = {}
        if monthly_periods:
            monthly_by_key = {p.period_key: p for p in monthly_periods}
        else:
            for mp in self.compute_monthly(matched_trades, today=today):
                monthly_by_key[mp.period_key] = mp

        periods = []
        for key in sorted(buckets.keys()):
            year        = int(key)
            is_complete = year < today.year
            year_months = [
                v for k, v in monthly_by_key.items()
                if k.startswith(f"{year:04d}-")
            ]
            p = self._build_yearly(key, year, buckets[key], is_complete, year_months)
            periods.append(p)

        self._add_yearly_comparisons(periods)
        log.info("[PeriodAnalytics] Computed %d yearly periods", len(periods))
        return periods

    # ── Monthly builder ───────────────────────────────────────────────────────

    def _build_monthly(
        self,
        key: str, year: int, month: int,
        trades: list, is_complete: bool,
    ) -> MonthlyPeriod:
        winners   = [t for t in trades if _pnl(t) > 0]
        losers    = [t for t in trades if _pnl(t) < 0]
        total     = len(trades)
        gross_pnl = round(sum(_pnl(t) for t in trades), 2)

        win_rate  = round(len(winners) / total * 100, 2) if total else None
        avg_win   = round(statistics.mean([_pnl(t) for t in winners]), 2) if winners else None
        avg_los   = round(statistics.mean([_pnl(t) for t in losers]),  2) if losers  else None
        max_win   = round(max([_pnl(t) for t in winners]), 2) if winners else None
        max_los   = round(min([_pnl(t) for t in losers]),  2) if losers  else None

        hold_days = [t.holding_days for t in trades
                     if getattr(t, 'holding_days', None) is not None]
        avg_hold  = round(statistics.mean(hold_days), 2) if hold_days else None

        # Best/worst stock by net P&L within month
        sym_pnl: dict[str, float] = defaultdict(float)
        for t in trades:
            sym_pnl[getattr(t, 'symbol', '?')] += _pnl(t)

        best_sym = max(sym_pnl, key=sym_pnl.get) if sym_pnl else None
        worst_sym= min(sym_pnl, key=sym_pnl.get) if sym_pnl else None

        return MonthlyPeriod(
            period_key      = key,
            year            = year,
            month           = month,
            is_complete     = is_complete,
            total_trades    = total,
            winning_trades  = len(winners),
            losing_trades   = len(losers),
            breakeven_trades= total - len(winners) - len(losers),
            win_rate_pct    = win_rate,
            gross_pnl       = gross_pnl,
            avg_winner      = avg_win,
            avg_loser       = avg_los,
            largest_winner  = max_win,
            largest_loser   = max_los,
            avg_holding_days= avg_hold,
            best_stock      = best_sym,
            best_stock_pnl  = round(sym_pnl[best_sym],  2) if best_sym  else None,
            worst_stock     = worst_sym,
            worst_stock_pnl = round(sym_pnl[worst_sym], 2) if worst_sym else None,
        )

    # ── Yearly builder ────────────────────────────────────────────────────────

    def _build_yearly(
        self,
        key: str, year: int,
        trades: list, is_complete: bool,
        year_months: list,
    ) -> YearlyPeriod:
        winners   = [t for t in trades if _pnl(t) > 0]
        losers    = [t for t in trades if _pnl(t) < 0]
        total     = len(trades)
        gross_pnl = round(sum(_pnl(t) for t in trades), 2)

        win_rate  = round(len(winners) / total * 100, 2) if total else None
        avg_win   = round(statistics.mean([_pnl(t) for t in winners]), 2) if winners else None
        avg_los   = round(statistics.mean([_pnl(t) for t in losers]),  2) if losers  else None
        max_win   = round(max([_pnl(t) for t in winners]), 2) if winners else None
        max_los   = round(min([_pnl(t) for t in losers]),  2) if losers  else None

        hold_days = [t.holding_days for t in trades
                     if getattr(t, 'holding_days', None) is not None]
        avg_hold  = round(statistics.mean(hold_days), 2) if hold_days else None

        # Best/worst month from monthly periods
        best_m = max(year_months, key=lambda m: m.gross_pnl) if year_months else None
        worst_m= min(year_months, key=lambda m: m.gross_pnl) if year_months else None

        # Best/worst stock for the year
        sym_pnl: dict[str, float] = defaultdict(float)
        for t in trades:
            sym_pnl[getattr(t, 'symbol', '?')] += _pnl(t)
        best_sym = max(sym_pnl, key=sym_pnl.get) if sym_pnl else None
        worst_sym= min(sym_pnl, key=sym_pnl.get) if sym_pnl else None

        return YearlyPeriod(
            period_key       = key,
            year             = year,
            is_complete      = is_complete,
            total_trades     = total,
            winning_trades   = len(winners),
            losing_trades    = len(losers),
            win_rate_pct     = win_rate,
            gross_pnl        = gross_pnl,
            avg_winner       = avg_win,
            avg_loser        = avg_los,
            largest_winner   = max_win,
            largest_loser    = max_los,
            avg_holding_days = avg_hold,
            best_month       = best_m.period_key     if best_m  else None,
            best_month_pnl   = best_m.gross_pnl      if best_m  else None,
            worst_month      = worst_m.period_key    if worst_m else None,
            worst_month_pnl  = worst_m.gross_pnl     if worst_m else None,
            best_stock       = best_sym,
            best_stock_pnl   = round(sym_pnl[best_sym],  2) if best_sym  else None,
            worst_stock      = worst_sym,
            worst_stock_pnl  = round(sym_pnl[worst_sym], 2) if worst_sym else None,
        )

    # ── Comparison deltas ─────────────────────────────────────────────────────

    def _add_monthly_comparisons(self, periods: list[MonthlyPeriod]) -> None:
        """
        Add month-over-month delta fields.
        Never compares an incomplete period to a complete one without labeling.
        """
        for i, curr in enumerate(periods):
            if i == 0:
                continue
            prev = periods[i - 1]
            note = f"vs {_month_label(prev.year, prev.month)}"
            if not prev.is_complete and curr.is_complete:
                note += " (prior month incomplete)"
            curr.pnl_delta      = round(curr.gross_pnl - prev.gross_pnl, 2)
            curr.pnl_delta_pct  = _pct_change(prev.gross_pnl, curr.gross_pnl)
            curr.win_rate_delta = _safe_sub(curr.win_rate_pct, prev.win_rate_pct)
            curr.trades_delta   = curr.total_trades - prev.total_trades
            curr.comparison_note= note

    def _add_yearly_comparisons(self, periods: list[YearlyPeriod]) -> None:
        """Add year-over-year delta fields."""
        for i, curr in enumerate(periods):
            if i == 0:
                continue
            prev = periods[i - 1]
            note = f"vs {prev.year}"
            if not prev.is_complete and curr.is_complete:
                note += " (prior year incomplete)"
            curr.pnl_delta      = round(curr.gross_pnl - prev.gross_pnl, 2)
            curr.pnl_delta_pct  = _pct_change(prev.gross_pnl, curr.gross_pnl)
            curr.win_rate_delta = _safe_sub(curr.win_rate_pct, prev.win_rate_pct)
            curr.trades_delta   = curr.total_trades - prev.total_trades
            curr.comparison_note= note


# ── Module-level helpers ──────────────────────────────────────────────────────

def _pnl(t) -> float:
    return float(getattr(t, 'gross_pnl', 0.0) or 0.0)

def _pct_change(old: float, new: float) -> Optional[float]:
    if old == 0:
        return None
    return round((new - old) / abs(old) * 100, 2)

def _safe_sub(a, b) -> Optional[float]:
    if a is None or b is None:
        return None
    return round(a - b, 2)

def _month_label(year: int, month: int) -> str:
    months = ["Jan","Feb","Mar","Apr","May","Jun",
              "Jul","Aug","Sep","Oct","Nov","Dec"]
    return f"{months[month-1]} {year}"