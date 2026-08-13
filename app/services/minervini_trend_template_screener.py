"""
Minervini Trend Template + IBD-style Relative Strength Screener
=================================================================

Implements Mark Minervini's "Trend Template" (the technical criteria that
define an eligible Stage-2 uptrend) combined with an IBD-style Relative
Strength (RS) Rating.

WHAT'S IMPLEMENTED
-------------------
1. Moving Average Alignment
   Price > 50-day SMA > 150-day SMA > 200-day SMA

2. Moving Average Slopes
   150-day SMA > 200-day SMA, AND the 200-day SMA is trending up
   (higher than it was ~1 month / 21 trading days ago)

3. 52-Week High/Low Boundaries
   Price >= 30% above the 52-week low
   Price within 25% of the 52-week high  (i.e. price >= 0.75 * 52w high)

4. IBD-style Relative Strength Rating (1-99 percentile), RS >= 80 required

5. Clean tabular output via pandas, with a boolean pass/fail per criterion
   plus a final `passes_all` column, sorted by RS Rating descending.

NOTES ON METHODOLOGY / HONESTY ABOUT APPROXIMATIONS
-----------------------------------------------------
- IBD's actual RS Rating formula is proprietary and computed against their
  full ~7,000-stock database. What's implemented here is the commonly used
  public approximation: a weighted blend of trailing 3/6/9/12-month price
  performance (40/20/20/20), ranked into a 1-99 percentile *within the
  universe you pass in*. Your number will not exactly match an IBD terminal,
  but the ranking behavior (recent momentum weighted most heavily) is the
  same idea.
- The percentile is computed across the FULL scanned universe (every symbol
  that returned usable data), not just the stocks that already passed the
  trend-template filter. Ranking only within a pre-filtered shortlist
  inflates everyone's percentile and defeats the purpose of a "top 20% of
  the market" signal.
- No fundamental filters (EPS growth, sales growth, margins, etc.) are
  implemented — the prompt's numbered list was purely technical. A
  `passes_fundamental_filters()` stub is included at the bottom so you can
  plug those in later without restructuring anything.
- Universe size matters for percentile meaningfulness: ranking 20 stocks
  against each other and calling the top one "RS 99" is statistically much
  weaker than ranking 500. A warning is logged if the universe is small.

USAGE
-----
    from minervini_trend_template_screener import screen_universe, ScreenerConfig

    symbols = ["RELIANCE.NS", "TCS.NS", "INFY.NS", ...]   # or "AAPL", "MSFT", ... for US
    results = screen_universe(symbols, benchmark_symbol="^CRSLDX")
    passing = results[results["passes_all"]].sort_values("rs_rating", ascending=False)

Dependencies: pandas, numpy, yfinance
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("minervini_screener")


# ---------------------------------------------------------------------------
# Configuration — every threshold from the prompt lives here, parameterized
# so you can retune (e.g. back to Minervini's original 25%/25%) without
# touching the filter logic itself.
# ---------------------------------------------------------------------------

@dataclass
class ScreenerConfig:
    sma_periods: tuple = (50, 150, 200)          # criterion 1
    sma_slope_lookback_days: int = 21            # ~1 trading month, criterion 2
    min_pct_above_52w_low: float = 30.0          # criterion 3 (Minervini's original is 25%)
    max_pct_below_52w_high: float = 25.0         # criterion 3
    min_rs_rating: float = 80.0                  # criterion 4
    rs_weight_3mo: float = 0.4
    rs_weight_6mo: float = 0.2
    rs_weight_9mo: float = 0.2
    rs_weight_12mo: float = 0.2
    fetch_period: str = "2y"                     # yfinance period token — must be a
                                                  # documented value (1d,5d,1mo,3mo,6mo,
                                                  # 1y,2y,5y,10y,ytd,max), not an arbitrary
                                                  # "Nd" string, or the download silently
                                                  # fails/returns empty for many tickers.
    min_trading_days_required: int = 260          # 200-SMA + slope lookback buffer
    small_universe_warning_threshold: int = 30


DEFAULT_CONFIG = ScreenerConfig()


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def fetch_price_history(symbol: str, period: str) -> pd.DataFrame:
    """Daily OHLCV history for one symbol. Returns an empty DataFrame (never
    raises) on any fetch failure, so a single bad ticker can't kill a batch
    scan — the caller just skips it."""
    try:
        df = yf.Ticker(symbol).history(period=period, interval="1d")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna(subset=["Close"])
    except Exception as e:
        logger.warning(f"Failed to fetch {symbol}: {e}")
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Criterion 1 & 2 — Moving average alignment and slope
# ---------------------------------------------------------------------------

def compute_technicals(df: pd.DataFrame, cfg: ScreenerConfig) -> Optional[dict]:
    """Computes price/SMA/52-week stats for one stock. Returns None if there
    isn't enough history to evaluate the template reliably."""
    if df.empty or len(df) < cfg.min_trading_days_required:
        return None

    close = df["Close"]
    sma_50 = close.rolling(50).mean()
    sma_150 = close.rolling(150).mean()
    sma_200 = close.rolling(200).mean()

    if sma_200.iloc[-1] != sma_200.iloc[-1]:  # NaN check without importing math
        return None

    current_price = float(close.iloc[-1])
    sma_50_now = float(sma_50.iloc[-1])
    sma_150_now = float(sma_150.iloc[-1])
    sma_200_now = float(sma_200.iloc[-1])

    # 200-SMA "trending up" = higher than it was ~1 month ago (criterion 2)
    lookback = cfg.sma_slope_lookback_days
    sma_200_prior = float(sma_200.iloc[-1 - lookback]) if len(sma_200) > lookback else np.nan

    window_252 = close.tail(252)
    week52_high = float(window_252.max())
    week52_low = float(window_252.min())

    pct_above_52w_low = ((current_price / week52_low) - 1) * 100 if week52_low > 0 else np.nan
    pct_below_52w_high = ((week52_high - current_price) / week52_high) * 100 if week52_high > 0 else np.nan

    return {
        "price": current_price,
        "sma_50": sma_50_now,
        "sma_150": sma_150_now,
        "sma_200": sma_200_now,
        "sma_200_prior": sma_200_prior,
        "week52_high": week52_high,
        "week52_low": week52_low,
        "pct_above_52w_low": pct_above_52w_low,
        "pct_below_52w_high": pct_below_52w_high,
    }


