import os
import json
import pandas as pd
import yfinance as yf
from datetime import datetime, timezone
from flask import Blueprint, render_template, jsonify, request

chart_bp = Blueprint("chart_engine", __name__)

UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'rs_roc'))
RESULTS_JSON  = os.path.join(UPLOAD_FOLDER, 'last_rs_roc_results.json')

# Nifty 500 ticker on yfinance
BENCHMARK_SYMBOL = "^CRSLDX"

@chart_bp.route("/analytics-chart")
def chart_dashboard():
    default_stock = request.args.get("symbol", "PRAJIND")
    return render_template("chart.html", default_stock=default_stock)


RANGE_OPTIONS = {
    # (download_period, yf_interval, display_bars)
    "1D":  {"period": "5d",   "interval": "1h",  "display_bars": 390},   # intraday hourly
    "1W":  {"period": "1mo",  "interval": "1h",  "display_bars": 168},   # 1 week hourly
    "1M":  {"period": "3mo",  "interval": "1d",  "display_bars": 22},    # 1 month daily
    "3M":  {"period": "1y",   "interval": "1d",  "display_bars": 66},
    "6M":  {"period": "2y",   "interval": "1d",  "display_bars": 132},
    "1Y":  {"period": "2y",   "interval": "1d",  "display_bars": 252},
    "2Y":  {"period": "3y",   "interval": "1d",  "display_bars": 504},
    "1W_TF": {"period": "5y", "interval": "1wk", "display_bars": 52},    # weekly candles 1Y
    "2W_TF": {"period": "5y", "interval": "1wk", "display_bars": 104},   # weekly candles 2Y
}


