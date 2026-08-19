import os
import json
import numpy as np
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, jsonify, request

from app.services.market_data_cache import ind_cache, us_cache, latest_bar_date

chart_combined_bp = Blueprint("chart_engine_combined", __name__)

# Path anchored to __file__ — never os.getcwd() which breaks under the
# Werkzeug reloader when the working directory shifts.
_PROJECT_ROOT     = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

# Cache files reused from the existing per-market screeners — combined chart
# doesn't compute its own RS percentile, it reads whichever cache matches
# the requested market so the number on screen always matches the screener.
US_UPLOAD_FOLDER  = os.path.join(_PROJECT_ROOT, 'uploads', 'volar_us')
US_RESULTS_JSON   = os.path.join(US_UPLOAD_FOLDER, 'last_volar_us_results.json')
NSE_UPLOAD_FOLDER = os.path.join(_PROJECT_ROOT, 'uploads', 'rs_roc')
NSE_RESULTS_JSON  = os.path.join(NSE_UPLOAD_FOLDER, 'last_rs_roc_results.json')

MARKET_CONFIG = {
    "US": {
        "benchmark": "^GSPC",
        "suffix": "",
        "results_json": US_RESULTS_JSON,
    },
    "NSE": {
        "benchmark": "^CRSLDX",
        "suffix": ".NS",
        "results_json": NSE_RESULTS_JSON,
    },
}

# Display window + how much history to download so EMA200 has warm-up room
RANGE_OPTIONS = {
    "3M": {"months": 3,  "download_period": "2y"},
    "6M": {"months": 6,  "download_period": "2y"},
    "1Y": {"months": 12, "download_period": "2y"},
    "2Y": {"months": 24, "download_period": "3y"},
}


def calculate_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def calculate_sma(series, window):
    return series.rolling(window=window).mean()


def calculate_slope(series, window=5):
    """Short-term directional slope using a rolling linear regression."""
    y = series.tail(window).values
    x = np.arange(len(y))
    if len(y) < window:
        return 0.0
    slope, _ = np.polyfit(x, y, 1)
    return slope


@chart_combined_bp.route("/analytics-chart-combined", endpoint="analytics-chart-combined")
def chart_combined_dashboard():
    default_market = request.args.get("market", "US").strip().upper()
    if default_market not in MARKET_CONFIG:
        default_market = "US"
    default_stock = request.args.get(
        "symbol", "NVDA" if default_market == "US" else "PRAJIND"
    )
    return render_template(
        "chart_combined.html",
        default_stock=default_stock,
        default_market=default_market,
    )


