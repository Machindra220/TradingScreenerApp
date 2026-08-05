import os
import io
import json
import numpy as np
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, jsonify, request

chart_weinstein_bp = Blueprint("chart_weinstein", __name__)

# Self-contained on purpose (same convention as chart_carousel.py / chart_combined.py
# each keeping their own copy of this config rather than importing from one another).
UPLOAD_ROOT = os.path.abspath(os.path.join(os.getcwd(), 'uploads'))

SCREENER_CONFIG = {
    "NSE": {
        "Volar_Stage2": os.path.join(UPLOAD_ROOT, 'volar_ind', 'last_volar_results.json'),
        "Adaptive_RS": os.path.join(UPLOAD_ROOT, 'volar_ind_adaptive', 'volar_results_ind_adaptive.json'),
        "RS_ROC_Momentum": os.path.join(UPLOAD_ROOT, 'rs_roc', 'last_rs_roc_results.json'),
        "HH_HL_Screener": os.path.join(UPLOAD_ROOT, 'india_hhhl', 'last_india_hhhl_results.json'),
        "Gap_Volume": os.path.join(UPLOAD_ROOT, 'gap_volume_india', 'last_gap_vol_india_results.json'),
        "Stage2_Screener": os.path.join(UPLOAD_ROOT, 'india_screener', 'last_stage2_india_results.json'),
        "IBD_SmartSelect": os.path.join(UPLOAD_ROOT, 'ibd_india', 'last_ibd_india_results.json')
    },
    "US": {
        "Volar_Stage2": os.path.join(UPLOAD_ROOT, 'volar_us', 'last_volar_us_results.json'),
        "Adaptive_RS": os.path.join(UPLOAD_ROOT, 'volar_us_adaptive', 'volar_results_adaptive.json'),
        "RS_ROC_Momentum": os.path.join(UPLOAD_ROOT, 'rs_roc', 'last_rs_roc_results.json'),
        "Stage2_Screener": os.path.join(UPLOAD_ROOT, 'us_screener', 'cached_results.json'),
        "Gap_Volume": os.path.join(UPLOAD_ROOT, 'gap_volume', 'last_gap_vol_results.json'),
        "IBD_SmartSelect": os.path.join(UPLOAD_ROOT, 'ibd_us', 'last_ibd_us_results.json')
    }
}

MARKET_SUFFIX = {"US": "", "NSE": ".NS"}
MARKET_BENCHMARK = {"US": "^GSPC", "NSE": "^CRSLDX"}

# Weinstein's method is explicitly a WEEKLY chart method — daily data is
# downloaded and resampled to weekly bars below, with generous extra history
# so the 30-week MA (needs 30 bars) has proper warm-up before the display window.
RANGE_OPTIONS = {
    # Default — always pull whatever yfinance actually has (period="max") and
    # show all of it (weeks=None means "don't slice"). This is what avoids
    # the "no data" failures: a fixed default like 5Y was downloading a fixed
    # period regardless of how much history a given stock actually has, and
    # a stock listed for less than that window could fall under the minimum-
    # bar checks below even though yfinance had perfectly usable data for it.
    "MAX": {"weeks": None, "download_period": "max"},
    "2Y":  {"weeks": 104, "download_period": "5y"},
    "3Y":  {"weeks": 156, "download_period": "6y"},
    "5Y":  {"weeks": 260, "download_period": "8y"},
    "10Y": {"weeks": 520, "download_period": "max"},
}

# ----------------------------------------------------------------------------
# Tunable thresholds for the stage/zone/event read below. Kept as named
# constants (not magic numbers) so the logic stays auditable/adjustable.
# ----------------------------------------------------------------------------
VOLUME_SURGE_MULTIPLE = 1.5     # Weinstein's "well above average" volume-confirmation bar
MA_SLOPE_LOOKBACK = 4            # weeks, short-term slope read on the 30-week MA
MA_SLOPE_FLAT_PCT = 0.5          # slope magnitude under this % over the lookback = "flat"
PIVOT_LEFT = 3                   # bars on each side required to confirm a swing high/low
PIVOT_RIGHT = 3
ZONE_CLUSTER_PCT = 3.0           # merge pivots within this % of each other into one zone
ZONE_LOOKBACK_WEEKS = 52         # only look at pivots from the last N weeks for "the current range"
BREAKOUT_LOOKBACK_WEEKS = 12     # how far back a breakout/breakdown still counts as "recent"
EXTENDED_ABOVE_MA_PCT = 30.0     # Weinstein's chasing-caution zone: %+ above the 30WMA

