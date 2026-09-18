"""
app/services/holding_analyzer.py
──────────────────────────────────
Phase 4 — Holding period and trading behavior analysis.

Pure Python — no DB imports, no Flask dependency.
Consumes CompletedTradeSummary-like objects (same as TradeAnalyticsEngine).

BUCKETS (matching spec):
  0-1 day, 2-5 days, 6-10 days, 11-20 days, 21-30 days, 31-60 days, 60+ days

LANGUAGE RULES:
  All pattern observations use factual, non-prescriptive wording.
  No causal claims. No invented risk metrics.
"""

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


BUCKETS = [
    ("0-1",   0,  1),
    ("2-5",   2,  5),
    ("6-10",  6, 10),
    ("11-20",11, 20),
    ("21-30",21, 30),
    ("31-60",31, 60),
    ("60+",  61, 99999),
]


def _assign_bucket(days: int) -> str:
    for label, lo, hi in BUCKETS:
        if lo <= days <= hi:
            return label
    return "60+"


@dataclass
class HoldingBucket:
    label:     str
    min_days:  int
    max_days:  int
    count:     int   = 0
    wins:      int   = 0
    losses:    int   = 0
    breakeven: int   = 0
    win_rate:  Optional[float] = None
    total_pnl: float = 0.0
    avg_pnl:   Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "label":     self.label,
            "min_days":  self.min_days,
            "max_days":  self.max_days if self.max_days < 99999 else None,
            "count":     self.count,
            "wins":      self.wins,
            "losses":    self.losses,
            "breakeven": self.breakeven,
            "win_rate":  self.win_rate,
            "total_pnl": self.total_pnl,
            "avg_pnl":   self.avg_pnl,
        }


@dataclass
class HoldingComparison:
    winner_count:  int   = 0
    winner_avg:    Optional[float] = None
    winner_median: Optional[float] = None
    winner_min:    Optional[int]   = None
    winner_max:    Optional[int]   = None
    loser_count:   int   = 0
    loser_avg:     Optional[float] = None
    loser_median:  Optional[float] = None
    loser_min:     Optional[int]   = None
    loser_max:     Optional[int]   = None
    observation:   Optional[str]   = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class HoldingPattern:
    category:     str
    title:        str
    body:         str
    metric:       Optional[float] = None
    metric_label: Optional[str]   = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class HoldingAnalysis:
    buckets:           list = field(default_factory=list)
    comparison:        Optional[HoldingComparison] = None
    patterns:          list = field(default_factory=list)
    total_trades:      int  = 0
    trades_with_days:  int  = 0
    overall_avg_days:  Optional[float] = None
    overall_median:    Optional[float] = None
    overall_min:       Optional[int]   = None
    overall_max:       Optional[int]   = None

    def to_dict(self) -> dict:
        return {
            "total_trades":     self.total_trades,
            "trades_with_days": self.trades_with_days,
            "overall_avg_days": self.overall_avg_days,
            "overall_median":   self.overall_median,
            "overall_min":      self.overall_min,
            "overall_max":      self.overall_max,
            "buckets":          [b.to_dict() for b in self.buckets],
            "comparison":       self.comparison.to_dict() if self.comparison else None,
            "patterns":         [p.to_dict() for p in self.patterns],
        }


