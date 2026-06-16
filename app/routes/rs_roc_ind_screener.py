import os
import json
import uuid
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Blueprint, render_template, request, session, send_file, redirect, url_for

# Standardize blueprint to use global app/templates
rs_roc_bp = Blueprint("rs_roc", __name__)

# Paths aligned with your existing 'uploads' structure
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'rs_roc'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_rs_roc_results.json')
HISTORY_CACHE_DIR = os.path.join(UPLOAD_FOLDER, 'history_cache')
os.makedirs(HISTORY_CACHE_DIR, exist_ok=True)

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
    data = yf.download(symbols + ["^NSEI"], period="1y", interval="1d", auto_adjust=True)['Close']
    
    results = []
    bench_ret_1y = (data["^NSEI"].iloc[-1] / data["^NSEI"].iloc[0]) - 1

    for item in stock_list:
        sym = f"{item['Symbol']}.NS"
        try:
            close = data[sym].dropna()
            if len(close) < 200: continue
            
            current_price = close.iloc[-1]
            ema_200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
            
            # Stage 2 Filter (Price > 200 EMA)
            if current_price < ema_200: continue

            # RS vs Nifty 50
            stock_ret_1y = (current_price / close.iloc[0]) - 1
            rs_score = stock_ret_1y - bench_ret_1y
            
            # ROC Calculations
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
    
    # 🛡️ DEFENSIVE SANITIZATION: Force casting structure to floats to prevent IntCastingNaNError
    df['rs_raw'] = pd.to_numeric(df['rs_raw'], errors='coerce')
    df = df.dropna(subset=['rs_raw'])
    
    if df.empty: return []
    
    df['rs_percentile'] = df['rs_raw'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)
    
    # History Tracking
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

@rs_roc_bp.route("/rs-roc-momentum", methods=["GET", "POST"])
def rs_roc_momentum_process():
    stocks, last_time, old_ranks = [], None, {}
    
    # Read/Initialize disk-backed track configurations safely
    user_cache_file = os.path.join(HISTORY_CACHE_DIR, 'meta_history.json')
    history_meta = []
    if os.path.exists(user_cache_file):
        with open(user_cache_file, 'r') as f:
            history_meta = json.load(f)

    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cache = json.load(f)
            stocks, last_time = cache.get('stocks', []), cache.get('time')
            old_ranks = {s['symbol']: s['rank'] for s in stocks}

    if request.method == "POST":
        stock_list = fetch_nifty500_symbols()
        results = screen_pro_momentum(stock_list)
        
        for s in results:
            prev = old_ranks.get(s['symbol'])
            if prev is None:
                s["rank_status"], s["rank_diff"] = "new", -999 
            else:
                diff = prev - s["rank"]
                s["rank_diff"] = diff
                s["rank_status"] = "up" if diff > 0 else ("down" if diff < 0 else "stable")
        
        last_time = datetime.now().strftime("%d %b %Y %I:%M %p")
        
        # 💾 HARD DRIVE RETENTION SNAPSHOT ENGINE
        snapshot_id = f"snapshot_{uuid.uuid4().hex}"
        snapshot_file_path = os.path.join(HISTORY_CACHE_DIR, f"{snapshot_id}.json")
        with open(snapshot_file_path, 'w') as f:
            json.dump(results, f)
            
        # Append lightweight summary descriptors into metadata manifest configuration files
        history_meta.insert(0, {
            "snapshot_id": snapshot_id,
            "time": last_time,
            "count": len(results)
        })
        history_meta = history_meta[:5] # Enforce strict 5-run constraint parameters
        with open(user_cache_file, 'w') as f:
            json.dump(history_meta, f)

        with open(RESULTS_JSON, 'w') as f:
            json.dump({'stocks': results, 'time': last_time}, f)
        stocks = results

    return render_template("rs_roc_ind_momentum.html", stocks=stocks, last_time=last_time, history=history_meta)

@rs_roc_bp.route("/restore-rs-roc-ind/<snapshot_id>")
def restore_rs_roc_ind_snapshot(snapshot_id):
    """🛡️ INFRASTRUCTURE SECURITY RECOVERY ROUTER: Instantly recovers local stock logs out of disk memory"""
    snapshot_file_path = os.path.join(HISTORY_CACHE_DIR, f"{snapshot_id}.json")
    user_cache_file = os.path.join(HISTORY_CACHE_DIR, 'meta_history.json')
    
    if os.path.exists(snapshot_file_path):
        with open(snapshot_file_path, 'r') as f:
            restored_records = json.load(f)
            
        restored_time = datetime.now().strftime("%d %b %Y %I:%M %p") + " (Restored Snapshot)"
        if os.path.exists(user_cache_file):
            with open(user_cache_file, 'r') as f:
                meta_list = json.load(f)
            for m in meta_list:
                if m.get('snapshot_id') == snapshot_id:
                    restored_time = m.get('time') + " (Restored Snapshot)"
                    break
                    
        with open(RESULTS_JSON, 'w') as f:
            json.dump({'stocks': restored_records, 'time': restored_time}, f)
            
    return redirect(url_for('rs_roc.rs_roc_momentum_process'))

@rs_roc_bp.route("/export-rs-roc")
def export_rs_roc():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            data = json.load(f)
            stocks = data.get('stocks', [])
            # Support backwards compatibility if cache format matches old simple list layers
            if not stocks and isinstance(data, list):
                stocks = data
        if stocks:
            df = pd.DataFrame(stocks)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            # Clear multi-user race window dependencies by dumping strictly into project temporary paths
            temp_filename = f"NSE_PRO_SCAN_{timestamp}.csv"
            temp_path = os.path.join(UPLOAD_FOLDER, 'temp_ind_export.csv')
            df.to_csv(temp_path, index=False)
            return send_file(temp_path, as_attachment=True, download_name=temp_filename)
    return "No Data", 404