import os
import json
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Blueprint, render_template, request, session, send_file

# Standardize blueprint without local template folder to use global app/templates
rs_roc_bp = Blueprint("rs_roc", __name__)

# Paths aligned with your existing 'uploads' structure
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'rs_roc'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_rs_roc_results.json')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def fetch_nifty500_symbols():
    """Fetches symbols from NSE for automation"""
    url = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
    try:
        df = pd.read_csv(url)
        return df[['Symbol', 'Industry']].to_dict('records')
    except:
        return []

def screen_pro_momentum(stock_list):
    """Core logic: RS Percentile (1Y) + ROC (3M/6M)"""
    symbols = [f"{s['Symbol']}.NS" for s in stock_list]
    # Fetch all data in one batch for speed
    data = yf.download(symbols + ["^NSEI"], period="1y", interval="1d")['Close']
    
    results = []
    bench_ret_1y = (data["^NSEI"].iloc[-1] / data["^NSEI"].iloc[0]) - 1

    for item in stock_list:
        sym = f"{item['Symbol']}.NS"
        try:
            close = data[sym].dropna()
            if len(close) < 200: continue
            
            current_price = close.iloc[-1]
            ema_200 = close.ewm(span=200).mean().iloc[-1]
            
            # SELECTION: Stage 2 Filter (Price > 200 EMA)
            if current_price < ema_200: continue

            # SELECTION: RS Calculation (vs Nifty 50)
            stock_ret_1y = (current_price / close.iloc[0]) - 1
            rs_score = stock_ret_1y - bench_ret_1y
            
            # TIMING: ROC (3M and 6M)
            roc_3m = ((current_price - close.iloc[-63]) / close.iloc[-63]) * 100
            roc_6m = ((current_price - close.iloc[-126]) / close.iloc[-126]) * 100

            results.append({
                "symbol": item['Symbol'],
                "sector": item['Industry'],
                "price": round(current_price, 2),
                "rs_raw": rs_score,
                "roc_3m": round(roc_3m, 2),
                "roc_6m": round(roc_6m, 2)
            })
        except: continue

    if not results: return []

    df = pd.DataFrame(results)
    df['rs_percentile'] = df['rs_raw'].rank(pct=True).mul(100).round(0).astype(int)
    
    # --- TREND PERSISTENCE (Last 5 Runs) ---
    existing_history = {}
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            old_data = json.load(f).get('stocks', [])
            existing_history = {s['symbol']: s.get('rs_h', []) for s in old_data}

    def inject_history(row):
        h = existing_history.get(row['symbol'], [])
        row['rs_h'] = (h + [row['rs_percentile']])[-5:]
        # Acceleration logic: strictly increasing or simple uptrend
        row['rs_up'] = len(row['rs_h']) > 1 and all(x < y for x, y in zip(row['rs_h'], row['rs_h'][1:]))
        return row

    df = df.apply(inject_history, axis=1)
    df.sort_values(by="rs_percentile", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    df["rank"] = df.index + 1
    
    return df.to_dict(orient="records")

@rs_roc_bp.route("/rs-roc-momentum", methods=["GET", "POST"])
def rs_roc_momentum_process():
    stocks = []
    last_time = None
    old_ranks = {}

    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cache = json.load(f)
            stocks = cache.get('stocks', [])
            last_time = cache.get('time')
            old_ranks = {s['symbol']: s['rank'] for s in stocks}

    if request.method == "POST":
        stock_list = fetch_nifty500_symbols()
        results = screen_pro_momentum(stock_list)
        
        # Rank Change & Acceleration Logic
        for s in results:
            prev = old_ranks.get(s['symbol'])
            if prev is None:
                s["rank_status"], s["rank_diff"] = "new", 0
            else:
                diff = prev - s["rank"]
                s["rank_diff"] = diff
                s["rank_status"] = "up" if diff > 0 else ("down" if diff < 0 else "stable")
        
        last_time = datetime.now().strftime("%d %b %Y %I:%M %p")
        with open(RESULTS_JSON, 'w') as f:
            json.dump({'stocks': results, 'time': last_time}, f)
        stocks = results

    return render_template("rs_roc_momentum.html", stocks=stocks, last_time=last_time)

@rs_roc_bp.route("/export-rs-roc")
def export_rs_roc():
    """Generates timestamped CSV for the RS-ROC scan"""
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            data = json.load(f)
        stocks = data.get('stocks', [])
        if stocks:
            df = pd.DataFrame(stocks)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            export_filename = f"RS_ROC_Screener_{timestamp}.csv"
            export_path = os.path.join(UPLOAD_FOLDER, 'temp_rs_roc_export.csv')
            df.to_csv(export_path, index=False)
            return send_file(export_path, as_attachment=True, download_name=export_filename)
    return "No data to export", 404