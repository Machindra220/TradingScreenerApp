import os
import json
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Blueprint, render_template, jsonify, request

chart_us_bp = Blueprint("chart_engine_us", __name__)

UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'volar_us'))
RESULTS_JSON  = os.path.join(UPLOAD_FOLDER, 'last_volar_us_results.json')
BENCHMARK_SYMBOL = "^GSPC"

def calculate_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calculate_sma(series, window):
    return series.rolling(window=window).mean()

def calculate_slope(series, window=5):
    """Calculates the short-term directional slope using a rolling linear regression."""
    y = series.tail(window).values
    x = np.arange(len(y))
    if len(y) < window:
        return 0.0
    slope, _ = np.polyfit(x, y, 1)
    return slope

@chart_us_bp.route("/analytics-chart-us")
def chart_us_dashboard():
    default_stock = request.args.get("symbol", "NVDA")
    return render_template("chart_us.html", default_stock=default_stock)

# Maps the client-facing range selector to (display window, download period).
# Download period is always >= display window + ~1y of EMA-200 warm-up room.
RANGE_OPTIONS = {
    "3M": {"months": 3,  "download_period": "2y"},
    "6M": {"months": 6,  "download_period": "2y"},
    "1Y": {"months": 12, "download_period": "2y"},
    "2Y": {"months": 24, "download_period": "3y"},
}