@chart_bp.route("/api/v1/chart-telemetry/<symbol>")
def get_chart_telemetry(symbol):
    try:
        symbol_clean    = symbol.strip().upper().replace(".NS", "")
        formatted_stock = f"{symbol_clean}.NS"

        range_key = request.args.get("range", "1Y").strip().upper()
        if range_key not in RANGE_OPTIONS:
            range_key = "1Y"
        cfg      = RANGE_OPTIONS[range_key]
        interval = cfg["interval"]

        # ------------------------------------------------------------------
        # 1. Download price + volume data
        # ------------------------------------------------------------------
        data = yf.download(
            [formatted_stock, BENCHMARK_SYMBOL],
            period=cfg["period"],
            interval=interval,
            auto_adjust=True,
            progress=False
        )

        if data.empty:
            return jsonify({"status": "error", "message": f"No data returned for '{symbol_clean}'."}), 400

        # Normalise MultiIndex: always (Price, Ticker) regardless of yfinance version
        if isinstance(data.columns, pd.MultiIndex):
            if data.columns.names[0] != 'Price':
                try:
                    data.columns = data.columns.swaplevel(0, 1)
                except Exception:
                    pass
            data.columns.names = ['Price', 'Ticker']

        if 'Close' not in data:
            return jsonify({"status": "error", "message": "Close data missing."}), 400
        if formatted_stock not in data['Close'].columns:
            return jsonify({"status": "error", "message": f"Invalid NSE ticker '{symbol_clean}'."}), 400

        # ------------------------------------------------------------------
        # 2. Build combined DataFrame
        # ------------------------------------------------------------------
        volume_series = data['Volume'][formatted_stock] if 'Volume' in data else pd.Series(dtype=float)

        combined = pd.DataFrame({
            "open":   data['Open'][formatted_stock],
            "high":   data['High'][formatted_stock],
            "low":    data['Low'][formatted_stock],
            "stock":  data['Close'][formatted_stock],
            "bench":  data['Close'][BENCHMARK_SYMBOL],
            "volume": volume_series
        })

        combined = combined.dropna(subset=["stock", "open", "high", "low"])
        combined["bench"]  = combined["bench"].ffill().bfill()
        combined["volume"] = combined["volume"].fillna(0)
        combined = combined.sort_index()
        combined = combined[~combined.index.duplicated(keep='first')]

        if len(combined) < 20:
            return jsonify({"status": "error", "message": "Insufficient historical data."}), 400

        # ------------------------------------------------------------------
        # 3. EMAs  (20, 50, 200)
        # ------------------------------------------------------------------
        combined['ema20']  = combined['stock'].ewm(span=20,  adjust=False).mean()
        combined['ema50']  = combined['stock'].ewm(span=50,  adjust=False).mean()
        combined['ema200'] = combined['stock'].ewm(span=200, adjust=False).mean()

        # ------------------------------------------------------------------
        # 4. Relative Strength vs Nifty 500 (cumulative excess return)
        # ------------------------------------------------------------------
        stock_ret          = combined['stock'].pct_change()
        bench_ret          = combined['bench'].pct_change()
        combined['rs_raw'] = (stock_ret - bench_ret).cumsum().fillna(0)

        # RS uptrend: 4 consecutive rising RS days
        combined['rs_inc']         = combined['rs_raw'].gt(combined['rs_raw'].shift(1))
        combined['green_underline'] = (
            combined['rs_inc'].rolling(window=4).sum()
            .apply(lambda x: 1 if x == 4 else 0)
            .fillna(0)
        )

        # ------------------------------------------------------------------
        # 5. Accumulation / Distribution (OBV-style)
        #    Up-day  → +volume ; Down-day → -volume ; Flat → 0
        #    Cumulative sum = net buying pressure over time
        # ------------------------------------------------------------------
        combined['price_chg'] = combined['stock'].diff()
        combined['acc_vol']   = combined.apply(
            lambda r: r['volume'] if r['price_chg'] > 0
                      else (-r['volume'] if r['price_chg'] < 0 else 0),
            axis=1
        )
        combined['acc_line'] = combined['acc_vol'].cumsum()

        # ------------------------------------------------------------------
        # 6. RS %tile from screener cache
        # ------------------------------------------------------------------
        cached_rs_pct = 50
        if os.path.exists(RESULTS_JSON):
            with open(RESULTS_JSON, 'r') as f:
                try:
                    for s in json.load(f).get('stocks', []):
                        if s['symbol'].strip().upper() == symbol_clean:
                            cached_rs_pct = int(s.get('rs_percentile', 50))
                            break
                except Exception:
                    pass

        # Volume 20-bar SMA — for dry-up detection in frontend
        combined['vol_sma20'] = combined['volume'].rolling(window=20).mean()

        # ------------------------------------------------------------------
        # 7. Serialise — trim to requested display_bars
        # ------------------------------------------------------------------
        display_bars = cfg["display_bars"]
        display      = combined.iloc[-display_bars:] if len(combined) > display_bars else combined

        candles         = []
        ema20_line      = []
        ema50_line      = []
        ema200_line     = []
        rs_line         = []
        rs_up_markers   = []   # green dot markers on PRICE chart (candle series)
        acc_line        = []

        for idx, row in display.iterrows():
            date_str = idx.strftime("%Y-%m-%d")
            rs_val   = round(float(row['rs_raw']), 6)

            candles.append({
                "time":      date_str,
                "open":      round(float(row['open']),   2),
                "high":      round(float(row['high']),   2),
                "low":       round(float(row['low']),    2),
                "close":     round(float(row['stock']),  2),
                "volume":    int(row['volume']),
                "vol_sma20": round(float(row['vol_sma20']), 0) if not pd.isna(row['vol_sma20']) else 0,
                "rs_pct":    int(cached_rs_pct),
                "rs_val":    rs_val,
            })

            ema20_line.append( {"time": date_str, "value": round(float(row['ema20']),  2)})
            ema50_line.append( {"time": date_str, "value": round(float(row['ema50']),  2)})
            ema200_line.append({"time": date_str, "value": round(float(row['ema200']), 2)})
            rs_line.append(    {"time": date_str, "value": rs_val})
            acc_line.append(   {"time": date_str, "value": round(float(row['acc_line']), 0)})

            # RS uptrend marker — goes on price candles so it's always visible
            if int(row['green_underline']) == 1:
                rs_up_markers.append({"time": date_str, "price": round(float(row['low']), 2)})

        return jsonify({
            "status":        "success",
            "symbol":        symbol_clean,
            "range":         range_key,
            "interval":      interval,
            "rs_percentile": cached_rs_pct,
            "series": {
                "candles":    candles,
                "ema20":      ema20_line,
                "ema50":      ema50_line,
                "ema200":     ema200_line,
                "rs":         rs_line,
                "rs_up":      rs_up_markers,
                "acc":        acc_line
            }
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500