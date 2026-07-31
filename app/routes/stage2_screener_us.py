import os
import glob
import json
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, request
from werkzeug.utils import secure_filename
from datetime import datetime, date, timedelta
from app.extensions import db
from app.models import Stage2Stock
from sqlalchemy import func

screener_us_bp = Blueprint("stage2_screener_us", __name__)

UPLOAD_FOLDER = 'uploads/us_screener'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def get_latest_file(directory):
    list_of_files = glob.glob(os.path.join(directory, '*'))
    list_of_files = [f for f in list_of_files if not f.endswith('.json')]
    return max(list_of_files, key=os.path.getmtime) if list_of_files else None

def is_minervini_stage2(symbol):
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="2y")
        
        try:
            info = ticker.info
            sector = info.get('sector', 'N/A')
        except:
            sector = 'N/A'

        if df.empty or len(df) < 200: 
            return None

        close = df['Close']
        vol = df['Volume']
        ma50 = close.rolling(window=50).mean()
        ma150 = close.rolling(window=150).mean()
        ma200 = close.rolling(window=200).mean()
        vol_avg = vol.rolling(window=50).mean()
        
        curr_price = float(close.iloc[-1])
        curr_ma50 = float(ma50.iloc[-1])
        curr_ma150 = float(ma150.iloc[-1])
        curr_ma200 = float(ma200.iloc[-1])
        curr_vol = int(vol.iloc[-1])
        curr_vol_avg = int(vol_avg.iloc[-1])
        ma200_20d_ago = float(ma200.iloc[-22])
        
        low_52wk = float(df['Low'].tail(252).min())
        high_52wk = float(df['High'].tail(252).max())

        cond_1 = curr_price > curr_ma150 and curr_price > curr_ma200
        cond_2 = curr_ma150 > curr_ma200
        cond_3 = curr_ma200 > ma200_20d_ago
        cond_4 = curr_ma50 > curr_ma150 and curr_ma50 > curr_ma200
        cond_5 = curr_price > (low_52wk * 1.30)
        cond_6 = curr_price >= (high_52wk * 0.75)

        if all([cond_1, cond_2, cond_3, cond_4, cond_5, cond_6]):
            rs_score = round(curr_price / curr_ma200, 2)
            
            # Calculate 20-Day RS Trend (RS Momentum)
            price_20d_ago = float(close.iloc[-20])
            ma200_20d = float(ma200.iloc[-20])
            rs_20d_ago = round(price_20d_ago / ma200_20d, 2)
            rs_change = round(((rs_score - rs_20d_ago) / rs_20d_ago) * 100, 1)

            if rs_change > 3.0:
                rs_trend = "🚀 Accelerating"
            elif rs_change >= 0:
                rs_trend = "🟢 Steady"
            else:
                rs_trend = "⚠️ Fading"

            retracement = round(((high_52wk - curr_price) / high_52wk) * 100, 2)
            
            return {
                "symbol": symbol,
                "sector": sector,
                "price": round(curr_price, 2),
                "retracement": retracement,
                "volume": curr_vol,
                "vol_avg": curr_vol_avg,
                "vol_status": "🔥" if curr_vol > curr_vol_avg else "Normal",
                "rs": rs_score,
                "rs_trend": rs_trend,
                "rs_change": rs_change,
                "ma50": round(curr_ma50, 2),
                "ma200": round(curr_ma200, 2)
            }
    except Exception as e:
        print(f"Error screening {symbol}: {e}")
    return None

@screener_us_bp.route("/stage2-us", methods=["GET", "POST"])
def stage2_us_view():
    stocks = []
    summary_message = None
    last_file = None
    last_run_time = None
    results_path = os.path.join(UPLOAD_FOLDER, 'cached_results.json')
    
    latest_file_path = get_latest_file(UPLOAD_FOLDER)
    if latest_file_path:
        last_file = os.path.basename(latest_file_path)

    if request.method == "POST":
        file = request.files.get('file')
        filepath = latest_file_path 
        
        if file and file.filename != '':
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            last_file = filename

        if filepath and os.path.exists(filepath):
            try:
                df_input = pd.read_excel(filepath) if filepath.endswith('.xlsx') else pd.read_csv(filepath)
                df_input.columns = df_input.columns.str.strip().str.lower()
                
                if 'symbol' in df_input.columns:
                    raw_symbols = df_input['symbol'].dropna().unique().tolist()
                    symbols = [str(s).strip().upper().replace('.', '-') for s in raw_symbols]
                    
                    cutoff = date.today() - timedelta(days=30)
                    counts = db.session.query(Stage2Stock.symbol, func.count(Stage2Stock.date)).filter(Stage2Stock.date >= cutoff).group_by(Stage2Stock.symbol).all()
                    presence_map = {s: c for s, c in counts}

                    for s in symbols:
                        res = is_minervini_stage2(s)
                        if res:
                            days = presence_map.get(res['symbol'], 0)
                            res['persistence'] = f"{days}D"
                            stocks.append(res)
                    
                    stocks.sort(key=lambda x: x['rs'], reverse=True)
                    last_run_time = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
                    
                    cache_payload = {
                        "last_run": last_run_time,
                        "stocks": stocks
                    }
                    with open(results_path, 'w') as f:
                        json.dump(cache_payload, f)
                        
                    summary_message = f"✅ Analysis Complete. Found {len(stocks)} stocks."
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
            summary_message = "Showing results from last run."

    # Top 5 Sectors Logic
    sector_counts = {}
    for s in stocks:
        sec = s.get('sector', 'N/A')
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    sorted_sectors = sorted(sector_counts.items(), key=lambda x: x[1], reverse=True)
    top_5 = [s[0] for s in sorted_sectors[:5] if s[0] != 'N/A']

    # Explicit Style Themes
    color_palette = [
        {"bg": "#d1fae5", "text": "#065f46", "badge": "#10b981", "border": "#a7f3d0", "name": "emerald"},
        {"bg": "#dbeafe", "text": "#1e40af", "badge": "#3b82f6", "border": "#bfdbfe", "name": "blue"},
        {"bg": "#f3e8ff", "text": "#6b21a8", "badge": "#a855f7", "border": "#e9d5ff", "name": "purple"},
        {"bg": "#fef3c7", "text": "#92400e", "badge": "#f59e0b", "border": "#fde68a", "name": "amber"},
        {"bg": "#ffe4e6", "text": "#9f1239", "badge": "#f43f5e", "border": "#fecdd3", "name": "rose"}
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

    return render_template("stage2_screener_us.html", 
                           stocks=stocks, 
                           last_file=last_file, 
                           last_run_time=last_run_time,
                           top_sectors=top_sectors_meta,
                           sector_color_map=sector_color_map,
                           summary_message=summary_message)