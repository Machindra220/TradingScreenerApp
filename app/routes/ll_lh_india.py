import os
import json
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, request, send_file
from werkzeug.utils import secure_filename
import glob

ll_lh_bp = Blueprint("ll_lh_india", __name__)

UPLOAD_FOLDER = 'uploads/india_lllh'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def get_latest_csv(directory):
    list_of_files = glob.glob(os.path.join(directory, '*.csv'))
    return max(list_of_files, key=os.path.getmtime) if list_of_files else None

def detect_ll_lh_stage3(symbol):
    try:
        yf_symbol = symbol if symbol.endswith(".NS") else f"{symbol}.NS"
        ticker = yf.Ticker(yf_symbol)
        # Fetch more data to ensure MA200 stability
        df = ticker.history(period="2y") 

        if df.empty or len(df) < 200:
            return None

        curr_price = df['Close'].iloc[-1]
        ma200 = df['Close'].rolling(window=200).mean()
        curr_ma200 = ma200.iloc[-1]
        
        # Stage 3/4 Logic: Price starts breaking below MA200 or MA200 flattens
        # We filter for stocks below or within 5% of MA200 to catch the breakdown
        if curr_price > (curr_ma200 * 1.05): 
            return None

        # Identify local peaks (Swing Highs)
        df['is_high'] = (df['High'] < df['High'].shift(1)) & (df['High'].shift(1) > df['High'].shift(2))
        # Identify local troughs (Swing Lows)
        df['is_low'] = (df['Low'] > df['Low'].shift(1)) & (df['Low'].shift(1) < df['Low'].shift(2))
        
        highs_df = df[df['is_high']]
        lows_df = df[df['is_low']]

        if len(highs_df) < 2 or len(lows_df) < 2: return None

        last_sh = highs_df['High'].iloc[-1]
        prev_sh = highs_df['High'].iloc[-2]
        
        last_sl = lows_df['Low'].iloc[-1]
        prev_sl = lows_df['Low'].iloc[-2]

        # Condition for Stage 3/4: Lower High (LH) AND Lower Low (LL)
        if last_sh < prev_sh and last_sl < prev_sl:
            high_52 = df['High'].iloc[-252:].max()
            
            # Retrace: How far below the 52W High are we?
            retracement = round(((high_52 - curr_price) / high_52) * 100, 2)
            
            # RS: Price relative to MA200 (Lower is weaker)
            rs_score = round(curr_price / curr_ma200, 2)
            
            # SL: Place Stop Loss at the recent Lower High
            sl_percent = round(((last_sh - curr_price) / curr_price) * 100, 2)

            return {
                "symbol": symbol,
                "price": round(curr_price, 2),
                "swing_low": round(last_sl, 2),
                "last_swing_high": round(last_sh, 2),
                "sl_percent": sl_percent,
                "retracement": retracement,
                "rs": rs_score
            }
    except Exception:
        return None
    return None

# ... (Route methods remain the same as provided in your original file)

@ll_lh_bp.route("/ll-lh-india", methods=["GET", "POST"])
def ll_lh_view():
    stocks = []
    summary_message = None
    results_path = os.path.join(UPLOAD_FOLDER, 'lllh_results.json')
    
    # Check for the last used CSV file
    latest_file_path = get_latest_csv(UPLOAD_FOLDER)
    last_file_name = os.path.basename(latest_file_path) if latest_file_path else "None"

    if request.method == "POST":
        file = request.files.get('file')
        filepath = latest_file_path # Default to the last one if no new file is chosen

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
                    res = detect_ll_lh_stage3(str(s).strip().upper())
                    if res: stocks.append(res)
                
                stocks.sort(key=lambda x: x['rs'], reverse=False)
                with open(results_path, 'w') as f:
                    json.dump(stocks, f)
                summary_message = f"✅ Scan Complete using {last_file_name}. Found {len(stocks)} stocks."
            except Exception as e:
                summary_message = f"❌ Error: {str(e)}"
    else:
        if os.path.exists(results_path):
            with open(results_path, 'r') as f:
                stocks = json.load(f)

    return render_template("ll_lh_india.html", stocks=stocks, summary_message=summary_message, last_file=last_file_name)

@ll_lh_bp.route("/export-lllh")
def export_lllh():
    results_path = os.path.join(UPLOAD_FOLDER, 'lllh_results.json')
    if os.path.exists(results_path):
        with open(results_path, 'r') as f:
            data = json.load(f)
        df = pd.DataFrame(data)
        export_path = os.path.join(UPLOAD_FOLDER, 'lllh_export.csv')
        df.to_csv(export_path, index=False)
        return send_file(export_path, as_attachment=True)
    return "No data to export", 404