@chart_combined_bp.route("/api/v1/chart-telemetry-combined/<symbol>")
def get_chart_telemetry_combined(symbol):
    try:
        market = request.args.get("market", "US").strip().upper()
        if market not in MARKET_CONFIG:
            market = "US"
        cfg = MARKET_CONFIG[market]
        benchmark_symbol = cfg["benchmark"]

        range_key = request.args.get("range", "1Y").strip().upper()
        if range_key not in RANGE_OPTIONS:
            range_key = "1Y"
        range_cfg = RANGE_OPTIONS[range_key]

        symbol_clean = symbol.strip().upper().replace(".NS", "").replace(".", "-")
        fetch_symbol = f"{symbol_clean}{cfg['suffix']}" if market == "NSE" else symbol_clean

        # Primary: yfinance — charts always show the latest available price data.
        # Fallback: SQLite cache — used only when yfinance is unavailable/rate-limited.
        stock_df = bench_df = None

        try:
            tmp = yf.download(
                [fetch_symbol, benchmark_symbol],
                period=range_cfg["download_period"],
                interval="1d",
                auto_adjust=True,
                progress=False,
            )
            if not tmp.empty:
                if isinstance(tmp.columns, pd.MultiIndex):
                    if tmp.columns.names[0] != "Price":
                        tmp.columns = tmp.columns.swaplevel(0, 1)
                    tmp.columns.names = ["Price", "Ticker"]

                if "Close" in tmp and fetch_symbol in tmp["Close"].columns:
                    stock_df = pd.DataFrame({
                        "Close":  tmp["Close"][fetch_symbol],
                        "Open":   tmp["Open"][fetch_symbol]   if "Open"   in tmp else tmp["Close"][fetch_symbol],
                        "High":   tmp["High"][fetch_symbol]   if "High"   in tmp else tmp["Close"][fetch_symbol],
                        "Low":    tmp["Low"][fetch_symbol]    if "Low"    in tmp else tmp["Close"][fetch_symbol],
                        "Volume": tmp["Volume"][fetch_symbol] if "Volume" in tmp else 0,
                    })
                if "Close" in tmp and benchmark_symbol in tmp["Close"].columns:
                    bench_df = pd.DataFrame({"Close": tmp["Close"][benchmark_symbol]})
        except Exception as _yf_err:
            print(f"[chart_combined] yfinance fetch failed ({_yf_err}), trying cache…")

        # Fallback: SQLite cache when yfinance is unavailable / rate-limited
        if stock_df is None or stock_df.empty:
            try:
                cache = us_cache if market == "US" else ind_cache
                price_result, _ = cache.get_price_history_bulk(
                    [fetch_symbol, benchmark_symbol],
                    interval="1d",
                    lookback_days=760,
                )
                stock_df = price_result.get(fetch_symbol)
                bench_df = price_result.get(benchmark_symbol)
            except Exception as _cache_err:
                print(f"[chart_combined] Cache fallback also failed: {_cache_err}")

        if stock_df is None or stock_df.empty:
            return jsonify({"status": "error", "message": f"No data for '{symbol_clean}' — yfinance and cache both failed."}), 400

        # Normalise timezone (yfinance returns tz-aware; cache returns tz-naive)
        for df in [stock_df, bench_df]:
            if df is not None and getattr(df.index, "tz", None) is not None:
                df.index = df.index.tz_localize(None)

        volume_series = stock_df["Volume"] if "Volume" in stock_df.columns else pd.Series(dtype=float)
        bench_series  = bench_df["Close"]  if bench_df is not None        else pd.Series(dtype=float)

        combined = pd.DataFrame({
            "open":   stock_df["Open"]  if "Open"  in stock_df.columns else stock_df["Close"],
            "high":   stock_df["High"]  if "High"  in stock_df.columns else stock_df["Close"],
            "low":    stock_df["Low"]   if "Low"   in stock_df.columns else stock_df["Close"],
            "stock":  stock_df["Close"],
            "bench":  bench_series,
            "volume": volume_series,
        })

        combined = combined.dropna(subset=["stock", "open", "high", "low"])
        combined["bench"]  = combined["bench"].ffill().bfill()
        combined["volume"] = combined["volume"].fillna(0)
        combined = combined.sort_index()
        combined = combined[~combined.index.duplicated(keep='first')]

        if len(combined) < 200:
            return jsonify({"status": "error", "message": "Insufficient data to compile indicators."}), 400

        # ------------------------------------------------------------------
        # EMAs (10/20/50/100/200) — from the US chart
        # ------------------------------------------------------------------
        combined['ema10']  = calculate_ema(combined['stock'], 10)
        combined['ema20']  = calculate_ema(combined['stock'], 20)
        combined['ema50']  = calculate_ema(combined['stock'], 50)
        combined['ema100'] = calculate_ema(combined['stock'], 100)
        combined['ema200'] = calculate_ema(combined['stock'], 200)

        # ------------------------------------------------------------------
        # RS ratio + smoothing — from the US chart
        # Confirmed: 'bench' here is benchmark_symbol (^GSPC / S&P 500 for US,
        # ^CRSLDX / Nifty 500 for NSE), so rs_ratio is genuinely stock-vs-index,
        # not stock-vs-itself or any other accidental denominator.
        # ------------------------------------------------------------------
        combined['rs_ratio'] = combined['stock'] / combined['bench']
        combined['rs_sma10'] = calculate_sma(combined['rs_ratio'], 10)
        combined['rs_ema21'] = calculate_ema(combined['rs_ratio'], 21)
        combined['rs_sma50'] = calculate_sma(combined['rs_ratio'], 50)

        # ------------------------------------------------------------------
        # Divergence phase — from the US chart (rendered as background tint,
        # not its own pane, on the combined page)
        # ------------------------------------------------------------------
        combined['rs_slope']    = combined['rs_ratio'].rolling(window=5).apply(calculate_slope)
        combined['bench_slope'] = combined['bench'].rolling(window=5).apply(calculate_slope)

        def assign_divergence_strength(row):
            rs_m, sp_m = row['rs_slope'], row['bench_slope']
            if rs_m > 0 and sp_m < 0:
                return 2.0   # True Alpha Divergence
            elif rs_m > 0 and sp_m >= 0 and rs_m > sp_m:
                return 1.0   # Outperformance
            elif rs_m <= 0 and sp_m > 0:
                return -1.0  # Relative Underperformance
            elif rs_m < 0 and sp_m <= 0:
                return -2.0  # Flushing Phase
            return 0.0

        combined['div_strength'] = combined.apply(assign_divergence_strength, axis=1)

        # ------------------------------------------------------------------
        # Accumulation / Distribution (OBV-style) — from the NSE chart
        # ------------------------------------------------------------------
        combined['price_chg'] = combined['stock'].diff()
        combined['acc_vol'] = combined.apply(
            lambda r: r['volume'] if r['price_chg'] > 0
                      else (-r['volume'] if r['price_chg'] < 0 else 0),
            axis=1
        )
        combined['acc_line'] = combined['acc_vol'].cumsum()

        # RS uptrend marker (4 consecutive rising RS days) — from the NSE chart,
        # re-derived here against the US-style rs_ratio rather than NSE's cumulative rs_raw
        combined['rs_inc'] = combined['rs_ratio'].gt(combined['rs_ratio'].shift(1))
        combined['rs_up_flag'] = (
            combined['rs_inc'].rolling(window=4).sum()
            .apply(lambda x: 1 if x == 4 else 0)
            .fillna(0)
        )

        # ------------------------------------------------------------------
        # RS percentile from whichever market's screener cache applies
        # ------------------------------------------------------------------
        cached_rs_pct = 50
        results_json = cfg["results_json"]
        if os.path.exists(results_json):
            with open(results_json, 'r') as f:
                try:
                    for s in json.load(f).get('stocks', []):
                        if s['symbol'].strip().upper() == symbol_clean:
                            cached_rs_pct = int(s.get('rs_percentile', 50))
                            break
                except Exception:
                    pass

        # ------------------------------------------------------------------
        # Trim to requested display range
        # ------------------------------------------------------------------
        range_start = combined.index[-1] - pd.DateOffset(months=range_cfg["months"])
        display = combined[combined.index >= range_start]

        series_data = {
            "candles": [], "ema10": [], "ema20": [], "ema50": [], "ema100": [], "ema200": [],
            "rs_ratio": [], "rs_sma10": [], "rs_ema21": [], "rs_sma50": [],
            "acc_line": [], "div_hist": [], "rs_up_markers": [], "bench_line": []
        }

        for idx, row in display.iterrows():
            date_str = idx.strftime("%Y-%m-%d")

            series_data["candles"].append({
                "time": date_str, "open": round(float(row['open']), 2), "high": round(float(row['high']), 2),
                "low": round(float(row['low']), 2), "close": round(float(row['stock']), 2),
                "volume": int(row['volume']), "rs_pct": int(cached_rs_pct)
            })

            for key in ["ema10", "ema20", "ema50", "ema100", "ema200"]:
                series_data[key].append({"time": date_str, "value": round(float(row[key]), 2)})

            series_data["rs_ratio"].append({"time": date_str, "value": round(float(row['rs_ratio']), 6)})
            series_data["rs_sma10"].append({"time": date_str, "value": round(float(row['rs_sma10']), 6)})
            series_data["rs_ema21"].append({"time": date_str, "value": round(float(row['rs_ema21']), 6)})
            series_data["rs_sma50"].append({"time": date_str, "value": round(float(row['rs_sma50']), 6)})

            series_data["acc_line"].append({"time": date_str, "value": round(float(row['acc_line']), 0)})
            series_data["bench_line"].append({"time": date_str, "value": round(float(row['bench']), 2)})

            v_strength = row['div_strength']
            color = '#3B82F6' if v_strength == 2.0 else ('#60A5FA' if v_strength == 1.0 else ('#F87171' if v_strength == -1.0 else '#B91C1C'))
            series_data["div_hist"].append({"time": date_str, "value": float(v_strength), "color": color})

            if int(row['rs_up_flag']) == 1:
                series_data["rs_up_markers"].append({"time": date_str, "price": round(float(row['low']), 2)})

        # ── RS trend state (for toolbar badge and line colour ramp) ────────
        # Computed server-side so the client doesn't need to search back through
        # 500 data points to find the last N values.
        rs_vals     = [p["value"] for p in series_data["rs_ratio"] if p["value"] == p["value"]]
        rs_sma10_v  = [p["value"] for p in series_data["rs_sma10"] if p["value"] == p["value"]]
        rs_sma50_v  = [p["value"] for p in series_data["rs_sma50"] if p["value"] == p["value"]]
        rs_ema21_v  = [p["value"] for p in series_data["rs_ema21"] if p["value"] == p["value"]]

        rs_trend_state = "neutral"
        if len(rs_vals) >= 2 and len(rs_sma10_v) >= 1 and len(rs_ema21_v) >= 1:
            rs_now, rs_prev = rs_vals[-1], rs_vals[-2]
            rising = rs_now > rs_sma10_v[-1] and rs_sma10_v[-1] > rs_ema21_v[-1]
            falling = rs_now < rs_sma10_v[-1] and rs_sma10_v[-1] < rs_ema21_v[-1]
            if rising:   rs_trend_state = "rising"
            elif falling: rs_trend_state = "falling"

        # RS outperformance vs benchmark over the display range (3M window)
        rs_outperf_3m = None
        if len(rs_vals) >= 63:
            rs_outperf_3m = round((rs_vals[-1] / rs_vals[-63] - 1) * 100, 1)

        # Sector from screener cache
        cached_sector = ""
        if os.path.exists(results_json):
            try:
                with open(results_json) as f:
                    for s in json.load(f).get("stocks", []):
                        if s.get("symbol", "").strip().upper() == symbol_clean:
                            cached_sector = s.get("sector", "")
                            break
            except Exception:
                pass

        return jsonify({
            "status":        "success",
            "symbol":        symbol_clean,
            "market":        market,
            "range":         range_key,
            "rs_percentile": cached_rs_pct,
            "rs_trend_state": rs_trend_state,
            "rs_outperf_3m": rs_outperf_3m,
            "sector":        cached_sector,
            "series":        series_data,
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500