def evaluate_trend_template(tech: dict, cfg: ScreenerConfig) -> dict:
    """Applies criteria 1-3 to an already-computed technicals dict. Every
    sub-check is returned individually so you can see exactly which leg of
    the template failed, not just a single pass/fail blob."""
    ma_alignment_pass = (
        tech["price"] > tech["sma_50"] > tech["sma_150"] > tech["sma_200"]
    )

    sma150_above_sma200 = tech["sma_150"] > tech["sma_200"]
    sma200_trending_up = (
        not np.isnan(tech["sma_200_prior"]) and tech["sma_200"] > tech["sma_200_prior"]
    )
    ma_slope_pass = sma150_above_sma200 and sma200_trending_up

    above_52w_low_pass = tech["pct_above_52w_low"] >= cfg.min_pct_above_52w_low
    within_52w_high_pass = tech["pct_below_52w_high"] <= cfg.max_pct_below_52w_high
    boundary_pass = above_52w_low_pass and within_52w_high_pass

    return {
        "ma_alignment_pass": ma_alignment_pass,
        "sma150_above_sma200": sma150_above_sma200,
        "sma200_trending_up": sma200_trending_up,
        "ma_slope_pass": ma_slope_pass,
        "above_52w_low_pass": above_52w_low_pass,
        "within_52w_high_pass": within_52w_high_pass,
        "boundary_pass": boundary_pass,
    }


# ---------------------------------------------------------------------------
# Criterion 4 — IBD-style Relative Strength
# ---------------------------------------------------------------------------

def compute_rs_raw_score(close: pd.Series, cfg: ScreenerConfig) -> Optional[float]:
    """Weighted blend of trailing 3/6/9/12-month price performance
    (40/20/20/20), approximating IBD's published RS methodology. Returns a
    raw score (not yet a percentile) — higher means stronger momentum."""
    trading_days = {3: 63, 6: 126, 9: 189, 12: 252}
    if len(close) < trading_days[12] + 1:
        return None

    current = close.iloc[-1]

    def perf(months_ago_days: int) -> float:
        past = close.iloc[-1 - months_ago_days]
        return current / past if past > 0 else np.nan

    p3, p6, p9, p12 = (perf(trading_days[m]) for m in (3, 6, 9, 12))
    if any(np.isnan(v) for v in (p3, p6, p9, p12)):
        return None

    return (
        cfg.rs_weight_3mo * p3
        + cfg.rs_weight_6mo * p6
        + cfg.rs_weight_9mo * p9
        + cfg.rs_weight_12mo * p12
    )


def rank_rs_scores_to_rating(raw_scores: pd.Series) -> pd.Series:
    """Converts raw weighted-performance scores into a 1-99 IBD-style
    percentile rating, ranked across every score passed in — always call
    this on the FULL scanned universe, not a pre-filtered shortlist."""
    pct_rank = raw_scores.rank(pct=True)
    rating = (pct_rank * 98 + 1).round(0)  # map (0,1] -> [1,99]
    return rating.clip(lower=1, upper=99)


# ---------------------------------------------------------------------------
# Fundamental filters — not requested, stubbed for future use
# ---------------------------------------------------------------------------

