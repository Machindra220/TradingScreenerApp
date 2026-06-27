import os
import json
import numpy as np
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, jsonify, request

chart_combined_bp = Blueprint("chart_engine_combined", __name__)

# Cache files reused from the existing per-market screeners — combined chart
# doesn't compute its own RS percentile, it reads whichever cache matches
# the requested market so the number on screen always matches the screener.
US_UPLOAD_FOLDER  = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'volar_us'))
US_RESULTS_JSON   = os.path.join(US_UPLOAD_FOLDER, 'last_volar_us_results.json')
NSE_UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'rs_roc'))
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

        data = yf.download(
            [fetch_symbol, benchmark_symbol],
            period=range_cfg["download_period"],
            interval="1d",
            auto_adjust=True,
            progress=False
        )

        if data.empty:
            return jsonify({"status": "error", "message": f"No data returned for '{symbol_clean}'."}), 400

        if isinstance(data.columns, pd.MultiIndex):
            if data.columns.names[0] != 'Price':
                try:
                    data.columns = data.columns.swaplevel(0, 1)
                except Exception:
                    pass
            data.columns.names = ['Price', 'Ticker']

        if 'Close' not in data or fetch_symbol not in data['Close'].columns:
            return jsonify({"status": "error", "message": f"Invalid {market} ticker '{symbol_clean}'."}), 400

        volume_series = data['Volume'][fetch_symbol] if 'Volume' in data else pd.Series(dtype=float)

        combined = pd.DataFrame({
            "open":   data['Open'][fetch_symbol],
            "high":   data['High'][fetch_symbol],
            "low":    data['Low'][fetch_symbol],
            "stock":  data['Close'][fetch_symbol],
            "bench":  data['Close'][benchmark_symbol],
            "volume": volume_series
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
            "acc_line": [], "div_hist": [], "rs_up_markers": []
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

            v_strength = row['div_strength']
            color = '#3B82F6' if v_strength == 2.0 else ('#60A5FA' if v_strength == 1.0 else ('#F87171' if v_strength == -1.0 else '#B91C1C'))
            series_data["div_hist"].append({"time": date_str, "value": float(v_strength), "color": color})

            if int(row['rs_up_flag']) == 1:
                series_data["rs_up_markers"].append({"time": date_str, "price": round(float(row['low']), 2)})

        return jsonify({
            "status": "success", "symbol": symbol_clean, "market": market, "range": range_key,
            "rs_percentile": cached_rs_pct, "series": series_data
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500