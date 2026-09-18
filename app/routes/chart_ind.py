import os
import json
import numpy as np
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, jsonify, request

chart_ind_bp = Blueprint("chart_engine_ind", __name__)

_PROJECT_ROOT    = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
NSE_UPLOAD_FOLDER = os.path.join(_PROJECT_ROOT, 'uploads', 'rs_roc')
RESULTS_JSON      = os.path.join(NSE_UPLOAD_FOLDER, 'last_rs_roc_results.json')
BENCHMARK_SYMBOL  = "^CRSLDX"   # Nifty 500 Total Return Index
BENCHMARK_LABEL   = "Nifty 500"


def calculate_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calculate_sma(series, window):
    return series.rolling(window=window).mean()

def calculate_slope(series, window=5):
    y = series.tail(window).values
    x = np.arange(len(y))
    if len(y) < window:
        return 0.0
    slope, _ = np.polyfit(x, y, 1)
    return slope


RANGE_OPTIONS = {
    "3M": {"months": 3,  "download_period": "2y"},
    "6M": {"months": 6,  "download_period": "2y"},
    "1Y": {"months": 12, "download_period": "2y"},
    "2Y": {"months": 24, "download_period": "3y"},
}


@chart_ind_bp.route("/analytics-chart-ind")
def chart_ind_dashboard():
    default_stock = request.args.get("symbol", "RELIANCE")
    return render_template("chart_ind.html", default_stock=default_stock)