RS_SMA_WEEKS = 10                # smoothing window for the RS trend line
RS_NEW_HIGH_LOOKBACK_WEEKS = 52  # "52-week RS high" is Weinstein's own convention
RS_NEW_HIGH_RECENT_WEEKS = 8     # how far back a 52-week RS high still counts as a "recent" event
RS_GROWTH_LOOKBACK_WEEKS = 13    # ~1 quarter — window used to rank cross-sectional RS strength


# ----------------------------------------------------------------------------
# Screener cache loading (identical pattern to chart_carousel.py)
# ----------------------------------------------------------------------------

def _load_screener_stock_entries(market, screener_key):
    if market not in SCREENER_CONFIG or screener_key not in SCREENER_CONFIG[market]:
        return []
    target_path = SCREENER_CONFIG[market][screener_key]
    entries = []
    if os.path.exists(target_path):
        try:
            with open(target_path, 'r') as f:
                cached_data = json.load(f)
            if isinstance(cached_data, dict):
                sections = cached_data.get('sections', {})
                if sections:
                    for sec_list in sections.values():
                        entries.extend([s for s in sec_list if 'symbol' in s])
                else:
                    entries = [s for s in cached_data.get('stocks', []) if 'symbol' in s]
            elif isinstance(cached_data, list):
                entries = [s for s in cached_data if 'symbol' in s]
        except Exception as e:
            print(f"Failed to load screener entries for {market}/{screener_key}: {e}")
    return entries


def _dedupe_tickers(entries):
    seen = set()
    out = []
    for e in entries:
        sym = e['symbol'].strip().upper()
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def _resolve_screener_key(market, screener_key):
    if market not in SCREENER_CONFIG:
        market = "NSE"
    if not screener_key or screener_key not in SCREENER_CONFIG[market]:
        screener_key = list(SCREENER_CONFIG[market].keys())[0]
    return market, screener_key


# ----------------------------------------------------------------------------
# Weekly resampling + pivot/zone/stage math
# ----------------------------------------------------------------------------

def resample_weekly(daily_df):
    """Standard Friday-ending weekly bars from daily OHLCV."""
    weekly = daily_df.resample('W-FRI').agg({
        'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
    })
    return weekly.dropna(subset=['Close', 'Open', 'High', 'Low'])


def find_pivots(series, left, right, mode):
    """Simple swing high/low detector: a bar qualifies if it's the max/min
    within a window of `left` bars before and `right` bars after it.

    Cast to float explicitly (rather than trusting series.values' dtype):
    thinly-traded tickers occasionally give yfinance/pandas a reason to leave
    a column as object-dtype with a stray Python None in it, and comparing
    None against a number with '>' raises TypeError instead of just being
    "missing data". Casting to float turns any None into a proper NaN, which
    comparisons handle gracefully (just evaluate to False) instead of crashing.
    """
    vals = np.asarray(series, dtype=float)
    n = len(vals)
    idxs = []
    for i in range(left, n - right):
        window = vals[i - left:i + right + 1]
        if np.isnan(window).any():
            continue
        if mode == 'high' and vals[i] >= window.max():
            idxs.append(i)
        elif mode == 'low' and vals[i] <= window.min():
            idxs.append(i)
    return idxs


def cluster_zone(prices, cluster_pct):
    """Given pivot price levels (chronological order), find the tightest
    cluster with the most touches and return (low, high, touch_count) — this
    is "the" support or resistance zone, mirroring the single horizontal band
    drawn in Weinstein's own charts rather than every individual swing point."""
    if not prices:
        return None
    prices_sorted = sorted(prices)
    best_band = None
    for p in prices_sorted:
        band = [q for q in prices_sorted if p > 0 and abs(q - p) / p * 100 <= cluster_pct]
        if best_band is None or len(band) > len(best_band):
            best_band = band
    if best_band and len(best_band) >= 2:
        return (round(float(min(best_band)), 2), round(float(max(best_band)), 2), len(best_band))
    latest = prices[-1]
    half_band = latest * (cluster_pct / 200.0)
    return (round(float(latest - half_band), 2), round(float(latest + half_band), 2), 1)


