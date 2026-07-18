import os
import json
import glob
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, request, send_file
from werkzeug.utils import secure_filename
from datetime import datetime

ll_lh_bp = Blueprint("ll_lh_india", __name__)

# Absolute Path Ingestion & Variable Sync Configuration Matrix
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'india_lllh'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_india_lllh_results.json')
LAST_CSV_CONFIG = os.path.join(UPLOAD_FOLDER, 'last_csv_path.json')
HISTORY_CACHE_DIR = os.path.join(UPLOAD_FOLDER, 'history_cache')

os.makedirs(HISTORY_CACHE_DIR, exist_ok=True)

def get_latest_csv(directory):
    list_of_files = glob.glob(os.path.join(directory, '*.csv'))
    return max(list_of_files, key=os.path.getmtime) if list_of_files else None

def detect_ll_lh_stage3(symbol):
    try:
        yf_symbol = symbol if symbol.endswith(".NS") else f"{symbol}.NS"
        ticker = yf.Ticker(yf_symbol)
        df = ticker.history(period="1y")

        try:
            info = ticker.info
            sector = info.get('sector', 'N/A')
        except:
            sector = 'N/A'

        if df.empty or len(df) < 200:
            return None

        close = df['Close']
        curr_price = close.iloc[-1]
        ma200_series = close.rolling(window=200).mean()
        curr_ma200 = ma200_series.iloc[-1]
        
        # Stage 3/4 Baseline Filter: Stock must be breaking down below or near its long-term average
        if curr_price > (curr_ma200 * 1.05): 
            return None

        # Identify local peaks (Swing Highs) and troughs (Swing Lows)
        df['is_high'] = (df['High'] < df['High'].shift(1)) & (df['High'].shift(1) > df['High'].shift(2))
        df['is_low'] = (df['Low'] > df['Low'].shift(1)) & (df['Low'].shift(1) < df['Low'].shift(2))
        
        highs_df = df[df['is_high']]
        lows_df = df[df['is_low']]

        if len(highs_df) < 2 or len(lows_df) < 2: 
            return None

        last_sh = highs_df['High'].iloc[-1]
        prev_sh = highs_df['High'].iloc[-2]
        
        last_sl = lows_df['Low'].iloc[-1]
        prev_sl = lows_df['Low'].iloc[-2]

        # Bearish structural breakdown: Lower High (LH) AND Lower Low (LL)
        if last_sh < prev_sh and last_sl < prev_sl:
            high_52 = df['High'].max()
            retracement = round(((high_52 - curr_price) / high_52) * 100, 2)
            rs_score = round(curr_price / curr_ma200, 2)
            sl_percent = round(((last_sh - curr_price) / curr_price) * 100, 2)

            # Calculate 20-Day RS Trend Momentum Vector for shorting setups
            price_20d_ago = float(close.iloc[-20])
            ma200_20d = float(ma200_series.iloc[-20])
            rs_20d_ago = round(price_20d_ago / ma200_20d, 2)
            rs_change = round(((rs_score - rs_20d_ago) / rs_20d_ago) * 100, 1)

            if rs_change < -3.0:
                rs_trend = "⚠️ Collapsing"
            elif rs_change <= 0:
                rs_trend = "🔴 Fading"
            else:
                rs_trend = "🟢 Bouncing"

            return {
                "symbol": symbol,
                "sector": sector,
                "price": round(curr_price, 2),
                "swing_low": round(last_sl, 2),
                "last_swing_high": round(last_sh, 2),
                "sl_percent": sl_percent,
                "retracement": retracement,
                "rs": rs_score,
                "rs_trend": rs_trend,
                "rs_change": rs_change
            }
    except Exception as e:
        print(f"Error processing {symbol}: {e}")
        return None

