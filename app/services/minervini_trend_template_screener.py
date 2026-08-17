"""
app/services/minervini_trend_template_screener.py

Minervini Trend Template screener — CACHE-FIRST implementation.

The original version called yf.download() or yf.Ticker().history() per symbol
inside screen_universe(), causing 500 individual yfinance network calls and
frequent 429 rate-limit errors on a Nifty 500 scan.

This rewrite splits the work into two layers:

  screen_universe(symbols, benchmark_symbol, progress_callback)
      ↳ Public API — unchanged signature so minervini_ind_screener.py needs
        NO modification. Internally it now calls ind_cache.get_price_history_bulk()
        for a single bulk fetch before any per-symbol work begins, then delegates
        to _screen_from_cache() for the actual indicator calculations.

  screen_universe_with_data(symbols, price_data, bench_close, progress_callback)
      ↳ Optional lower-level entry point if the route already has the bulk
        DataFrames (e.g. it shares a cache fetch with another screener running
        in the same request). Skips the cache call entirely.

  _screen_symbol(symbol, df, bench_close)
      ↳ Pure calculation — NO network I/O. Receives a pre-fetched DataFrame,
        computes all Minervini Trend Template conditions plus RS Rating, and
        returns a result dict matching the HTML contract exactly.

Output schema (one dict per qualifying symbol):
    symbol, price,
    sma_50, sma_150, sma_200,
    week52_low, week52_high,
    pct_above_52w_low, pct_below_52w_high,
    ma_alignment_pass, ma_slope_pass, boundary_pass,
    rs_raw_score, rs_rating, rs_pass,
    fundamentals_pass,   ← always True (no fundamental data needed)
    passes_all           ← True only when ALL five conditions pass
"""

import numpy as np
import pandas as pd

# ── shared IND SQLite cache ────────────────────────────────────────────────
from app.services.market_data_cache import ind_cache, latest_bar_date

# ── Minervini Trend Template thresholds ───────────────────────────────────
# All five conditions must be True for passes_all = True.
#
# Condition 1 (ma_alignment_pass):
#   price > SMA50 > SMA150 > SMA200
#
# Condition 2 (ma_slope_pass):
#   SMA150 > SMA200  AND  SMA200 today > SMA200 ~1 month ago (rising)
#
# Condition 3 (boundary_pass):
#   price ≥ 52W low × 1.25  (at least 25% above the 52-week low)
#   price ≤ 52W high × 1.25 (within 25% of the 52-week high)
#   → in Minervini's words: "within 25% of 52-week high"
#
# Condition 4 (rs_pass):
#   RS Rating ≥ 70  (computed as a percentile within the scanned universe)
#
# Condition 5 (fundamentals_pass):
#   Always True — we don't fetch fundamental data to avoid extra API calls.
#   Set to False here and override in screen_universe() if you add EPS data.

MA_SLOPE_LOOKBACK = 20        # sessions (~1 month) for "SMA200 rising" check
RS_PASS_THRESHOLD = 70        # RS Rating percentile minimum
LOOKBACK_DAYS     = 500       # cache lookback — enough for 252-session SMA200 + margin

_noop = lambda *_: None       # default no-op progress callback


# ── per-symbol calculation (pure, no I/O) ─────────────────────────────────

