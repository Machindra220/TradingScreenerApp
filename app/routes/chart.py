import os
import json
import pandas as pd
import yfinance as yf
from datetime import datetime, timezone
from flask import Blueprint, render_template, jsonify, request

chart_bp = Blueprint("chart_engine", __name__)

UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'rs_roc'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_rs_roc_results.json')

@chart_bp.route("/analytics-chart")
def chart_dashboard():
    """Renders the dedicated full-screen charting page"""
    default_stock = request.args.get("symbol", "PRAJIND")
    return render_template("chart.html", default_stock=default_stock)

@chart_bp.route("/api/v1/chart-telemetry/<symbol>")
def get_chart_telemetry(symbol):
    """Processes historical price arrays and extracts RS matrices against Nifty Index"""
    try:
        symbol_clean = symbol.strip().upper().replace(".NS", "")
        formatted_stock = f"{symbol_clean}.NS"
        benchmark_symbol = "^CRSLDX"

        # Download 1 year of historical data for both tickers together - "^CRSLDX" (NIFTY500) - "^NSEI" (NIFTY50)
        data = yf.download(
            [formatted_stock, benchmark_symbol],
            period="1y",
            interval="1d",
            auto_adjust=True,
            progress=False
        )

        if data.empty:
            return jsonify({"status": "error", "message": f"No data returned for '{symbol_clean}'."}), 400

        # FIX 1: yfinance >= 0.2.38 swapped MultiIndex level order to (ticker, metric).
        # Normalise by always swapping to (metric, ticker) if needed so the rest
        # of the code works regardless of installed version.
        if isinstance(data.columns, pd.MultiIndex):
            if data.columns.names[0] != 'Price':          # old style: (Price/metric, Ticker)
                # new style already has Ticker on level-0 — swap
                try:
                    data.columns = data.columns.swaplevel(0, 1)
                except Exception:
                    pass
            data.columns.names = ['Price', 'Ticker']      # normalise names

        if 'Close' not in data:
            return jsonify({"status": "error", "message": f"Close data missing for '{symbol_clean}'."}), 400

        if formatted_stock not in data['Close'].columns:
            return jsonify({"status": "error", "message": f"Invalid NSE Ticker '{symbol_clean}'. Please verify the symbol."}), 400

        # Extract per-ticker series from normalised MultiIndex
        open_series  = data['Open'][formatted_stock]
        high_series  = data['High'][formatted_stock]
        low_series   = data['Low'][formatted_stock]
        close_series = data['Close'][formatted_stock]
        bench_series = data['Close'][benchmark_symbol]

        combined = pd.DataFrame({
            "open":  open_series,
            "high":  high_series,
            "low":   low_series,
            "stock": close_series,
            "bench": bench_series
        })

        combined = combined.dropna(subset=["stock", "open", "high", "low"])
        combined["bench"] = combined["bench"].ffill().bfill()
        combined = combined.sort_index()
        combined = combined[~combined.index.duplicated(keep='first')]

        if len(combined) < 10:
            return jsonify({"status": "error", "message": "Insufficient historical data found."}), 400

        # Moving Averages
        combined['ema20']  = combined['stock'].ewm(span=20,  adjust=False).mean()
        combined['ema200'] = combined['stock'].ewm(span=200, adjust=False).mean()

        # Relative Strength
        stock_ret = combined['stock'].pct_change()
        bench_ret = combined['bench'].pct_change()
        combined['rs_raw'] = (stock_ret - bench_ret).cumsum().fillna(0)

        # RS rank from cache
        cached_rs_pct = 50
        if os.path.exists(RESULTS_JSON):
            with open(RESULTS_JSON, 'r') as f:
                try:
                    cached_data = json.load(f).get('stocks', [])
                    for s in cached_data:
                        if s['symbol'].strip().upper() == symbol_clean:
                            cached_rs_pct = int(s.get('rs_percentile', 50))
                            break
                except Exception:
                    pass

        # Rolling uptrend sequence
        combined['rs_inc'] = combined['rs_raw'].gt(combined['rs_raw'].shift(1))
        combined['green_underline'] = (
            combined['rs_inc'].rolling(window=4).sum()
            .apply(lambda x: 1 if x == 4 else 0)
            .fillna(0)
        )

        candles = []
        ema20_line = []
        ema200_line = []
        rs_line = []
        underline_markers = []

        for idx, row in combined.iterrows():
            # FIX 2: LightweightCharts expects time as 'YYYY-MM-DD' string (timezone-safe)
            # Using unix timestamps caused blank/shifted charts for IST because
            # the library interprets them as UTC and IST midnight != UTC midnight.
            date_str = idx.strftime("%Y-%m-%d")

            candles.append({
                "time":   date_str,
                "open":   round(float(row['open']),  2),
                "high":   round(float(row['high']),  2),
                "low":    round(float(row['low']),   2),
                "close":  round(float(row['stock']), 2),
                "rs_pct": int(cached_rs_pct)
            })

            ema20_line.append( {"time": date_str, "value": round(float(row['ema20']),  2)})
            ema200_line.append({"time": date_str, "value": round(float(row['ema200']), 2)})
            rs_line.append(    {"time": date_str, "value": round(float(row['rs_raw']), 6)})

            if int(row['green_underline']) == 1:
                underline_markers.append({"time": date_str, "value": round(float(row['rs_raw']), 6)})

        return jsonify({
            "status": "success",
            "symbol": symbol_clean,
            "rs_percentile": cached_rs_pct,
            "series": {
                "candles":   candles,
                "ema20":     ema20_line,
                "ema200":    ema200_line,
                "rs":        rs_line,
                "underline": underline_markers
            }
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500