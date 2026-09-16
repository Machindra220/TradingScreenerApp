"""
app/services/insights_engine.py
─────────────────────────────────
Phase 9 — Deterministic trading insights engine.

Analyzes historical trade data and produces categorized observations.
NO AI, NO external APIs. Every insight is derived from a deterministic
rule applied to computed statistics.

LANGUAGE RULES:
  - Neutral, factual, past-tense observations.
  - Never prescriptive ("you must", "you should").
  - Preferred phrases: "Your historical data suggests...",
    "Potential pattern...", "Consider reviewing..."
  - Positive patterns use affirming language without exaggeration.

SEVERITY LEVELS:
  info     — neutral observation, no action implied
  positive — statistically favorable pattern observed
  warning  — pattern worth reviewing; not necessarily bad
  alert    — strong pattern that frequently correlates with poor outcomes

INPUT:
  summaries:  list[CompletedTradeSummary]  (from TradeAnalyticsEngine)
  report:     AnalyticsReport              (from TradeAnalyticsEngine)
  monthly:    list[MonthlyPeriod]          (from PeriodAnalytics, optional)

OUTPUT:
  InsightsReport with categorized Insight objects.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

log = logging.getLogger(__name__)

# Minimum trades required to generate insights (avoid noise from tiny samples)
_MIN_TRADES = 5


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class Insight:
    """
    A single deterministic observation from trade history.

    severity:  'info' | 'positive' | 'warning' | 'alert'
    category:  rule group that generated this insight
    title:     short label (used as card heading)
    body:      full neutral-language explanation with stats
    metric:    the primary numeric value driving this insight (for UI display)
    metric_label: human-readable unit/label for the metric
    supporting_stat: optional secondary stat shown alongside
    """
    severity:         str
    category:         str
    title:            str
    body:             str
    metric:           Optional[float] = None
    metric_label:     Optional[str]   = None
    supporting_stat:  Optional[str]   = None

    def to_dict(self) -> dict:
        return {
            "severity":        self.severity,
            "category":        self.category,
            "title":           self.title,
            "body":            self.body,
            "metric":          self.metric,
            "metric_label":    self.metric_label,
            "supporting_stat": self.supporting_stat,
        }


@dataclass
class InsightsReport:
    """
    Categorized collection of insights.
    Split into positive_patterns, potential_issues, and areas_to_review
    matching the UI sections in the spec.
    """
    positive_patterns: list[Insight] = field(default_factory=list)
    potential_issues:  list[Insight] = field(default_factory=list)
    areas_to_review:   list[Insight] = field(default_factory=list)
    info_observations: list[Insight] = field(default_factory=list)

    insufficient_data: bool = False
    trade_count:       int  = 0
    min_trades_needed: int  = _MIN_TRADES

    def all_insights(self) -> list[Insight]:
        return (self.positive_patterns + self.potential_issues +
                self.areas_to_review + self.info_observations)

    def to_dict(self) -> dict:
        return {
            "insufficient_data": self.insufficient_data,
            "trade_count":       self.trade_count,
            "min_trades_needed": self.min_trades_needed,
            "positive_patterns": [i.to_dict() for i in self.positive_patterns],
            "potential_issues":  [i.to_dict() for i in self.potential_issues],
            "areas_to_review":   [i.to_dict() for i in self.areas_to_review],
            "info_observations": [i.to_dict() for i in self.info_observations],
            "total_insights":    len(self.all_insights()),
        }


# ── Engine ────────────────────────────────────────────────────────────────────

class InsightsEngine:
    """
    Deterministic insights engine.

    Usage:
        engine = InsightsEngine()
        report = engine.compute(summaries, analytics_report, monthly_periods)

    All rule methods follow the pattern:
        def _rule_<name>(self, ...) -> list[Insight]
    Each returns zero or more Insight objects based on computed stats.
    """

    def compute(
        self,
        summaries: list,
        report=None,
        monthly: list | None = None,
    ) -> InsightsReport:
        """
        Run all insight rules and return categorized InsightsReport.

        summaries: list of CompletedTradeSummary-like objects
        report:    AnalyticsReport-like object (optional, pre-computed)
        monthly:   list of MonthlyPeriod-like objects (optional)
        """
        result = InsightsReport(trade_count=len(summaries))

        if len(summaries) < _MIN_TRADES:
            result.insufficient_data = True
            log.info(
                "[InsightsEngine] Only %d trades — need %d for insights",
                len(summaries), _MIN_TRADES
            )
            return result

        # Collect all insights from all rules
        all_insights: list[Insight] = []
        for rule_fn in [
            self._rule_holding_behavior,
            self._rule_winners_vs_losers,
            self._rule_stock_concentration,
            self._rule_frequent_trading,
            self._rule_short_holding,
            self._rule_long_held_losers,
            self._rule_large_losses,
            self._rule_consecutive_losses,
            self._rule_monthly_consistency,
            self._rule_profit_concentration,
        ]:
            try:
                all_insights.extend(rule_fn(summaries, report, monthly or []))
            except Exception as e:
                log.warning("[InsightsEngine] Rule %s failed: %s", rule_fn.__name__, e)

        # Route into buckets by severity
        for ins in all_insights:
            if ins.severity == "positive":
                result.positive_patterns.append(ins)
            elif ins.severity == "alert":
                result.potential_issues.append(ins)
            elif ins.severity == "warning":
                result.areas_to_review.append(ins)
            else:
                result.info_observations.append(ins)

        log.info(
            "[InsightsEngine] Generated %d insights: %d positive, %d issues, %d review",
            len(all_insights),
            len(result.positive_patterns),
            len(result.potential_issues),
            len(result.areas_to_review),
        )
        return result

    # ── Rule 1: Holding behavior ──────────────────────────────────────────────

    def _rule_holding_behavior(self, summaries, report, monthly) -> list[Insight]:
        winners = [s for s in summaries if _pnl(s) > 0]
        losers  = [s for s in summaries if _pnl(s) < 0]

        w_days = [s.holding_period_calendar_days for s in winners
                  if s.holding_period_calendar_days is not None]
        l_days = [s.holding_period_calendar_days for s in losers
                  if s.holding_period_calendar_days is not None]

        if not w_days or not l_days:
            return []

        avg_w = round(sum(w_days) / len(w_days), 1)
        avg_l = round(sum(l_days) / len(l_days), 1)
        insights = []

        if avg_l > avg_w * 1.5:
            insights.append(Insight(
                severity     = "warning",
                category     = "holding_behavior",
                title        = "Losing trades held longer than winners",
                body         = (
                    f"Your average losing trade is held {avg_l} days while your "
                    f"average winning trade is held {avg_w} days. "
                    f"Historically, holding losses longer than gains can amplify "
                    f"drawdowns. Your historical data suggests reviewing exit "
                    f"criteria for underperforming positions."
                ),
                metric       = avg_l,
                metric_label = "avg losing hold (days)",
                supporting_stat = f"avg winning hold: {avg_w}d",
            ))
        elif avg_w >= avg_l:
            insights.append(Insight(
                severity     = "positive",
                category     = "holding_behavior",
                title        = "Winners held at least as long as losers",
                body         = (
                    f"Your average winning trade is held {avg_w} days vs "
                    f"{avg_l} days for losing trades. "
                    f"Potential pattern: your historical data suggests you tend "
                    f"to let winners run longer than you hold losses."
                ),
                metric       = avg_w,
                metric_label = "avg winning hold (days)",
                supporting_stat = f"avg losing hold: {avg_l}d",
            ))

        return insights

    # ── Rule 2: Winners vs losers ratio ──────────────────────────────────────

    def _rule_winners_vs_losers(self, summaries, report, monthly) -> list[Insight]:
        if not report:
            return []
        insights = []

        avg_w = getattr(report, 'avg_winner', None)
        avg_l = getattr(report, 'avg_loser',  None)
        wr    = getattr(report, 'win_rate_pct', None)

        if avg_w and avg_l and avg_l != 0:
            ratio = round(avg_w / abs(avg_l), 2)
            if ratio >= 1.5:
                insights.append(Insight(
                    severity     = "positive",
                    category     = "winners_vs_losers",
                    title        = "Average winner substantially larger than average loser",
                    body         = (
                        f"Your average winning trade (₹{avg_w:,.0f}) is "
                        f"{ratio}× your average losing trade (₹{abs(avg_l):,.0f}). "
                        f"A ratio above 1.0 means each win recovers more than each loss."
                    ),
                    metric       = ratio,
                    metric_label = "win/loss ratio",
                    supporting_stat = f"avg winner: ₹{avg_w:,.0f}  avg loser: ₹{abs(avg_l):,.0f}",
                ))
            elif ratio < 0.8:
                insights.append(Insight(
                    severity     = "warning",
                    category     = "winners_vs_losers",
                    title        = "Average loser larger than average winner",
                    body         = (
                        f"Your average winning trade (₹{avg_w:,.0f}) is "
                        f"{ratio}× your average losing trade (₹{abs(avg_l):,.0f}). "
                        f"Potential pattern: losses tend to be larger than gains. "
                        f"Consider reviewing whether position sizing or exit timing "
                        f"differs between profitable and unprofitable trades."
                    ),
                    metric       = ratio,
                    metric_label = "win/loss ratio",
                ))

        # Win rate with low ratio warning
        if wr is not None and wr < 40 and avg_w and avg_l:
            ratio = round(avg_w / abs(avg_l), 2) if avg_l else None
            if ratio and ratio < 1.5:
                insights.append(Insight(
                    severity     = "alert",
                    category     = "winners_vs_losers",
                    title        = "Low win rate with unfavorable win/loss ratio",
                    body         = (
                        f"Win rate is {wr}% with a win/loss ratio of {ratio}. "
                        f"Your historical data suggests that both the frequency "
                        f"and magnitude of wins are below losses. "
                        f"Areas to review: trade selection criteria and exit rules."
                    ),
                    metric       = wr,
                    metric_label = "win rate %",
                    supporting_stat = f"win/loss ratio: {ratio}",
                ))

        return insights

    # ── Rule 3: Stock concentration ───────────────────────────────────────────

    def _rule_stock_concentration(self, summaries, report, monthly) -> list[Insight]:
        sym_count: dict[str, int] = defaultdict(int)
        for s in summaries:
            sym_count[_sym(s)] += 1

        total  = len(summaries)
        ranked = sorted(sym_count.items(), key=lambda x: x[1], reverse=True)
        top3   = ranked[:3]
        top3_count = sum(c for _, c in top3)
        top3_pct   = round(top3_count / total * 100, 1)

        insights = []
        if top3_pct >= 60:
            names = ", ".join(f"{s}({c})" for s, c in top3)
            insights.append(Insight(
                severity     = "warning",
                category     = "stock_concentration",
                title        = "High trade concentration in few symbols",
                body         = (
                    f"{top3_pct}% of your completed trades are in just "
                    f"{len(top3)} symbols: {names}. "
                    f"Potential pattern: high repetition in specific stocks. "
                    f"Consider reviewing whether this reflects conviction or "
                    f"availability bias."
                ),
                metric       = top3_pct,
                metric_label = "% trades in top 3 symbols",
                supporting_stat = names,
            ))
        elif len(sym_count) >= 10:
            insights.append(Insight(
                severity     = "positive",
                category     = "stock_concentration",
                title        = "Trades spread across multiple symbols",
                body         = (
                    f"Completed trades span {len(sym_count)} distinct symbols. "
                    f"Your historical data shows diversification across stocks."
                ),
                metric       = len(sym_count),
                metric_label = "distinct symbols traded",
            ))

        return insights

    # ── Rule 4: Frequent trading ──────────────────────────────────────────────

    def _rule_frequent_trading(self, summaries, report, monthly) -> list[Insight]:
        if not monthly:
            return []

        complete = [m for m in monthly if m.is_complete]
        if not complete:
            return []

        avg_per_month = round(
            sum(m.total_trades for m in complete) / len(complete), 1
        )
        max_month = max(complete, key=lambda m: m.total_trades)
        insights  = []

        if avg_per_month > 20:
            insights.append(Insight(
                severity     = "warning",
                category     = "frequent_trading",
                title        = "High average monthly trade frequency",
                body         = (
                    f"Your average is {avg_per_month} completed trades per month "
                    f"(peak: {max_month.total_trades} in {max_month.period_key}). "
                    f"Potential pattern: high frequency can increase transaction "
                    f"costs and reduce per-trade selectivity. "
                    f"Your historical data may reveal whether higher-frequency "
                    f"months correlate with better or worse returns."
                ),
                metric       = avg_per_month,
                metric_label = "avg trades/month",
                supporting_stat = f"peak: {max_month.total_trades} in {max_month.period_key}",
            ))

        # Check if peak months have lower win rates
        if len(complete) >= 3:
            high_freq = sorted(complete, key=lambda m: m.total_trades, reverse=True)
            top_third = high_freq[:max(1, len(high_freq)//3)]
            wrs = [m.win_rate_pct for m in top_third if m.win_rate_pct is not None]
            if wrs:
                avg_wr_high = round(sum(wrs) / len(wrs), 1)
                overall_wr  = getattr(report, 'win_rate_pct', None)
                if overall_wr and avg_wr_high < overall_wr - 10:
                    insights.append(Insight(
                        severity     = "warning",
                        category     = "frequent_trading",
                        title        = "Higher-frequency months show lower win rate",
                        body         = (
                            f"In your highest-volume months, the average win rate "
                            f"is {avg_wr_high}% vs overall {overall_wr}%. "
                            f"Potential pattern: trade quality may decrease when "
                            f"trading frequency increases."
                        ),
                        metric       = avg_wr_high,
                        metric_label = "win rate in high-frequency months",
                        supporting_stat = f"overall win rate: {overall_wr}%",
                    ))

        return insights

    # ── Rule 5: Very short holding periods ────────────────────────────────────

    def _rule_short_holding(self, summaries, report, monthly) -> list[Insight]:
        with_days = [s for s in summaries
                     if s.holding_period_calendar_days is not None]
        if not with_days:
            return []

        very_short = [s for s in with_days if s.holding_period_calendar_days <= 2]
        short_pct  = round(len(very_short) / len(with_days) * 100, 1)
        insights   = []

        if short_pct >= 30:
            short_winners = [s for s in very_short if _pnl(s) > 0]
            short_wr      = round(len(short_winners) / len(very_short) * 100, 1)
            insights.append(Insight(
                severity     = "info",
                category     = "short_holding",
                title        = "Significant portion of trades closed within 2 days",
                body         = (
                    f"{short_pct}% of your completed trades are closed within "
                    f"2 calendar days (win rate for these: {short_wr}%). "
                    f"Your historical data suggests a pattern of short-duration "
                    f"trading. Consider whether this aligns with your intended "
                    f"strategy."
                ),
                metric       = short_pct,
                metric_label = "% trades ≤2 days",
                supporting_stat = f"win rate (≤2d): {short_wr}%",
            ))

        # Find best holding-period bucket
        buckets = {"1-5": [], "6-15": [], "16-30": [], "31+": []}
        for s in with_days:
            d = s.holding_period_calendar_days
            if d <= 5:      buckets["1-5"].append(s)
            elif d <= 15:   buckets["6-15"].append(s)
            elif d <= 30:   buckets["16-30"].append(s)
            else:           buckets["31+"].append(s)

        best_bucket = None
        best_wr     = -1
        for label, group in buckets.items():
            if len(group) < 3:
                continue
            wr = len([s for s in group if _pnl(s) > 0]) / len(group) * 100
            if wr > best_wr:
                best_wr, best_bucket = wr, (label, len(group), round(wr, 1))

        if best_bucket and best_bucket[2] >= 55:
            label, cnt, wr = best_bucket
            insights.append(Insight(
                severity     = "positive",
                category     = "short_holding",
                title        = f"Trades held {label} days show highest win rate",
                body         = (
                    f"Trades held {label} calendar days have a historical win rate "
                    f"of {wr}% ({cnt} trades). "
                    f"Potential pattern: this holding window has historically "
                    f"performed better than other durations in your trade history."
                ),
                metric       = wr,
                metric_label = f"win rate ({label} days)",
                supporting_stat = f"{cnt} trades in this range",
            ))

        return insights

    # ── Rule 6: Long-held losing trades ──────────────────────────────────────

    def _rule_long_held_losers(self, summaries, report, monthly) -> list[Insight]:
        losers = [s for s in summaries if _pnl(s) < 0
                  and s.holding_period_calendar_days is not None]
        if len(losers) < 3:
            return []

        long_losers = [s for s in losers if s.holding_period_calendar_days > 20]
        pct         = round(len(long_losers) / len(losers) * 100, 1)
        insights    = []

        if pct >= 35:
            avg_pnl = round(sum(_pnl(s) for s in long_losers) / len(long_losers), 2)
            insights.append(Insight(
                severity     = "warning",
                category     = "long_held_losers",
                title        = "Many losing trades held longer than 20 days",
                body         = (
                    f"{pct}% of losing trades lasted more than 20 days "
                    f"(avg P&L: ₹{avg_pnl:,.0f}). "
                    f"Potential pattern: these longer-held losses tend to be larger. "
                    f"Your historical data suggests reviewing whether extended "
                    f"holding periods improved or worsened outcomes for losing trades."
                ),
                metric       = pct,
                metric_label = "% losers held >20 days",
                supporting_stat = f"avg P&L of long losers: ₹{avg_pnl:,.0f}",
            ))

        return insights

    # ── Rule 7: Large losses ──────────────────────────────────────────────────

    def _rule_large_losses(self, summaries, report, monthly) -> list[Insight]:
        losers = [s for s in summaries if _pnl(s) < 0]
        if len(losers) < 3:
            return []

        avg_loss   = sum(_pnl(s) for s in losers) / len(losers)   # negative
        threshold  = avg_loss * 2                                   # 2× avg loss
        large_l    = [s for s in losers if _pnl(s) < threshold]
        pct        = round(len(large_l) / len(losers) * 100, 1)
        insights   = []

        if pct >= 20:
            avg_large = round(sum(_pnl(s) for s in large_l) / len(large_l), 2)
            syms      = list({_sym(s) for s in large_l})[:5]
            insights.append(Insight(
                severity     = "alert",
                category     = "large_losses",
                title        = "Outsized losses detected",
                body         = (
                    f"{pct}% of losing trades exceed twice the average loss "
                    f"(avg: ₹{avg_large:,.0f}). "
                    f"These outlier losses occur in: {', '.join(syms)}. "
                    f"Potential pattern: a small number of trades account for "
                    f"disproportionate drawdown. Consider reviewing these trades "
                    f"for common characteristics."
                ),
                metric       = pct,
                metric_label = "% losses >2× avg loss",
                supporting_stat = f"avg outsized loss: ₹{avg_large:,.0f}",
            ))

        return insights

    # ── Rule 8: Consecutive losses ────────────────────────────────────────────

    def _rule_consecutive_losses(self, summaries, report, monthly) -> list[Insight]:
        # Sort by sell_date to get chronological order
        dated = sorted(
            [s for s in summaries if s.sell_date is not None],
            key=lambda s: s.sell_date
        )
        if len(dated) < 5:
            return []

        max_streak = 0
        current    = 0
        for s in dated:
            if _pnl(s) < 0:
                current   += 1
                max_streak = max(max_streak, current)
            else:
                current = 0

        insights = []
        if max_streak >= 5:
            insights.append(Insight(
                severity     = "alert",
                category     = "consecutive_losses",
                title        = f"Maximum losing streak: {max_streak} consecutive trades",
                body         = (
                    f"Your longest consecutive losing streak in this period is "
                    f"{max_streak} trades. "
                    f"Potential pattern: extended streaks can indicate a "
                    f"market-condition mismatch. Your historical data may "
                    f"reveal whether these clusters occur in specific periods "
                    f"or market conditions."
                ),
                metric       = max_streak,
                metric_label = "max consecutive losses",
            ))
        elif max_streak <= 2 and len(dated) >= 10:
            insights.append(Insight(
                severity     = "positive",
                category     = "consecutive_losses",
                title        = "No extended losing streaks observed",
                body         = (
                    f"The longest consecutive losing streak is {max_streak} trade(s). "
                    f"Your historical data shows no extended loss clustering."
                ),
                metric       = max_streak,
                metric_label = "max consecutive losses",
            ))

        return insights

    # ── Rule 9: Monthly consistency ────────────────────────────────────────────

    def _rule_monthly_consistency(self, summaries, report, monthly) -> list[Insight]:
        complete = [m for m in monthly if m.is_complete] if monthly else []
        if len(complete) < 3:
            return []

        profitable    = [m for m in complete if m.gross_pnl > 0]
        profitable_pct= round(len(profitable) / len(complete) * 100, 1)
        insights      = []

        if profitable_pct >= 70:
            insights.append(Insight(
                severity     = "positive",
                category     = "monthly_consistency",
                title        = "High monthly profitability consistency",
                body         = (
                    f"{profitable_pct}% of complete months are profitable "
                    f"({len(profitable)} of {len(complete)} months). "
                    f"Your historical data suggests consistent monthly performance."
                ),
                metric       = profitable_pct,
                metric_label = "% profitable months",
                supporting_stat = f"{len(profitable)} of {len(complete)} months",
            ))
        elif profitable_pct < 50:
            insights.append(Insight(
                severity     = "warning",
                category     = "monthly_consistency",
                title        = "Fewer than half of months are profitable",
                body         = (
                    f"{profitable_pct}% of complete months show a net profit "
                    f"({len(profitable)} of {len(complete)} months). "
                    f"Potential pattern: P&L consistency across months "
                    f"is below 50%. Areas to review: whether losing months "
                    f"share common market conditions or trade types."
                ),
                metric       = profitable_pct,
                metric_label = "% profitable months",
            ))

        return insights

    # ── Rule 10: Profit concentration ─────────────────────────────────────────

    def _rule_profit_concentration(self, summaries, report, monthly) -> list[Insight]:
        sym_pnl: dict[str, float] = defaultdict(float)
        for s in summaries:
            sym_pnl[_sym(s)] += _pnl(s)

        total_pnl = sum(sym_pnl.values())
        if total_pnl == 0 or len(sym_pnl) < 4:
            return []

        winners_by_sym = {k: v for k, v in sym_pnl.items() if v > 0}
        if not winners_by_sym:
            return []

        top3_syms = sorted(winners_by_sym, key=winners_by_sym.get, reverse=True)[:3]
        top3_pnl  = sum(winners_by_sym[s] for s in top3_syms)
        pct       = round(top3_pnl / total_pnl * 100, 1) if total_pnl > 0 else 0
        insights  = []

        if pct >= 80:
            names = ", ".join(
                f"{s} (₹{winners_by_sym[s]:,.0f})" for s in top3_syms
            )
            insights.append(Insight(
                severity     = "info",
                category     = "profit_concentration",
                title        = "Gross profit concentrated in few symbols",
                body         = (
                    f"{pct}% of gross profit comes from just {len(top3_syms)} symbols: "
                    f"{names}. "
                    f"Potential pattern: overall profitability is heavily influenced "
                    f"by a small number of trades. "
                    f"Your historical data suggests reviewing whether this "
                    f"concentration is intentional."
                ),
                metric       = pct,
                metric_label = "% profit from top 3 symbols",
                supporting_stat = names,
            ))

        return insights


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pnl(s) -> float:
    return float(getattr(s, 'gross_pnl', 0.0) or 0.0)

def _sym(s) -> str:
    return str(getattr(s, 'symbol', '?') or '?').upper().strip()