def _screen_symbol(symbol: str, df: pd.DataFrame, bench_close: pd.Series) -> dict | None:
    """
    Apply all Minervini Trend Template conditions to a single pre-fetched
    DataFrame.  Returns a result dict or None if the symbol should be skipped
    (insufficient data, bad data, etc.).

    Parameters
    ----------
    symbol      : yfinance ticker string (e.g. "RELIANCE.NS")
    df          : OHLCV DataFrame from ind_cache — must have a 'Close' column
    bench_close : benchmark Close series (e.g. ^CRSLDX) aligned to trading dates
    """
    try:
        if df is None or df.empty:
            return None

        # ── Use _normalise_df helper pattern from other cache-aware screeners
        # Handle both simple-string and MultiIndex column formats (Memory #8).
        if isinstance(df.columns, pd.MultiIndex):
            # MultiIndex: (field, ticker) — extract the ticker level
            try:
                close = df['Close'][symbol].dropna()
            except (KeyError, TypeError):
                # Try xs approach
                try:
                    close = df.xs(symbol, level=1, axis=1)['Close'].dropna()
                except Exception:
                    return None
        else:
            close = df['Close'].dropna()

        if len(close) < 252:           # need a full year for 52W stats + SMA200
            return None

        current_price = float(close.iloc[-1])
        if current_price <= 0:
            return None

        # ── Moving averages ───────────────────────────────────────────────
        sma_50  = float(close.rolling(50).mean().iloc[-1])
        sma_150 = float(close.rolling(150).mean().iloc[-1])
        sma_200 = float(close.rolling(200).mean().iloc[-1])
        sma_200_month_ago = float(close.rolling(200).mean().iloc[-(MA_SLOPE_LOOKBACK + 1)])

        if any(np.isnan(v) for v in [sma_50, sma_150, sma_200, sma_200_month_ago]):
            return None

        # Condition 1: MA alignment — price > SMA50 > SMA150 > SMA200
        ma_alignment_pass = bool(
            current_price > sma_50 > sma_150 > sma_200
        )

        # Condition 2: MA slope — SMA150 > SMA200 AND SMA200 is rising
        ma_slope_pass = bool(
            sma_150 > sma_200 and sma_200 > sma_200_month_ago
        )

        # ── 52-week range ─────────────────────────────────────────────────
        week52_low  = float(close.iloc[-252:].min())
        week52_high = float(close.iloc[-252:].max())

        pct_above_52w_low  = round((current_price / week52_low  - 1) * 100, 2) if week52_low  > 0 else 0.0
        pct_below_52w_high = round((1 - current_price / week52_high) * 100, 2) if week52_high > 0 else 0.0

        # Condition 3: price ≥ 25% above 52W low AND within 25% of 52W high
        boundary_pass = bool(
            current_price >= week52_low * 1.25 and
            current_price >= week52_high * 0.75
        )

        # ── Relative Strength raw score ───────────────────────────────────
        # Ratio-of-relatives vs benchmark (Memory #1 — never simple subtraction)
        bench_aligned = bench_close.reindex(close.index).ffill()
        rs_raw_score  = 0.0

        if not bench_aligned.isna().any() and len(bench_aligned) >= 252:
            stock_ret = (float(close.iloc[-1]) / float(close.iloc[0])) - 1
            bench_ret = (float(bench_aligned.iloc[-1]) / float(bench_aligned.iloc[0])) - 1
            if (1 + bench_ret) != 0:
                rs_raw_score = float(((1 + stock_ret) / (1 + bench_ret)) - 1)

        return {
            "symbol":            symbol,
            "price":             round(current_price, 2),
            "sma_50":            round(sma_50, 2),
            "sma_150":           round(sma_150, 2),
            "sma_200":           round(sma_200, 2),
            "week52_low":        round(week52_low, 2),
            "week52_high":       round(week52_high, 2),
            "pct_above_52w_low": pct_above_52w_low,
            "pct_below_52w_high": pct_below_52w_high,
            "ma_alignment_pass": ma_alignment_pass,
            "ma_slope_pass":     ma_slope_pass,
            "boundary_pass":     boundary_pass,
            "rs_raw_score":      rs_raw_score,
            "rs_rating":         0,    # filled in after percentile ranking below
            "rs_pass":           False, # filled in after percentile ranking
            "fundamentals_pass": True,  # no fundamental filter — always passes
            "passes_all":        False, # filled in after RS percentile is computed
        }

    except Exception as e:
        print(f"  [Minervini] Error screening {symbol}: {e}")
        return None


# ── percentile ranking + final pass/fail ──────────────────────────────────

def _rank_and_finalise(raw_results: list[dict]) -> pd.DataFrame:
    """
    Given a list of raw result dicts from _screen_symbol(), compute the RS
    Rating percentile within the scanned universe, apply the RS threshold,
    and set passes_all.
    """
    if not raw_results:
        return pd.DataFrame()

    df = pd.DataFrame(raw_results)
    df["rs_raw_score"] = pd.to_numeric(df["rs_raw_score"], errors="coerce").fillna(0.0)

    # RS Rating = percentile rank within this scan's universe (1-99 scale)
    df["rs_rating"] = (
        df["rs_raw_score"]
        .rank(pct=True)
        .mul(98)           # scale to 1-99 range
        .add(1)
        .round(0)
        .clip(1, 99)
        .astype(int)
    )

    df["rs_pass"] = df["rs_rating"] >= RS_PASS_THRESHOLD

    df["passes_all"] = (
        df["ma_alignment_pass"] &
        df["ma_slope_pass"]     &
        df["boundary_pass"]     &
        df["rs_pass"]           &
        df["fundamentals_pass"]
    )

    return df


# ── public entry points ───────────────────────────────────────────────────