def compute_weinstein_analysis(weekly_df):
    """Core read: stage classification, support/resistance zones, and any
    recent breakout/breakdown/pullback event. Everything here traces to a
    plain, inspectable rule — no black-box scoring."""
    df = weekly_df
    n = len(df)
    if n < 34:
        return None  # need at least 30 weeks for the MA itself + a few more for a slope read

    close = np.asarray(df['Close'].values, dtype=float)
    sma30 = np.asarray(df['sma30'].values, dtype=float)
    last = n - 1

    def slope_pct(arr, i, lookback):
        j = i - lookback
        if j < 0 or np.isnan(arr[i]) or np.isnan(arr[j]) or arr[j] == 0:
            return 0.0
        return float((arr[i] - arr[j]) / arr[j] * 100)

    cur_slope = slope_pct(sma30, last, MA_SLOPE_LOOKBACK)
    prior_slope = slope_pct(sma30, max(0, last - MA_SLOPE_LOOKBACK), MA_SLOPE_LOOKBACK)

    stage = None
    if not np.isnan(sma30[last]):
        above_ma = close[last] > sma30[last]
        if cur_slope > MA_SLOPE_FLAT_PCT and above_ma:
            stage = 2   # Advancing
        elif cur_slope < -MA_SLOPE_FLAT_PCT and not above_ma:
            stage = 4   # Declining
        elif abs(cur_slope) <= MA_SLOPE_FLAT_PCT:
            # MA has gone flat — was the trend coming into this flattening up
            # or down? That distinguishes Stage 1 (basing after a decline)
            # from Stage 3 (topping after an advance).
            stage = 3 if prior_slope > 0 else 1
        else:
            stage = 2 if above_ma else 4  # transitional bar — lean on price position

    # --- Support / resistance zones from recent swing pivots ---
    lb = min(ZONE_LOOKBACK_WEEKS, n)
    recent = df.iloc[-lb:]
    high_pivot_idx = find_pivots(recent['High'], PIVOT_LEFT, PIVOT_RIGHT, 'high')
    low_pivot_idx = find_pivots(recent['Low'], PIVOT_LEFT, PIVOT_RIGHT, 'low')
    resistance_prices = [float(recent['High'].iloc[i]) for i in high_pivot_idx]
    support_prices = [float(recent['Low'].iloc[i]) for i in low_pivot_idx]

    resistance_zone = cluster_zone(resistance_prices, ZONE_CLUSTER_PCT)
    support_zone = cluster_zone(support_prices, ZONE_CLUSTER_PCT)

    # --- Breakout / breakdown / pullback read ---
    events = {
        "breakout": False, "breakout_weeks_ago": None, "breakout_volume_confirmed": False,
        "breakdown": False, "breakdown_weeks_ago": None, "breakdown_volume_confirmed": False,
        "pullback_holding": False, "pullback_failed": False,
        "extended_above_ma_pct": None,
    }

    lookback_n = min(BREAKOUT_LOOKBACK_WEEKS, n - 1)

    if resistance_zone:
        r_hi = resistance_zone[1]
        for w in range(1, lookback_n + 1):
            i = n - w
            if i - 1 < 0:
                break
            if close[i] > r_hi and close[i - 1] <= r_hi:
                events["breakout"] = True
                events["breakout_weeks_ago"] = w - 1
                vol_i_raw = df['Volume'].iloc[i]
                avgvol_i_raw = df['avg_vol10'].iloc[i - 1]
                vol_i = float(vol_i_raw) if pd.notna(vol_i_raw) else float('nan')
                avgvol_i = float(avgvol_i_raw) if pd.notna(avgvol_i_raw) else float('nan')
                events["breakout_volume_confirmed"] = bool(
                    not np.isnan(avgvol_i) and vol_i > avgvol_i * VOLUME_SURGE_MULTIPLE
                )
                break
        if events["breakout"]:
            if close[last] >= r_hi:
                pass  # still at/above the breakout level, no pullback yet
            elif close[last] >= r_hi * (1 - ZONE_CLUSTER_PCT / 100):
                events["pullback_holding"] = True   # classic Weinstein "buy the pullback" zone
            else:
                events["pullback_failed"] = True     # gave the breakout back — treat with caution

    if support_zone:
        s_lo = support_zone[0]
        for w in range(1, lookback_n + 1):
            i = n - w
            if i - 1 < 0:
                break
            if close[i] < s_lo and close[i - 1] >= s_lo:
                events["breakdown"] = True
                events["breakdown_weeks_ago"] = w - 1
                vol_i_raw = df['Volume'].iloc[i]
                avgvol_i_raw = df['avg_vol10'].iloc[i - 1]
                vol_i = float(vol_i_raw) if pd.notna(vol_i_raw) else float('nan')
                avgvol_i = float(avgvol_i_raw) if pd.notna(avgvol_i_raw) else float('nan')
                events["breakdown_volume_confirmed"] = bool(
                    not np.isnan(avgvol_i) and vol_i > avgvol_i * VOLUME_SURGE_MULTIPLE
                )
                break

    if not np.isnan(sma30[last]) and sma30[last] > 0:
        events["extended_above_ma_pct"] = round(float((close[last] / sma30[last] - 1) * 100), 2)

    return {
        "stage": stage,
        "ma_slope_pct": round(cur_slope, 3),
        "resistance_zone": resistance_zone,
        "support_zone": support_zone,
        "events": events,
    }


