import os
import json
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, request, send_file
from werkzeug.utils import secure_filename
import glob

hh_hl_bp = Blueprint("hh_hl_india", __name__)

UPLOAD_FOLDER = 'uploads/india_hhhl'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def get_latest_csv(directory):
    list_of_files = glob.glob(os.path.join(directory, '*.csv'))
    return max(list_of_files, key=os.path.getmtime) if list_of_files else None

def detect_hh_hl_stage2(symbol):
    try:
        yf_symbol = symbol if symbol.endswith(".NS") else f"{symbol}.NS"
        ticker = yf.Ticker(yf_symbol)
        df = ticker.history(period="1y")

        if df.empty or len(df) < 200:
            return None

        curr_price = df['Close'].iloc[-1]
        ma200 = df['Close'].rolling(window=200).mean().iloc[-1]
        
        if curr_price < ma200: # Stage 2 Filter
            return None

        df['is_low'] = (df['Low'] < df['Low'].shift(1)) & (df['Low'] < df['Low'].shift(2)) & \
                       (df['Low'] < df['Low'].shift(-1)) & (df['Low'] < df['Low'].shift(-2))
        
        lows_df = df[df['is_low']]
        if len(lows_df) < 2: return None

        last_swing_low = lows_df['Low'].iloc[-1]
        prev_swing_low = lows_df['Low'].iloc[-2]

        if last_swing_low > prev_swing_low and curr_price > last_swing_low:
            high_52 = df['High'].max()
            retracement = round(((high_52 - curr_price) / high_52) * 100, 2)
            rs_score = round(curr_price / ma200, 2)
            sl_percent = round(((curr_price - last_swing_low) / curr_price) * 100, 2)

            return {
                "symbol": symbol,
                "price": round(curr_price, 2),
                "last_swing_low": round(last_swing_low, 2),
                "sl_percent": sl_percent,
                "retracement": retracement,
                "rs": rs_score
            }
    except:
        return None

@hh_hl_bp.route("/hh-hl-india", methods=["GET", "POST"])
def hh_hl_view():
    stocks = []
    summary_message = None
    results_path = os.path.join(UPLOAD_FOLDER, 'hhhl_results.json')
    
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
                    res = detect_hh_hl_stage2(str(s).strip().upper())
                    if res: stocks.append(res)
                
                stocks.sort(key=lambda x: x['rs'], reverse=True)
                with open(results_path, 'w') as f:
                    json.dump(stocks, f)
                summary_message = f"✅ Scan Complete using {last_file_name}. Found {len(stocks)} stocks."
            except Exception as e:
                summary_message = f"❌ Error: {str(e)}"
    else:
        if os.path.exists(results_path):
            with open(results_path, 'r') as f:
                stocks = json.load(f)

    return render_template("hh_hl_india.html", stocks=stocks, summary_message=summary_message, last_file=last_file_name)

@hh_hl_bp.route("/export-hhhl")
def export_hhhl():
    results_path = os.path.join(UPLOAD_FOLDER, 'hhhl_results.json')
    if os.path.exists(results_path):
        with open(results_path, 'r') as f:
            data = json.load(f)
        df = pd.DataFrame(data)
        export_path = os.path.join(UPLOAD_FOLDER, 'hhhl_export.csv')
        df.to_csv(export_path, index=False)
        return send_file(export_path, as_attachment=True)
    return "No data to export", 404