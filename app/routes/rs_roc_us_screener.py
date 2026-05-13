import os
import json
import pandas as pd
import yfinance as yf
from datetime import datetime
from io import StringIO
import requests
from flask import Blueprint, render_template, request, send_file

# Standardize blueprint to use global app/templates
rs_roc_us_bp = Blueprint("rs_roc_us", __name__)

# Paths for US results
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'rs_roc_us'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_rs_roc_us_results.json')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def fetch_snp500_symbols():
    """Fetches the current S&P 500 list from Wikipedia with robust headers"""
    url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        tables = pd.read_html(StringIO(response.text))
        df = tables[0]
        
        # Standardize tickers: Wikipedia uses '.' (BRK.B), yfinance needs '-' (BRK-B)
        df['Symbol'] = df['Symbol'].str.replace('.', '-', regex=False)
        
        return df[['Symbol', 'GICS Sector']].rename(columns={'GICS Sector': 'Industry'}).to_dict('records')
    except Exception as e:
        print(f"Error fetching S&P 500: {e}")
        return []

def screen_us_pro_momentum(stock_list):
    """US Logic: RS Percentile (1Y) vs S&P 500 + ROC (3M/6M)"""
    symbols = [s['Symbol'] for s in stock_list]
    
    # auto_adjust=True handles dividend/split adjustments
    data = yf.download(symbols + ["^GSPC"], period="1y", interval="1d", auto_adjust=True)['Close']
    
    results = []
    # Calculate Benchmark Return
    bench_ret_1y = (data["^GSPC"].iloc[-1] / data["^GSPC"].iloc[0]) - 1

    for item in stock_list:
        sym = item['Symbol']
        try:
            close = data[sym].dropna()
            if len(close) < 200: continue
            
            current_price = close.iloc[-1]
            ema_200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
            
            if current_price < ema_200: continue

            stock_ret_1y = (current_price / close.iloc[0]) - 1
            rs_score = stock_ret_1y - bench_ret_1y
            
            roc_3m = ((current_price - close.iloc[-63]) / close.iloc[-63]) * 100
            roc_6m = ((current_price - close.iloc[-126]) / close.iloc[-126]) * 100

            results.append({
                "symbol": sym,
                "sector": item['Industry'],
                "price": round(current_price, 2), # ✅ Added Price
                "rs_raw": rs_score,
                "roc_3m": round(roc_3m, 2),
                "roc_6m": round(roc_6m, 2)
            })
        except: continue

    if not results: return []

    df = pd.DataFrame(results)
    df['rs_percentile'] = df['rs_raw'].rank(pct=True).mul(100).round(0).astype(int)
    
    # --- Trend Persistence Logic ---
    existing_history = {}
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            old_data = json.load(f).get('stocks', [])
            existing_history = {s['symbol']: s.get('rs_h', []) for s in old_data}

    def inject_history(row):
        h = existing_history.get(row['symbol'], [])
        row['rs_h'] = (h + [row['rs_percentile']])[-5:]
        row['rs_up'] = len(row['rs_h']) > 1 and all(x < y for x, y in zip(row['rs_h'], row['rs_h'][1:]))
        return row

    df = df.apply(inject_history, axis=1)
    df.sort_values(by="rs_percentile", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    df["rank"] = df.index + 1
    
    return df.to_dict(orient="records")

@rs_roc_us_bp.route("/rs-roc-us-momentum", methods=["GET", "POST"])
def rs_roc_us_momentum_process():
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
        stock_list = fetch_snp500_symbols()
        if not stock_list:
            return "Failed to fetch symbols from Wikipedia. Check lxml/requests.", 500
            
        results = screen_us_pro_momentum(stock_list)
        
        for s in results:
            prev = old_ranks.get(s['symbol'])
            if prev is None:
                s["rank_status"], s["rank_diff"] = "new", -999 #  Numerical flag for sorting
            else:
                diff = prev - s["rank"]
                s["rank_diff"] = diff
                s["rank_status"] = "up" if diff > 0 else ("down" if diff < 0 else "stable")
        
        last_time = datetime.now().strftime("%d %b %Y %I:%M %p")
        with open(RESULTS_JSON, 'w') as f:
            json.dump({'stocks': results, 'time': last_time}, f)
        stocks = results

    return render_template("rs_roc_us_momentum.html", stocks=stocks, last_time=last_time)

@rs_roc_us_bp.route("/export-rs-roc-us")
def export_rs_roc_us():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            stocks = json.load(f).get('stocks', [])
        if stocks:
            df = pd.DataFrame(stocks)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_path = os.path.join(UPLOAD_FOLDER, 'temp_us_export.csv')
            df.to_csv(temp_path, index=False)
            return send_file(temp_path, as_attachment=True, download_name=f"US_RS_ROC_Screener_{timestamp}.csv")
    return "No scan data available to export", 404