STAGE_LABELS = {
    1: "Stage 1 — Basing (Accumulation)",
    2: "Stage 2 — Advancing (Markup)",
    3: "Stage 3 — Topping (Distribution)",
    4: "Stage 4 — Declining (Markdown)",
    None: "Unclassified — not enough history yet",
}


def compute_rs_events(rs_ratio):
    """RS-specific reads, independent of the price/stage analysis above:

    - rs_new_high: has the weekly RS ratio (stock ÷ benchmark) made a new
      RS_NEW_HIGH_LOOKBACK_WEEKS-week high within the last RS_NEW_HIGH_RECENT_WEEKS
      weeks? Weinstein treats RS making new highs — especially ahead of price
      itself — as one of the earliest, most reliable strength signals.
    - rs_trend_up: is the RS ratio higher than it was ~4 weeks ago (a short
      trend read, same style as the 30WMA slope check).
    """
    result = {"rs_new_high": False, "rs_new_high_weeks_ago": None, "rs_trend_up": False}
    vals = np.asarray(rs_ratio.values, dtype=float)
    n = len(vals)
    if n < 2:
        return result

    lookback = min(RS_NEW_HIGH_LOOKBACK_WEEKS, n)
    recent_window = min(RS_NEW_HIGH_RECENT_WEEKS, n - 1)

    for w in range(0, recent_window + 1):
        i = n - 1 - w
        if i < 0:
            break
        window = vals[max(0, i - lookback + 1):i + 1]
        if len(window) == 0 or np.all(np.isnan(window)) or np.isnan(vals[i]):
            continue
        if vals[i] >= np.nanmax(window):
            result["rs_new_high"] = True
            result["rs_new_high_weeks_ago"] = w
            break

    j = n - 1
    if j - 4 >= 0 and not np.isnan(vals[j]) and not np.isnan(vals[j - 4]) and vals[j - 4] != 0:
        result["rs_trend_up"] = bool(vals[j] > vals[j - 4])

    return result


@chart_weinstein_bp.route("/chart-weinstein-stage-analysis")
def weinstein_dashboard():
    default_market = request.args.get("market", "NSE").strip().upper()
    if default_market not in MARKET_SUFFIX: default_market = "NSE"
    default_stock = request.args.get("symbol", "PRAJIND" if default_market == "NSE" else "NVDA")
    return render_template(
        "chart_weinstein.html",
        default_stock=default_stock,
        default_market=default_market
    )