@chart_ind_bp.route("/api/v1/chart-telemetry-ind/<symbol>")
def get_chart_telemetry_ind(symbol):
    try:
        # Normalise: strip .NS, add it back for yfinance fetch
        symbol_clean = symbol.strip().upper().replace(".NS", "").replace(".", "-")
        fetch_symbol = f"{symbol_clean}.NS"

        range_key = request.args.get("range", "1Y").strip().upper()
        if range_key not in RANGE_OPTIONS:
            range_key = "1Y"
        range_cfg = RANGE_OPTIONS[range_key]

        data = yf.download(
            [fetch_symbol, BENCHMARK_SYMBOL],
            period=range_cfg["download_period"],
            interval="1d",
            auto_adjust=True,
            progress=False
        )

        if data.empty:
            return jsonify({"status": "error",
                            "message": f"No data for '{symbol_clean}'. Check the NSE ticker."}), 400

        # Enforce standard MultiIndex layout
        if isinstance(data.columns, pd.MultiIndex):
            if data.columns.names[0] != 'Price':
                try:
                    data.columns = data.columns.swaplevel(0, 1)
                except Exception:
                    pass
            data.columns.names = ['Price', 'Ticker']

        if 'Close' not in data or fetch_symbol not in data['Close'].columns:
            return jsonify({"status": "error",
                            "message": f"Invalid NSE ticker '{symbol_clean}'. Try without .NS suffix."}), 400

        volume_series = (data['Volume'][fetch_symbol]
                         if 'Volume' in data else pd.Series(dtype=float))

        combined = pd.DataFrame({
            "open":   data['Open'][fetch_symbol],
            "high":   data['High'][fetch_symbol],
            "low":    data['Low'][fetch_symbol],
            "stock":  data['Close'][fetch_symbol],
            "bench":  data['Close'][BENCHMARK_SYMBOL],
            "volume": volume_series,
        })

        combined = combined.dropna(subset=["stock", "open", "high", "low"])
        combined["bench"]  = combined["bench"].ffill().bfill()
        combined["volume"] = combined["volume"].fillna(0)
        combined = combined.sort_index()
        combined = combined[~combined.index.duplicated(keep='first')]

        if len(combined) < 200:
            return jsonify({"status": "error",
                            "message": "Insufficient history to compute indicators (need 200+ days)."}), 400

        # ── Indicators ────────────────────────────────────────────────────────
        combined['ema10']  = calculate_ema(combined['stock'], 10)
        combined['ema20']  = calculate_ema(combined['stock'], 20)
        combined['ema50']  = calculate_ema(combined['stock'], 50)
        combined['ema100'] = calculate_ema(combined['stock'], 100)
        combined['ema200'] = calculate_ema(combined['stock'], 200)

        # RS Ratio vs Nifty 500
        combined['rs_ratio'] = combined['stock'] / combined['bench']
        combined['rs_sma10'] = calculate_sma(combined['rs_ratio'], 10)
        combined['rs_ema21'] = calculate_ema(combined['rs_ratio'], 21)
        combined['rs_sma50'] = calculate_sma(combined['rs_ratio'], 50)

        # RS Divergence Phase Matrix
        combined['rs_slope']    = combined['rs_ratio'].rolling(window=5).apply(calculate_slope)
        combined['bench_slope'] = combined['bench'].rolling(window=5).apply(calculate_slope)

        def assign_divergence_strength(row):
            rs_m = row['rs_slope']
            bm_m = row['bench_slope']
            if rs_m > 0 and bm_m < 0:              return  2.0   # True Alpha
            elif rs_m > 0 and bm_m >= 0 and rs_m > bm_m: return  1.0   # Outperformance
            elif rs_m <= 0 and bm_m > 0:            return -1.0   # Relative weakness
            elif rs_m < 0 and bm_m <= 0:            return -2.0   # Flushing
            return 0.0

        combined['div_strength'] = combined.apply(assign_divergence_strength, axis=1)

        # RS 3-day rising flag — computed BEFORE slicing to avoid KeyError
        combined['rs_inc']   = combined['rs_ratio'] > combined['rs_ratio'].shift(1)
        combined['rs_up_3d'] = (
            combined['rs_inc'].rolling(window=3).sum()
            .apply(lambda x: 1 if x == 3 else 0)
            .fillna(0)
        )

        # ── RS percentile from screener cache ─────────────────────────────────
        cached_rs_pct = 50
        cached_sector = ""
        if os.path.exists(RESULTS_JSON):
            try:
                with open(RESULTS_JSON) as f:
                    for s in json.load(f).get('stocks', []):
                        sym_cache = s.get('symbol', '').strip().upper().replace('.NS', '')
                        if sym_cache == symbol_clean:
                            cached_rs_pct = int(s.get('rs_percentile', 50))
                            cached_sector = s.get('sector', '')
                            break
            except Exception:
                pass

        # ── Trim to display range ─────────────────────────────────────────────
        range_start = combined.index[-1] - pd.DateOffset(months=range_cfg["months"])
        display = combined[combined.index >= range_start]

        series_data = {
            "candles": [], "ema10": [], "ema20": [], "ema50": [], "ema100": [], "ema200": [],
            "rs_ratio": [], "rs_sma10": [], "rs_ema21": [], "rs_sma50": [],
            "bench_line": [], "div_hist": [], "rs_up_markers": []
        }

        for idx, row in display.iterrows():
            date_str = idx.strftime("%Y-%m-%d")

            series_data["candles"].append({
                "time": date_str,
                "open":   round(float(row['open']),   2),
                "high":   round(float(row['high']),   2),
                "low":    round(float(row['low']),    2),
                "close":  round(float(row['stock']),  2),
                "volume": int(row['volume']),
                "rs_pct": int(cached_rs_pct),
            })

            for key in ["ema10", "ema20", "ema50", "ema100", "ema200"]:
                series_data[key].append({"time": date_str, "value": round(float(row[key]), 2)})

            series_data["rs_ratio"].append({"time": date_str, "value": round(float(row['rs_ratio']), 6)})
            series_data["rs_sma10"].append({"time": date_str, "value": round(float(row['rs_sma10']), 6)})
            series_data["rs_ema21"].append({"time": date_str, "value": round(float(row['rs_ema21']), 6)})
            series_data["rs_sma50"].append({"time": date_str, "value": round(float(row['rs_sma50']), 6)})
            series_data["bench_line"].append({"time": date_str, "value": round(float(row['bench']), 2)})

            v = row['div_strength']
            color = ('#3B82F6' if v == 2.0 else
                     '#60A5FA' if v == 1.0 else
                     '#F87171' if v == -1.0 else '#B91C1C')
            series_data["div_hist"].append({"time": date_str, "value": float(v), "color": color})

            if int(row['rs_up_3d']) == 1:
                series_data["rs_up_markers"].append({
                    "time":  date_str,
                    "price": round(float(row['low']), 2),
                })

        # ── RS trend state ────────────────────────────────────────────────────
        rs_vals    = [p["value"] for p in series_data["rs_ratio"]]
        rs_sma10_v = [p["value"] for p in series_data["rs_sma10"]]
        rs_ema21_v = [p["value"] for p in series_data["rs_ema21"]]

        rs_trend_state = "neutral"
        if len(rs_vals) >= 2 and rs_sma10_v and rs_ema21_v:
            rising  = rs_vals[-1] > rs_sma10_v[-1] and rs_sma10_v[-1] > rs_ema21_v[-1]
            falling = rs_vals[-1] < rs_sma10_v[-1] and rs_sma10_v[-1] < rs_ema21_v[-1]
            if rising:    rs_trend_state = "rising"
            elif falling: rs_trend_state = "falling"

        rs_outperf_3m = None
        if len(rs_vals) >= 63:
            rs_outperf_3m = round((rs_vals[-1] / rs_vals[-63] - 1) * 100, 1)

        return jsonify({
            "status":         "success",
            "symbol":         symbol_clean,
            "range":          range_key,
            "rs_percentile":  cached_rs_pct,
            "rs_trend_state": rs_trend_state,
            "rs_outperf_3m":  rs_outperf_3m,
            "sector":         cached_sector,
            "benchmark_label": BENCHMARK_LABEL,
            "series":         series_data,
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500