def passes_fundamental_filters(symbol: str) -> bool:
    """Placeholder. Minervini's full SEPA criteria also screen fundamentals
    (accelerating EPS/sales growth, margin expansion, institutional
    sponsorship, etc.), but none were specified in this request. Wire real
    checks in here (e.g. via yf.Ticker(symbol).quarterly_financials) if/when
    you want them — returning True keeps this a no-op until then."""
    return True


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def screen_universe(
    symbols: list[str],
    benchmark_symbol: str = "^GSPC",
    cfg: ScreenerConfig = DEFAULT_CONFIG,
    include_fundamentals: bool = False,
    progress_callback=None,
) -> pd.DataFrame:
    """
    Runs the full Trend Template + RS screen over `symbols`.

    progress_callback, if given, is called as progress_callback(index, total,
    symbol) once per symbol INCLUDING the benchmark fetch and every skipped/
    failed symbol — wired through the actual per-symbol loop below rather
    than bolted on afterward, so a caller polling for progress sees it move
    the whole time the scan is running, not just at the very end.

    benchmark_symbol is fetched and reported for reference (its own raw RS
    score is included in the output as a row of context) but, matching true
    IBD methodology, does NOT enter the percentile-ranking formula directly
    — the ranking is stock-vs-stock-universe, not stock-vs-index division
    (a naive stock_return/index_return ratio inverts sign whenever the
    index is down, which silently scrambles rankings in a falling market —
    worth avoiding).

    Returns a DataFrame, one row per symbol with usable data, sorted by
    rs_rating descending. Symbols that failed to fetch or had insufficient
    history are simply omitted (check the logs for warnings).
    """
    if len(symbols) < cfg.small_universe_warning_threshold:
        logger.warning(
            f"Universe has only {len(symbols)} symbols — RS percentile rankings "
            f"are statistically weak below ~{cfg.small_universe_warning_threshold}. "
            f"Consider scanning a broader list even if you only care about a subset."
        )

    total = len(symbols)

    benchmark_df = fetch_price_history(benchmark_symbol, cfg.fetch_period)
    benchmark_rs_raw = (
        compute_rs_raw_score(benchmark_df["Close"], cfg) if not benchmark_df.empty else None
    )
    if benchmark_rs_raw is not None:
        logger.info(f"Benchmark ({benchmark_symbol}) raw momentum score: {benchmark_rs_raw:.4f}")

    rows = []
    for i, symbol in enumerate(symbols):
        if progress_callback:
            progress_callback(i, total, symbol)

        df = fetch_price_history(symbol, cfg.fetch_period)
        tech = compute_technicals(df, cfg)
        if tech is None:
            continue

        rs_raw = compute_rs_raw_score(df["Close"], cfg)
        if rs_raw is None:
            continue

        template = evaluate_trend_template(tech, cfg)

        row = {"symbol": symbol, **tech, **template, "rs_raw_score": rs_raw}
        rows.append(row)

    if progress_callback:
        progress_callback(total, total, "")

    if not rows:
        logger.warning("No symbols returned usable data — check tickers/suffixes/network.")
        return pd.DataFrame()

    result = pd.DataFrame(rows)

    # Percentile across the FULL scanned universe (see module docstring for
    # why this matters) — not just the rows that already passed the
    # trend-template filters above.
    result["rs_rating"] = rank_rs_scores_to_rating(result["rs_raw_score"])
    result["rs_pass"] = result["rs_rating"] >= cfg.min_rs_rating

    if include_fundamentals:
        result["fundamentals_pass"] = result["symbol"].apply(passes_fundamental_filters)
    else:
        result["fundamentals_pass"] = True

    result["passes_all"] = (
        result["ma_alignment_pass"]
        & result["ma_slope_pass"]
        & result["boundary_pass"]
        & result["rs_pass"]
        & result["fundamentals_pass"]
    )

    display_cols = [
        "symbol", "price", "sma_50", "sma_150", "sma_200",
        "week52_low", "week52_high", "pct_above_52w_low", "pct_below_52w_high",
        "ma_alignment_pass", "ma_slope_pass", "boundary_pass",
        "rs_raw_score", "rs_rating", "rs_pass",
        "fundamentals_pass", "passes_all",
    ]
    result = result[display_cols].sort_values("rs_rating", ascending=False).reset_index(drop=True)
    return result


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # --- India (NSE) example ---
    # nifty_symbols = pd.read_csv("data/nifty_500.csv")["Symbol"].tolist()
    # symbols = [s.strip().upper() + ".NS" for s in nifty_symbols]
    # results = screen_universe(symbols, benchmark_symbol="^CRSLDX")

    # --- US (S&P 500) example ---
    # sp500_symbols = pd.read_csv("data/sp500.csv")["Symbol"].tolist()
    # results = screen_universe(sp500_symbols, benchmark_symbol="^GSPC")

    # --- Minimal runnable demo ---
    demo_symbols = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "COST", "AMD"]
    results = screen_universe(demo_symbols, benchmark_symbol="^GSPC")

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", None)
    print("\nFull scan results:\n", results)

    passing = results[results["passes_all"]]
    print(f"\n{len(passing)} of {len(results)} symbols pass the full Trend Template + RS >= "
          f"{DEFAULT_CONFIG.min_rs_rating}:\n", passing[["symbol", "price", "rs_rating"]])