class HoldingAnalyzer:
    """
    Usage:
        analysis = HoldingAnalyzer().compute(summaries)

    summaries: objects with gross_pnl, holding_period_calendar_days, symbol
    """

    def compute(self, summaries: list) -> HoldingAnalysis:
        result = HoldingAnalysis(total_trades=len(summaries))
        with_days = [s for s in summaries
                     if getattr(s, 'holding_period_calendar_days', None) is not None]
        result.trades_with_days = len(with_days)
        if not with_days:
            return result

        days = [s.holding_period_calendar_days for s in with_days]
        result.overall_avg_days = round(sum(days) / len(days), 2)
        result.overall_median   = round(statistics.median(days), 2)
        result.overall_min      = min(days)
        result.overall_max      = max(days)
        result.buckets          = self._build_buckets(with_days)
        result.comparison       = self._build_comparison(with_days)
        result.patterns         = self._detect_patterns(with_days, result.buckets,
                                                         result.comparison, summaries)
        return result

    def _build_buckets(self, with_days: list) -> list:
        bmap = {lbl: HoldingBucket(label=lbl, min_days=lo, max_days=hi)
                for lbl, lo, hi in BUCKETS}
        for s in with_days:
            pnl = float(getattr(s, 'gross_pnl', 0.0) or 0.0)
            b   = bmap[_assign_bucket(s.holding_period_calendar_days)]
            b.count += 1; b.total_pnl += pnl
            if pnl > 0:   b.wins      += 1
            elif pnl < 0: b.losses    += 1
            else:          b.breakeven += 1
        for b in bmap.values():
            if b.count:
                b.win_rate  = round(b.wins / b.count * 100, 2)
                b.avg_pnl   = round(b.total_pnl / b.count, 2)
                b.total_pnl = round(b.total_pnl, 2)
        return [bmap[lbl] for lbl, _, _ in BUCKETS]

    def _build_comparison(self, with_days: list) -> HoldingComparison:
        winners = [s for s in with_days if float(getattr(s,'gross_pnl',0) or 0) > 0]
        losers  = [s for s in with_days if float(getattr(s,'gross_pnl',0) or 0) < 0]
        c = HoldingComparison()
        if winners:
            wd = [s.holding_period_calendar_days for s in winners]
            c.winner_count  = len(winners)
            c.winner_avg    = round(sum(wd)/len(wd), 2)
            c.winner_median = round(statistics.median(wd), 2)
            c.winner_min    = min(wd); c.winner_max = max(wd)
        if losers:
            ld = [s.holding_period_calendar_days for s in losers]
            c.loser_count   = len(losers)
            c.loser_avg     = round(sum(ld)/len(ld), 2)
            c.loser_median  = round(statistics.median(ld), 2)
            c.loser_min     = min(ld); c.loser_max = max(ld)
        if c.winner_avg is not None and c.loser_avg is not None:
            if c.loser_avg > c.winner_avg * 1.5:
                c.observation = (
                    f"Your average losing trade is held {c.loser_avg} days "
                    f"while your average winning trade is held {c.winner_avg} days.")
            elif c.winner_avg >= c.loser_avg:
                c.observation = (
                    f"Your average winning trade is held {c.winner_avg} days "
                    f"vs {c.loser_avg} days for losing trades.")
            else:
                c.observation = (
                    f"Average holding: winners {c.winner_avg}d, losers {c.loser_avg}d.")
        return c

    def _detect_patterns(self, with_days, buckets, comp, all_trades) -> list:
        patterns = []
        active = [b for b in buckets if b.count >= 2]

        # Best bucket by win rate
        if active:
            best = max(active, key=lambda b: b.win_rate or 0)
            if best.win_rate and best.win_rate >= 55:
                patterns.append(HoldingPattern(
                    category="best_bucket",
                    title=f"Trades held {best.label} days show highest win rate",
                    body=(f"Potential pattern: trades held {best.label} calendar days "
                          f"have a historical win rate of {best.win_rate}% "
                          f"({best.wins}/{best.count} trades). "
                          f"Historical data shows this range has outperformed "
                          f"other holding periods in your trade history."),
                    metric=best.win_rate,
                    metric_label=f"win rate ({best.label}d)",
                ))

        # Worst bucket by total P&L
        if active:
            worst = min(active, key=lambda b: b.total_pnl)
            if worst.total_pnl < 0:
                patterns.append(HoldingPattern(
                    category="worst_bucket",
                    title=f"Trades held {worst.label} days show largest cumulative loss",
                    body=(f"Trades held {worst.label} calendar days produced "
                          f"₹{worst.total_pnl:,.2f} across {worst.count} trades. "
                          f"Consider reviewing whether this range "
                          f"shares common entry characteristics."),
                    metric=worst.total_pnl,
                    metric_label=f"total P&L ({worst.label}d)",
                ))

        # Long-held losers
        losers = [s for s in with_days if float(getattr(s,'gross_pnl',0) or 0) < 0]
        if losers:
            long_l = [s for s in losers if s.holding_period_calendar_days > 20]
            pct = round(len(long_l) / len(losers) * 100, 1)
            if pct >= 30:
                avg_pnl = round(sum(float(s.gross_pnl) for s in long_l)/len(long_l), 2)
                patterns.append(HoldingPattern(
                    category="long_loser",
                    title="Losing trades frequently held beyond 20 days",
                    body=(f"{pct}% of losing trades were held more than 20 days "
                          f"(avg P&L: ₹{avg_pnl:,.2f}). "
                          f"Historical data shows these longer-held losses "
                          f"tend to be larger in magnitude."),
                    metric=pct,
                    metric_label="% losers held >20d",
                ))

        # Short-hold dominance
        total = len(with_days)
        short = [s for s in with_days if s.holding_period_calendar_days <= 2]
        if total >= 5 and len(short) / total >= 0.35:
            short_wr = round(
                sum(1 for s in short if float(getattr(s,'gross_pnl',0) or 0) > 0)
                / len(short) * 100, 1) if short else 0
            patterns.append(HoldingPattern(
                category="short_hold",
                title="High proportion of trades closed within 2 days",
                body=(f"{round(len(short)/total*100,1)}% of trades closed within "
                      f"2 days (win rate for these: {short_wr}%). "
                      f"Potential pattern: short-duration trades represent "
                      f"a significant portion of your activity."),
                metric=round(len(short)/total*100, 1),
                metric_label="% trades ≤2d",
            ))

        # Repeated symbol losses
        sym_loss: dict = defaultdict(int)
        for s in all_trades:
            if float(getattr(s,'gross_pnl',0) or 0) < 0:
                sym_loss[str(getattr(s,'symbol','?')).upper().strip()] += 1
        repeated = {k: v for k, v in sym_loss.items() if v >= 2}
        if repeated:
            top = max(repeated, key=repeated.get)
            patterns.append(HoldingPattern(
                category="repeated_loss",
                title="Repeated losses in same symbol(s)",
                body=(f"{len(repeated)} symbol(s) appear in multiple losing trades. "
                      f"Potential pattern: {top} has {repeated[top]} separate "
                      f"losing trades in this period."),
                metric=repeated[top],
                metric_label=f"losing trades in {top}",
            ))

        return patterns