@chart_us_bp.route("/api/v1/chart-telemetry-us/<symbol>")
def get_chart_telemetry_us(symbol):
    try:
        symbol_clean = symbol.strip().upper().replace(".NS", "").replace(".", "-")

        range_key = request.args.get("range", "1Y").strip().upper()
        if range_key not in RANGE_OPTIONS:
            range_key = "1Y"
        range_cfg = RANGE_OPTIONS[range_key]

        # Download enough history to allow the 200 EMA to warm up completely,
        # regardless of which display range the client asked for.
        data = yf.download(
            [symbol_clean, BENCHMARK_SYMBOL],
            period=range_cfg["download_period"],
            interval="1d",
            auto_adjust=True,
            progress=False
        )

        if data.empty:
            return jsonify({"status": "error", "message": f"No data returned for '{symbol_clean}'."}), 400

        # Enforce standard yfinance MultiIndex layout conventions
        if isinstance(data.columns, pd.MultiIndex):
            if data.columns.names[0] != 'Price':
                try: data.columns = data.columns.swaplevel(0, 1)
                except Exception: pass
            data.columns.names = ['Price', 'Ticker']

        if 'Close' not in data or symbol_clean not in data['Close'].columns:
            return jsonify({"status": "error", "message": f"Invalid US ticker '{symbol_clean}'."}), 400

        volume_series = data['Volume'][symbol_clean] if 'Volume' in data else pd.Series(dtype=float)

        combined = pd.DataFrame({
            "open":   data['Open'][symbol_clean],
            "high":   data['High'][symbol_clean],
            "low":    data['Low'][symbol_clean],
            "stock":  data['Close'][symbol_clean],
            "bench":  data['Close'][BENCHMARK_SYMBOL],
            "volume": volume_series
        })

        combined = combined.dropna(subset=["stock", "open", "high", "low"])
        combined["bench"]  = combined["bench"].ffill().bfill()
        combined["volume"] = combined["volume"].fillna(0)
        combined = combined.sort_index()
        combined = combined[~combined.index.duplicated(keep='first')]

        if len(combined) < 200:
            return jsonify({"status": "error", "message": "Insufficient data to compile indicators."}), 400

        # --- Indicator Calculations ---
        combined['ema10']  = calculate_ema(combined['stock'], 10)
        combined['ema20']  = calculate_ema(combined['stock'], 20)
        combined['ema50']  = calculate_ema(combined['stock'], 50)
        combined['ema100'] = calculate_ema(combined['stock'], 100)
        combined['ema200'] = calculate_ema(combined['stock'], 200)

        # 1. Base RS Ratio Line Metrics
        combined['rs_ratio'] = combined['stock'] / combined['bench']
        combined['rs_sma10'] = calculate_sma(combined['rs_ratio'], 10)
        combined['rs_ema21'] = calculate_ema(combined['rs_ratio'], 21)
        combined['rs_sma50'] = calculate_sma(combined['rs_ratio'], 50)

        # 2. RS Divergence Phase Matrix
        combined['rs_slope']    = combined['rs_ratio'].rolling(window=5).apply(calculate_slope)
        combined['bench_slope'] = combined['bench'].rolling(window=5).apply(calculate_slope)

        def assign_divergence_strength(row):
            rs_m = row['rs_slope']
            sp_m = row['bench_slope']
            
            # True Alpha Divergence (RS rises, S&P drops) -> Blue Bar
            if rs_m > 0 and sp_m < 0:
                return 2.0
            # Outperformance (Both rise, RS faster than S&P) -> Light Blue Bar
            elif rs_m > 0 and sp_m >= 0 and rs_m > sp_m:
                return 1.0
            # Relative Underperformance -> Light Red Bar
            elif rs_m <= 0 and sp_m > 0:
                return -1.0
            # Flushing Phase (Both indicators drop) -> Dark Red Bar
            elif rs_m < 0 and sp_m <= 0:
                return -2.0
            return 0.0

        combined['div_strength'] = combined.apply(assign_divergence_strength, axis=1)

        cached_rs_pct = 50
        if os.path.exists(RESULTS_JSON):
            with open(RESULTS_JSON, 'r') as f:
                try:
                    for s in json.load(f).get('stocks', []):
                        if s['symbol'].strip().upper() == symbol_clean:
                            cached_rs_pct = int(s.get('rs_percentile', 50))
                            break
                except Exception: pass

        # Trim outputs down to the requested display range to keep payloads lightweight
        range_start = combined.index[-1] - pd.DateOffset(months=range_cfg["months"])
        display = combined[combined.index >= range_start]

        series_data = {
            "candles": [], "ema10": [], "ema20": [], "ema50": [], "ema100": [], "ema200": [],
            "rs_ratio": [], "rs_sma10": [], "rs_ema21": [], "rs_sma50": [],
            "spx_line": [], "div_hist": []
        }

        for idx, row in display.iterrows():
            date_str = idx.strftime("%Y-%m-%d")

            series_data["candles"].append({
                "time": date_str, "open": round(float(row['open']), 2), "high": round(float(row['high']), 2),
                "low": round(float(row['low']), 2), "close": round(float(row['stock']), 2),
                "volume": int(row['volume']), "rs_pct": int(cached_rs_pct)
            })

            # Append core trend metrics
            for key in ["ema10", "ema20", "ema50", "ema100", "ema200"]:
                series_data[key].append({"time": date_str, "value": round(float(row[key]), 2)})

            # Append custom RS profile fields
            series_data["rs_ratio"].append({"time": date_str, "value": round(float(row['rs_ratio']), 6)})
            series_data["rs_sma10"].append({"time": date_str, "value": round(float(row['rs_sma10']), 6)})
            series_data["rs_ema21"].append({"time": date_str, "value": round(float(row['rs_ema21']), 6)})
            series_data["rs_sma50"].append({"time": date_str, "value": round(float(row['rs_sma50']), 6)})

            # Track benchmark index values
            series_data["spx_line"].append({"time": date_str, "value": round(float(row['bench']), 2)})

            # Map divergence strength metrics to standard hexadecimal color codes
            v_strength = row['div_strength']
            color = '#3B82F6' if v_strength == 2.0 else ('#60A5FA' if v_strength == 1.0 else ('#F87171' if v_strength == -1.0 else '#B91C1C'))
            series_data["div_hist"].append({"time": date_str, "value": float(v_strength), "color": color})

        return jsonify({
            "status": "success", "symbol": symbol_clean, "range": range_key,
            "rs_percentile": cached_rs_pct, "series": series_data
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500