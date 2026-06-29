import os
import glob
import json
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, request, session
from werkzeug.utils import secure_filename
from datetime import datetime, date, timedelta
from app.extensions import db
from app.models import Stage2Stock
from sqlalchemy import func

screener_india_bp = Blueprint("stage2_india", __name__)

# --- PATH COMPARTMENTALIZATION & CACHE CHANNELS ---
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'india_screener'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_stage2_india_results.json')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def get_latest_file(directory):
    list_of_files = glob.glob(os.path.join(directory, '*'))
    # Filter out your JSON state cache to prevent scanning it as a spreadsheet
    valid_files = [f for f in list_of_files if not f.endswith('.json')]
    return max(valid_files, key=os.path.getmtime) if valid_files else None

def is_minervini_stage2_india(symbol):
    try:
        yf_symbol = symbol if symbol.endswith(".NS") else f"{symbol}.NS"
        ticker = yf.Ticker(yf_symbol)
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

        # Stage 2 Criteria
        cond_1 = curr_price > curr_ma150 and curr_price > curr_ma200
        cond_2 = curr_ma150 > curr_ma200
        cond_3 = curr_ma200 > ma200_20d_ago
        cond_4 = curr_ma50 > curr_ma150 and curr_ma50 > curr_ma200
        cond_5 = curr_price > (low_52wk * 1.30)
        cond_6 = curr_price >= (high_52wk * 0.75)

        if all([cond_1, cond_2, cond_3, cond_4, cond_5, cond_6]):
            rs_score = round(curr_price / curr_ma200, 2)
            retracement = round(((high_52wk - curr_price) / high_52wk) * 100, 2)
            
            return {
                "symbol": symbol.replace(".NS", ""),
                "sector": sector,
                "price": round(curr_price, 2),
                "retracement": retracement,
                "volume": curr_vol,
                "vol_avg": curr_vol_avg,
                "vol_status": "🔥" if curr_vol > curr_vol_avg else "正常",
                "rs": rs_score,
                "ma50": round(curr_ma50, 2),
                "ma200": round(curr_ma200, 2)
            }
    except Exception as e:
        print(f"Error screening {symbol}: {e}")
    return None

@screener_india_bp.route("/stage2-india", methods=["GET", "POST"])
def stage2_india_view():
    stocks = []
    summary_message = None
    last_file = session.get('stage2_last_uploaded_filename_ind')
    
    # Check if a persistent database layout slice already exists on disk cache
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            try:
                cache = json.load(f)
                stocks = cache.get('stocks', [])
                summary_message = cache.get('summary_message')
                if not last_file:
                    last_file = cache.get('last_file')
            except Exception:
                pass

    if request.method == "POST":
        file = request.files.get('file')
        filepath = session.get('stage2_last_uploaded_path_ind')
        
        # Save file to server if a new attachment is uploaded
        if file and file.filename != '':
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            last_file = filename
            session['stage2_last_uploaded_path_ind'] = filepath
            session['stage2_last_uploaded_filename_ind'] = filename
            session.modified = True
        elif not filepath or not os.path.exists(filepath):
            # Fallback to verify directory layout directly if your session cleared
            latest_file_path = get_latest_file(UPLOAD_FOLDER)
            if latest_file_path:
                filepath = latest_file_path
                last_file = os.path.basename(latest_file_path)

        if filepath and os.path.exists(filepath):
            try:
                # Initialize working environment rows
                stocks = []
                df_input = pd.read_excel(filepath) if filepath.endswith('.xlsx') else pd.read_csv(filepath)
                df_input.columns = df_input.columns.str.strip().str.lower()
                
                if 'symbol' in df_input.columns:
                    raw_symbols = df_input['symbol'].dropna().unique().tolist()
                    
                    cutoff = date.today() - timedelta(days=30)
                    counts = db.session.query(
                        Stage2Stock.symbol, 
                        func.count(Stage2Stock.date)
                    ).filter(Stage2Stock.date >= cutoff).group_by(Stage2Stock.symbol).all()
                    presence_map = {s: c for s, c in counts}

                    for s in raw_symbols:
                        res = is_minervini_stage2_india(str(s).strip().upper())
                        if res:
                            db_symbol = f"{res['symbol']}.NS"
                            days = presence_map.get(db_symbol, 0)
                            res['persistence'] = f"{days}D"
                            stocks.append(res)
                    
                    stocks.sort(key=lambda x: x['rs'], reverse=True)
                    summary_message = f"✅ Analysis Complete. Found {len(stocks)} Indian Stage 2 stocks."
                    
                    # 💾 THE DISK STATE PERSISTENCE LAYER CACHE SAVE
                    with open(RESULTS_JSON, 'w') as f:
                        json.dump({
                            'stocks': stocks, 
                            'summary_message': summary_message,
                            'last_file': last_file
                        }, f)
                else:
                    summary_message = "❌ Error: Column layout missing 'symbol' identification metric."
            except Exception as e:
                summary_message = f"❌ Error: {str(e)}"
        else:
            summary_message = "⚠️ No file loaded. Please attach a CSV tracking matrix spreadsheet file first."

    return render_template("stage2_india.html", 
                           stocks=stocks, 
                           last_file=last_file, 
                           summary_message=summary_message)