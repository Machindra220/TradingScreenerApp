import os
import json
import glob
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, request, send_file
from werkzeug.utils import secure_filename
from datetime import datetime

hh_hl_bp = Blueprint("hh_hl_india", __name__)

# UPLOAD_FOLDER = 'uploads/india_hhhl'
# UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'volar_ind'))
# os.makedirs(UPLOAD_FOLDER, exist_ok=True)

UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'india_hhhl'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_india_hhhl_results.json')
LAST_CSV_CONFIG = os.path.join(UPLOAD_FOLDER, 'last_csv_path.json')
HISTORY_CACHE_DIR = os.path.join(UPLOAD_FOLDER, 'history_cache')
os.makedirs(HISTORY_CACHE_DIR, exist_ok=True)

def get_latest_csv(directory):
    list_of_files = glob.glob(os.path.join(directory, '*.csv'))
    return max(list_of_files, key=os.path.getmtime) if list_of_files else None

def detect_hh_hl_stage2(symbol):
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
        ma200 = ma200_series.iloc[-1]
        
        if curr_price < ma200: # Stage 2 Baseline Filter
            return None

        df['is_low'] = (df['Low'] < df['Low'].shift(1)) & (df['Low'] < df['Low'].shift(2)) & \
                       (df['Low'] < df['Low'].shift(-1)) & (df['Low'] < df['Low'].shift(-2))
        
        lows_df = df[df['is_low']]
        if len(lows_df) < 2: 
            return None

        last_swing_low = lows_df['Low'].iloc[-1]
        prev_swing_low = lows_df['Low'].iloc[-2]

        if last_swing_low > prev_swing_low and curr_price > last_swing_low:
            high_52 = df['High'].max()
            retracement = round(((high_52 - curr_price) / high_52) * 100, 2)
            rs_score = round(curr_price / ma200, 2)
            sl_percent = round(((curr_price - last_swing_low) / curr_price) * 100, 2)

            # Calculate 20-Day RS Trend Momentum Vector
            price_20d_ago = float(close.iloc[-20])
            ma200_20d = float(ma200_series.iloc[-20])
            rs_20d_ago = round(price_20d_ago / ma200_20d, 2)
            rs_change = round(((rs_score - rs_20d_ago) / rs_20d_ago) * 100, 1)

            if rs_change > 3.0:
                rs_trend = "🚀 Accelerating"
            elif rs_change >= 0:
                rs_trend = "🟢 Steady"
            else:
                rs_trend = "⚠️ Fading"

            return {
                "symbol": symbol,
                "sector": sector,
                "price": round(curr_price, 2),
                "last_swing_low": round(last_swing_low, 2),
                "sl_percent": sl_percent,
                "retracement": retracement,
                "rs": rs_score,
                "rs_trend": rs_trend,
                "rs_change": rs_change
            }
    except Exception as e:
        print(f"Error processing {symbol}: {e}")
        return None

@hh_hl_bp.route("/hh-hl-india", methods=["GET", "POST"])
def hh_hl_view():
    stocks = []
    summary_message = None
    last_run_time = None
    results_path = os.path.join(UPLOAD_FOLDER, 'hhhl_results.json')
    
    latest_file_path = get_latest_csv(UPLOAD_FOLDER)
    last_file_name = os.path.basename(latest_file_path) if latest_file_path else "None"

    if request.method == "POST":
        file = request.files.get('file')
        filepath = latest_file_path

        if file and file.filename != '':
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            last_file_name = filename

        if filepath and os.path.exists(filepath):
            try:
                df_input = pd.read_csv(filepath)
                df_input.columns = df_input.columns.str.strip().str.lower()
                symbols = df_input['symbol'].dropna().unique().tolist()
                
                for s in symbols:
                    res = detect_hh_hl_stage2(str(s).strip().upper())
                    if res: 
                        stocks.append(res)
                
                stocks.sort(key=lambda x: x['rs'], reverse=True)
                last_run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                cache_payload = {
                    "last_run": last_run_time,
                    "stocks": stocks
                }
                with open(results_path, 'w') as f:
                    json.dump(cache_payload, f)
                    
                summary_message = f"✅ Scan Complete using {last_file_name}. Found {len(stocks)} stocks."
            except Exception as e:
                summary_message = f"❌ Error: {str(e)}"
    else:
        if os.path.exists(results_path):
            with open(results_path, 'r') as f:
                cached_data = json.load(f)
                if isinstance(cached_data, dict):
                    stocks = cached_data.get("stocks", [])
                    last_run_time = cached_data.get("last_run", "Unknown")
                else:
                    stocks = cached_data
                    last_run_time = "N/A"

    # Sector Breakdown and Dominance Distribution Parsing
    sector_counts = {}
    for s in stocks:
        sec = s.get('sector', 'N/A')
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    sorted_sectors = sorted(sector_counts.items(), key=lambda x: x[1], reverse=True)
    top_5 = [s[0] for s in sorted_sectors[:5] if s[0] != 'N/A']

    color_palette = [
        {"bg": "rgba(16, 185, 129, 0.15)", "text": "#34d399", "badge": "#10b981", "border": "rgba(16, 185, 129, 0.3)"},
        {"bg": "rgba(59, 130, 246, 0.15)", "text": "#60a5fa", "badge": "#3b82f6", "border": "rgba(59, 130, 246, 0.3)"},
        {"bg": "rgba(168, 85, 247, 0.15)", "text": "#c084fc", "badge": "#a855f7", "border": "rgba(168, 85, 247, 0.3)"},
        {"bg": "rgba(245, 158, 11, 0.15)", "text": "#fbbf24", "badge": "#f59e0b", "border": "rgba(245, 158, 11, 0.3)"},
        {"bg": "rgba(244, 63, 94, 0.15)", "text": "#f472b6", "badge": "#f43f5e", "border": "rgba(244, 63, 94, 0.3)"}
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

    return render_template("hh_hl_india.html", 
                           stocks=stocks, 
                           summary_message=summary_message, 
                           last_file=last_file_name,
                           last_run_time=last_run_time,
                           top_sectors=top_sectors_meta,
                           sector_color_map=sector_color_map)

@hh_hl_bp.route("/export-hhhl")
def export_hhhl():
    results_path = os.path.join(UPLOAD_FOLDER, 'hhhl_results.json')
    if os.path.exists(results_path):
        with open(results_path, 'r') as f:
            cached_data = json.load(f)
            data = cached_data.get("stocks", []) if isinstance(cached_data, dict) else cached_data
        df = pd.DataFrame(data)
        export_path = os.path.join(UPLOAD_FOLDER, 'hhhl_export.csv')
        df.to_csv(export_path, index=False)
        return send_file(export_path, as_attachment=True)
    return "No data to export", 404