@chart_weinstein_bp.route("/api/v1/weinstein-ticker-payload")
def get_weinstein_ticker_payload():
    market = request.args.get("market", "NSE").strip().upper()
    screener_key = request.args.get("screener", "").strip()
    market, screener_key = _resolve_screener_key(market, screener_key)

    entries = _load_screener_stock_entries(market, screener_key)
    tickers = _dedupe_tickers(entries)

    return jsonify({"status": "success", "market": market, "screener": screener_key, "tickers": tickers})


@chart_weinstein_bp.route("/api/v1/weinstein-telemetry-data/<symbol>")
def get_weinstein_telemetry_data(symbol):
    try:
        market = request.args.get("market", "NSE").strip().upper()
        if market not in MARKET_SUFFIX: market = "NSE"
        suffix = MARKET_SUFFIX[market]

        range_key = request.args.get("range", "MAX").strip().upper()
        if range_key not in RANGE_OPTIONS: range_key = "MAX"
        range_cfg = RANGE_OPTIONS[range_key]

        symbol_clean = symbol.strip().upper().replace(".NS", "").replace(".", "-")
        fetch_symbol = f"{symbol_clean}{suffix}"
        benchmark_symbol = MARKET_BENCHMARK[market]

        data = yf.download(
            [fetch_symbol, benchmark_symbol], period=range_cfg["download_period"], interval="1d",
            auto_adjust=True, progress=False
        )

        if data.empty:
            return jsonify({"status": "error", "message": f"No data returned for '{symbol_clean}'."}), 400

        if isinstance(data.columns, pd.MultiIndex):
            if data.columns.names[0] != 'Price':
                try: data.columns = data.columns.swaplevel(0, 1)
                except Exception: pass
            data.columns.names = ['Price', 'Ticker']

        if 'Close' not in data or fetch_symbol not in data['Close'].columns:
            return jsonify({"status": "error", "message": f"Invalid {market} ticker '{symbol_clean}'."}), 400

        daily = pd.DataFrame({
            "Open": data['Open'][fetch_symbol], "High": data['High'][fetch_symbol],
            "Low": data['Low'][fetch_symbol], "Close": data['Close'][fetch_symbol],
            "Volume": data['Volume'][fetch_symbol] if 'Volume' in data else pd.Series(dtype=float)
        })
        bench_daily_close = (
            data['Close'][benchmark_symbol] if benchmark_symbol in data['Close'].columns
            else pd.Series(dtype=float)
        )

        # Coerce to clean numeric dtype BEFORE any resampling/aggregation.
        # This has to happen here, not after resample_weekly() — thinly-
        # traded tickers (CEMPRO, THANGAMAYL, etc.) can hand yfinance actual
        # Python None values on gappy days, leaving an object-dtype column.
        # resample('W-FRI').agg({'High':'max','Low':'min',...}) then does
        # pairwise '>' comparisons internally to find the max/min, and
        # comparing an int to None raises TypeError right there — before any
        # sanitization done on the *output* of resampling gets a chance to
        # run. Coercing the raw daily columns first (turning any None into a
        # proper NaN) fixes it at the actual source.
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            daily[col] = pd.to_numeric(daily[col], errors="coerce")
        bench_daily_close = pd.to_numeric(bench_daily_close, errors="coerce")

        daily = daily.dropna(subset=["Close", "Open", "High", "Low"])
        if len(daily) < 180:
            return jsonify({
                "status": "error",
                "message": f"'{symbol_clean}' only has {len(daily)} trading days of history on yfinance — "
                           f"a 30-week moving average needs at least ~180 (about 8-9 months). "
                           f"This stock may be too newly listed for Stage Analysis yet."
            }), 400

        weekly = resample_weekly(daily)

        # Thinly-traded tickers occasionally leave pandas/yfinance with an
        # object-dtype column holding a stray Python None instead of a
        # proper NaN (this is what was breaking THANGAMAYL: a bare None
        # reaching a numeric '>' comparison raises TypeError instead of
        # just being treated as missing data). pd.to_numeric(errors='coerce')
        # forces every column to real floats, turning any such None into a
        # NaN that the rest of the pipeline already knows how to skip.
        for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
            weekly[col] = pd.to_numeric(weekly[col], errors='coerce')
        weekly = weekly.dropna(subset=['Open', 'High', 'Low', 'Close'])

        if len(weekly) < 34:
            return jsonify({
                "status": "error",
                "message": f"'{symbol_clean}' has only {len(weekly)} clean weekly bars after removing "
                           f"gaps/bad data points — a 30-week MA needs at least 34."
            }), 400

        weekly['sma30'] = weekly['Close'].rolling(30).mean()
        weekly['avg_vol10'] = weekly['Volume'].rolling(10).mean()

        # --- Relative Strength vs the market benchmark (weekly), Weinstein's
        # own preferred RS read — same benchmark tickers as the carousel/
        # combined charts (^GSPC / ^CRSLDX) so all three stay consistent. ---
        bench_weekly = bench_daily_close.resample('W-FRI').last().ffill().bfill()
        bench_weekly = bench_weekly.reindex(weekly.index, method='ffill').bfill()
        weekly['bench'] = pd.to_numeric(bench_weekly, errors='coerce')
        weekly['rs_ratio'] = weekly['Close'] / weekly['bench']
        weekly['rs_sma'] = weekly['rs_ratio'].rolling(RS_SMA_WEEKS).mean()

        analysis = compute_weinstein_analysis(weekly)
        if analysis is None:
            return jsonify({
                "status": "error",
                "message": "Not enough weekly bars yet for a 30-week MA read (need 34+ weeks of history)."
            }), 400

        rs_events = compute_rs_events(weekly['rs_ratio'])

        weeks = range_cfg["weeks"]
        display = weekly.iloc[-weeks:] if (weeks is not None and len(weekly) > weeks) else weekly
        # Index of the first displayed bar within the *full* weekly series —
        # needed to translate event "weeks ago" positions into marker dates below.
        display_start_pos = len(weekly) - len(display)

        candles, sma30_series, volume_series = [], [], []
        rs_ratio_series, rs_sma_series = [], []
        for idx, row in display.iterrows():
            date_str = idx.strftime("%Y-%m-%d")
            candles.append({
                "time": date_str, "open": round(float(row['Open']), 2), "high": round(float(row['High']), 2),
                "low": round(float(row['Low']), 2), "close": round(float(row['Close']), 2),
                "volume": int(row['Volume'])
            })
            if not pd.isna(row['sma30']):
                sma30_series.append({"time": date_str, "value": round(float(row['sma30']), 2)})

            surge = bool(not pd.isna(row['avg_vol10']) and row['Volume'] > row['avg_vol10'] * VOLUME_SURGE_MULTIPLE)
            up_week = row['Close'] >= row['Open']
            color = 'rgba(245,158,11,0.85)' if surge else ('rgba(34,197,94,0.5)' if up_week else 'rgba(239,68,68,0.5)')
            volume_series.append({"time": date_str, "value": int(row['Volume']), "color": color, "surge": surge})

            if not pd.isna(row['rs_ratio']):
                rs_ratio_series.append({"time": date_str, "value": round(float(row['rs_ratio']), 6)})
            if not pd.isna(row['rs_sma']):
                rs_sma_series.append({"time": date_str, "value": round(float(row['rs_sma']), 6)})

        markers = []
        ev = analysis['events']
        full_n = len(weekly)
        if ev['breakout'] and ev['breakout_weeks_ago'] is not None:
            i = full_n - 1 - ev['breakout_weeks_ago']
            if display_start_pos <= i < full_n:
                t = weekly.index[i].strftime("%Y-%m-%d")
                markers.append({
                    "time": t, "position": "belowBar", "color": "#22c55e", "shape": "arrowUp",
                    "text": "Breakout ✓Vol" if ev['breakout_volume_confirmed'] else "Breakout"
                })
        if ev['breakdown'] and ev['breakdown_weeks_ago'] is not None:
            i = full_n - 1 - ev['breakdown_weeks_ago']
            if display_start_pos <= i < full_n:
                t = weekly.index[i].strftime("%Y-%m-%d")
                markers.append({
                    "time": t, "position": "aboveBar", "color": "#ef4444", "shape": "arrowDown",
                    "text": "Breakdown ✓Vol" if ev['breakdown_volume_confirmed'] else "Breakdown"
                })

        rs_markers = []
        if rs_events['rs_new_high'] and rs_events['rs_new_high_weeks_ago'] is not None:
            i = full_n - 1 - rs_events['rs_new_high_weeks_ago']
            if display_start_pos <= i < full_n:
                t = weekly.index[i].strftime("%Y-%m-%d")
                rs_markers.append({
                    "time": t, "position": "aboveBar", "color": "#facc15", "shape": "circle",
                    "text": f"{RS_NEW_HIGH_LOOKBACK_WEEKS}W RS High"
                })

        return jsonify({
            "status": "success", "symbol": symbol_clean, "market": market, "range": range_key,
            "stage": analysis['stage'], "stage_label": STAGE_LABELS.get(analysis['stage']),
            "ma_slope_pct": analysis['ma_slope_pct'],
            "resistance_zone": analysis['resistance_zone'],
            "support_zone": analysis['support_zone'],
            "events": analysis['events'],
            "rs_events": rs_events,
            "series": {
                "candles": candles, "sma30": sma30_series, "volume": volume_series, "markers": markers,
                "rs_ratio": rs_ratio_series, "rs_sma": rs_sma_series, "rs_markers": rs_markers
            }
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ----------------------------------------------------------------------------
# Weekly RS strength scanner — ranks the entire active screener list by
# cross-sectional relative strength, answering "which stocks are actually
# strong this week" rather than reading one ticker at a time.
#
# One batch yfinance call (ticker list + the market benchmark together),
# then per ticker: weekly-resample, compute the same rs_ratio used on the
# main chart (stock ÷ benchmark), and rank by how much that ratio has grown
# over the last RS_GROWTH_LOOKBACK_WEEKS (~1 quarter) — a % change is
# comparable across tickers regardless of each stock's absolute price level,
# which a raw rs_ratio value is not.
# ----------------------------------------------------------------------------

@chart_weinstein_bp.route("/api/v1/weinstein-rs-scanner")
def get_weinstein_rs_scanner():
    market = request.args.get("market", "NSE").strip().upper()
    screener_key = request.args.get("screener", "").strip()
    market, screener_key = _resolve_screener_key(market, screener_key)

    entries = _load_screener_stock_entries(market, screener_key)
    tickers = _dedupe_tickers(entries)

    if not tickers:
        return jsonify({"status": "success", "market": market, "screener": screener_key, "count": 0, "ranked": []})

    suffix = MARKET_SUFFIX[market]
    benchmark_symbol = MARKET_BENCHMARK[market]
    fetch_list = [f"{t}{suffix}" for t in tickers] if market == "NSE" else list(tickers)
    full_fetch_list = fetch_list + [benchmark_symbol]

    try:
        raw = yf.download(
            full_fetch_list, period="2y", interval="1d",
            auto_adjust=True, progress=False, group_by='ticker', threads=True
        )
    except Exception as e:
        return jsonify({"status": "error", "message": f"Batch download failed: {e}"}), 500

    # Pull the benchmark's own weekly close once, reused for every ticker.
    try:
        if benchmark_symbol in raw.columns.get_level_values(0):
            bench_daily = pd.to_numeric(raw[benchmark_symbol]['Close'], errors='coerce').dropna()
        else:
            return jsonify({"status": "error", "message": f"Benchmark '{benchmark_symbol}' data unavailable."}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": f"Couldn't read benchmark data: {e}"}), 500

    bench_weekly = bench_daily.resample('W-FRI').last().ffill().bfill()

    ranked = []
    for orig_sym, fsym in zip(tickers, fetch_list):
        try:
            if fsym not in raw.columns.get_level_values(0):
                continue
            df = raw[fsym]
            close = pd.to_numeric(df['Close'], errors='coerce').dropna()
            if len(close) < 100:
                continue

            weekly_close = close.resample('W-FRI').last().dropna()
            if len(weekly_close) < RS_GROWTH_LOOKBACK_WEEKS + 5:
                continue

            bench_aligned = bench_weekly.reindex(weekly_close.index, method='ffill').bfill()
            rs_ratio = weekly_close / bench_aligned
            rs_ratio = rs_ratio.dropna()
            if len(rs_ratio) < RS_GROWTH_LOOKBACK_WEEKS + 1:
                continue

            rs_now = float(rs_ratio.iloc[-1])
            rs_then = float(rs_ratio.iloc[-1 - RS_GROWTH_LOOKBACK_WEEKS])
            if rs_then <= 0:
                continue
            rs_growth_pct = round((rs_now / rs_then - 1) * 100, 2)

            rs_events = compute_rs_events(rs_ratio)

            ranked.append({
                "symbol": orig_sym,
                "rs_growth_pct": rs_growth_pct,
                "last_close": round(float(weekly_close.iloc[-1]), 2),
                "rs_new_high": rs_events["rs_new_high"],
                "rs_new_high_weeks_ago": rs_events["rs_new_high_weeks_ago"],
                "rs_trend_up": rs_events["rs_trend_up"],
            })
        except Exception:
            continue

    if not ranked:
        return jsonify({"status": "success", "market": market, "screener": screener_key, "count": 0, "ranked": []})

    # Cross-sectional percentile: how this ticker's RS growth compares to
    # every other ticker actually scanned, not an absolute scale.
    growth_values = sorted([r["rs_growth_pct"] for r in ranked])
    n_vals = len(growth_values)
    for r in ranked:
        rank_pos = sum(1 for v in growth_values if v <= r["rs_growth_pct"])
        r["rs_percentile"] = int(round(rank_pos / n_vals * 100))

    ranked.sort(key=lambda r: -r["rs_growth_pct"])

    return jsonify({
        "status": "success", "market": market, "screener": screener_key,
        "lookback_weeks": RS_GROWTH_LOOKBACK_WEEKS, "count": len(ranked), "scanned": len(tickers), "ranked": ranked
    })


# ----------------------------------------------------------------------------
# Custom symbol-list upload — identical pattern/behavior to the carousel's
# upload endpoint, kept local rather than imported so this blueprint has no
# hard dependency on chart_carousel.py.
# ----------------------------------------------------------------------------

ALLOWED_UPLOAD_EXTENSIONS = {'.csv', '.xlsx', '.xls'}


@chart_weinstein_bp.route("/api/v1/weinstein-upload-symbols", methods=["POST"])
def upload_weinstein_symbols():
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file was included in the upload request."}), 400

    file = request.files['file']
    if not file or file.filename == '':
        return jsonify({"status": "error", "message": "No file selected."}), 400

    filename = file.filename
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        return jsonify({
            "status": "error",
            "message": f"Unsupported file type '{ext or 'unknown'}'. Please upload a .csv, .xlsx, or .xls file."
        }), 400

    try:
        raw_bytes = file.read()
        buffer = io.BytesIO(raw_bytes)
        if ext == '.csv':
            try:
                df = pd.read_csv(buffer, encoding='utf-8-sig')
            except UnicodeDecodeError:
                buffer.seek(0)
                df = pd.read_csv(buffer)
        elif ext == '.xlsx':
            df = pd.read_excel(buffer, engine='openpyxl')
        else:
            df = pd.read_excel(buffer)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Couldn't parse '{filename}': {e}"}), 400

    df.columns = [str(c).replace('\ufeff', '').strip() for c in df.columns]

    symbol_col = None
    for col in df.columns:
        if str(col).strip().lower() == 'symbol':
            symbol_col = col
            break

    if symbol_col is None:
        return jsonify({
            "status": "error",
            "message": "No 'Symbol' column found in the uploaded file. Make sure the header row has a column named exactly 'Symbol'."
        }), 400

    raw_symbols = df[symbol_col].dropna().astype(str).tolist()
    tickers = []
    seen = set()
    for s in raw_symbols:
        sym = s.strip().upper().replace('.NS', '')
        if sym and sym.upper() not in ('NAN', 'NONE', '') and sym not in seen:
            seen.add(sym)
            tickers.append(sym)

    if not tickers:
        return jsonify({"status": "error", "message": "The 'Symbol' column didn't contain any usable ticker values."}), 400

    return jsonify({"status": "success", "filename": filename, "count": len(tickers), "tickers": tickers})