@ll_lh_bp.route("/ll-lh-india", methods=["GET", "POST"])
def ll_lh_view():
    stocks = []
    summary_message = None
    last_run_time = None
    
    # Read persistent config schema
    last_file_name = "None"
    if os.path.exists(LAST_CSV_CONFIG):
        with open(LAST_CSV_CONFIG, 'r') as cf:
            last_file_name = json.load(cf).get("last_used_csv", "None")

    if request.method == "POST":
        file = request.files.get('file')
        filepath = get_latest_csv(UPLOAD_FOLDER)

        if file and file.filename != '':
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            last_file_name = filename
            with open(LAST_CSV_CONFIG, 'w') as cf:
                json.dump({"last_used_csv": last_file_name}, cf)

        if filepath and os.path.exists(filepath):
            try:
                df_input = pd.read_csv(filepath)
                df_input.columns = df_input.columns.str.strip().str.lower()
                symbols = df_input['symbol'].dropna().unique().tolist()
                
                for s in symbols:
                    res = detect_ll_lh_stage3(str(s).strip().upper())
                    if res: 
                        stocks.append(res)
                
                # Sort weakest stocks first (lowest RS score at top for shorting)
                stocks.sort(key=lambda x: x['rs'], reverse=False)
                last_run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                cache_payload = {
                    "last_run": last_run_time,
                    "stocks": stocks
                }
                with open(RESULTS_JSON, 'w') as f:
                    json.dump(cache_payload, f)
                    
                summary_message = f"✅ Bearish Scan Complete using {last_file_name}. Found {len(stocks)} breakdown candidates."
            except Exception as e:
                summary_message = f"❌ Error: {str(e)}"
    else:
        if os.path.exists(RESULTS_JSON):
            with open(RESULTS_JSON, 'r') as f:
                cached_data = json.load(f)
                if isinstance(cached_data, dict):
                    stocks = cached_data.get("stocks", [])
                    last_run_time = cached_data.get("last_run", "Unknown")
                else:
                    stocks = cached_data
                    last_run_time = "N/A"

    # Bearer Sector Breakdown and Dominance Distribution Parsing
    sector_counts = {}
    for s in stocks:
        sec = s.get('sector', 'N/A')
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    sorted_sectors = sorted(sector_counts.items(), key=lambda x: x[1], reverse=True)
    top_5 = [s[0] for s in sorted_sectors[:5] if s[0] != 'N/A']

    # Custom Bearish Rose/Crimson color configuration palette
    color_palette = [
        {"bg": "rgba(244, 63, 94, 0.15)", "text": "#f43f5e", "badge": "#f43f5e", "border": "rgba(244, 63, 94, 0.3)"},
        {"bg": "rgba(239, 68, 68, 0.15)", "text": "#f87171", "badge": "#ef4444", "border": "rgba(239, 68, 68, 0.3)"},
        {"bg": "rgba(245, 158, 11, 0.15)", "text": "#fbbf24", "badge": "#f59e0b", "border": "rgba(245, 158, 11, 0.3)"},
        {"bg": "rgba(168, 85, 247, 0.15)", "text": "#c084fc", "badge": "#a855f7", "border": "rgba(168, 85, 247, 0.3)"},
        {"bg": "rgba(100, 116, 139, 0.15)", "text": "#94a3b8", "badge": "#64748b", "border": "rgba(100, 116, 139, 0.3)"}
    ]

    top_sectors_meta = []
    sector_color_map = {}

    for idx, sec in enumerate(top_5):
        theme = color_palette[idx]
        count = sector_counts[sec]
        sector_color_map[sec] = theme
        top_sectors_meta.append({
            "name": sec,
            "count": count,
            "theme": theme
        })

    return render_template("ll_lh_india.html", 
                           stocks=stocks, 
                           summary_message=summary_message, 
                           last_file=last_file_name,
                           last_run_time=last_run_time,
                           top_sectors=top_sectors_meta,
                           sector_color_map=sector_color_map)

@ll_lh_bp.route("/export-lllh")
def export_lllh():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cached_data = json.load(f)
            data = cached_data.get("stocks", []) if isinstance(cached_data, dict) else cached_data
        df = pd.DataFrame(data)
        export_path = os.path.join(UPLOAD_FOLDER, 'lllh_export.csv')
        df.to_csv(export_path, index=False)
        return send_file(export_path, as_attachment=True)
    return "No dataset found to export.", 404