def screen_universe_with_data(
    symbols:           list[str],
    price_data:        dict,          # {symbol: DataFrame} from bulk cache fetch
    bench_close:       pd.Series,
    progress_callback  = None,
) -> pd.DataFrame:
    """
    Screen `symbols` using pre-fetched DataFrames — no network I/O.

    Parameters
    ----------
    symbols           : list of yfinance ticker strings (e.g. ["RELIANCE.NS", …])
    price_data        : dict mapping each symbol to its OHLCV DataFrame
    bench_close       : benchmark Close series (^CRSLDX or ^NSEI fallback)
    progress_callback : optional fn(index, total, symbol) called per symbol

    Returns
    -------
    pd.DataFrame with all output columns; may be empty if nothing passes.
    """
    callback = progress_callback or _noop
    total    = len(symbols)
    raw      = []

    for i, sym in enumerate(symbols):
        callback(i, total, sym)
        df     = price_data.get(sym)
        result = _screen_symbol(sym, df, bench_close)
        if result:
            raw.append(result)

    callback(total, total, "")
    return _rank_and_finalise(raw)


def screen_universe(
    symbols:           list[str],
    benchmark_symbol:  str  = "^CRSLDX",
    progress_callback       = None,
) -> pd.DataFrame:
    """
    Original public API — unchanged signature.

    Now routes ALL data fetching through ind_cache (single bulk call) instead
    of per-symbol yfinance calls. This eliminates 500 individual HTTP requests
    on a full Nifty 500 scan and re-uses any data already in the SQLite cache.

    Cache source log printed to terminal (Memory #13).
    """
    callback = progress_callback or _noop
    total    = len(symbols)

    # ── Step 1: fetch benchmark ONCE via shared cache ─────────────────────
    callback(0, total, benchmark_symbol)
    bench_data, _ = ind_cache.get_price_history_bulk(
        [benchmark_symbol], interval="1d", lookback_days=LOOKBACK_DAYS
    )
    bench_df    = bench_data.get(benchmark_symbol)
    bench_close = bench_df["Close"].dropna() if (bench_df is not None and not bench_df.empty) else None

    # Fallback: ^NSEI if ^CRSLDX unavailable
    if bench_close is None or len(bench_close) < 252:
        print(f"[Minervini] {benchmark_symbol} unavailable — trying ^NSEI fallback")
        fb_data, _ = ind_cache.get_price_history_bulk(
            ["^NSEI"], interval="1d", lookback_days=LOOKBACK_DAYS
        )
        fb_df = fb_data.get("^NSEI")
        if fb_df is not None and not fb_df.empty:
            bench_close = fb_df["Close"].dropna()
        else:
            print("[Minervini] Both benchmarks unavailable — RS will be 0 for all")
            bench_close = pd.Series(dtype=float)

    # ── Step 2: bulk-fetch all symbols via shared cache (Memory #8) ───────
    # Progress callback wired through the cache call so the button fill moves
    # during the network phase (not just after it completes).
    def _cache_progress(i, t, sym):
        callback(i, t, sym)

    price_data, fetch_report = ind_cache.get_price_history_bulk(
        symbols,
        interval      = "1d",
        lookback_days = LOOKBACK_DAYS,
        progress_callback = _cache_progress,
    )

    # ── Terminal cache source log (Memory #13) ────────────────────────────
    _n  = len(symbols)
    _ch = fetch_report.get("from_cache", 0)
    _yf = fetch_report.get("fetched",    0)
    _fl = fetch_report.get("failed",     [])
    sep = "=" * 55
    print(f"\n{sep}")
    print(f"  [CACHE] Minervini IND — {benchmark_symbol}")
    print(f"{sep}")
    print(f"  Total  : {_n}")
    print(f"  Cache  : {_ch} ({round(_ch / _n * 100) if _n else 0}%)  ← no yfinance call")
    print(f"  Fetched: {_yf} ({round(_yf / _n * 100) if _n else 0}%)  ← yfinance + DB updated")
    print(f"  Failed : {len(_fl)}")
    if _fl:
        extra = f" …+{len(_fl) - 10} more" if len(_fl) > 10 else ""
        print(f"  Symbols: {', '.join(_fl[:10])}{extra}")
    print(f"  As of  : {latest_bar_date(price_data)}")
    print(f"{sep}\n")

    # ── Step 3: per-symbol screening (pure calculation, no I/O) ──────────
    results_df = screen_universe_with_data(
        symbols,
        price_data,
        bench_close,
        progress_callback = callback,
    )

    # Return DataFrame + fetch stats so the route can surface
    # cache_hits / yf_fetches / price_data_asof to the template
    # (Memory #13 -- show 💾 Cache badge and Price Data As Of).
    fetch_stats = {
        'cache_hits':      _ch,
        'yf_fetches':      _yf,
        'stale_count':     len(_fl),
        'stale_sample':    _fl[:10],
        'price_data_asof': latest_bar_date(price_data),
    }
    return results_df, fetch_stats