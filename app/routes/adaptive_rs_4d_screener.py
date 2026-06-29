import os
import json
import uuid
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from io import StringIO
import requests
from flask import Blueprint, render_template, request, send_file, session, redirect

adaptive_4d_bp = Blueprint("adaptive_4d", __name__)

# --- PATH COMPARTMENTALIZATION ---
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'adaptive_4d'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_adaptive_4d_results.json')
HISTORY_CACHE_DIR = os.path.join(UPLOAD_FOLDER, 'history_cache')
os.makedirs(HISTORY_CACHE_DIR, exist_ok=True)

# Fixed lookback periods matching specifications
LB_3M = 55    
LB_6M = 122   

def fetch_snp500_with_sectors():
    """Automated S&P 500 scraping with fundamental sector classifications"""
    url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        tables = pd.read_html(StringIO(response.text))
        df = tables[0]
        df['Symbol'] = df['Symbol'].str.replace('.', '-', regex=False)
        return df[['Symbol', 'GICS Sector']].rename(columns={'Symbol': 'symbol', 'GICS Sector': 'sector'}).to_dict('records')
    except Exception as e:
        print(f"Error fetching S&P 500: {e}")
        return []

def fetch_nifty500_with_sectors():
    """Automated Nifty 500 fetching with sector mapping records"""
    url = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
    try:
        df = pd.read_csv(url)
        return df[['Symbol', 'Industry']].rename(columns={'Symbol': 'symbol', 'Industry': 'sector'}).to_dict('records')
    except Exception as e:
        print(f"Error fetching Nifty 500: {e}")
        return []

def compute_volar_metric(prices):
    if len(prices) < 2: return 0.0
    returns = prices.pct_change().dropna()
    std = returns.std()
    total_ret = (prices.iloc[-1] / prices.iloc[0]) - 1
    return round(total_ret / std, 2) if std != 0 else 0.0

def process_4d_screening_pipeline(stock_list, market_type="US"):
    benchmark = "^GSPC" if market_type == "US" else "^NSEI"
    symbols = [f"{s['symbol']}.NS" if market_type == "INDIA" else s['symbol'] for s in stock_list]
    sector_map = {s['symbol']: s['sector'] for s in stock_list}

    fetch_days = LB_6M + 50
    start_date = (datetime.today() - timedelta(days=fetch_days)).strftime("%Y-%m-%d")
    
    print(f"📥 Downloading batch dataset for {len(symbols)} assets...")
    data = yf.download(symbols + [benchmark], start=start_date, interval="1d", auto_adjust=True)
    if data.empty: return []

    data.index = data.index.tz_localize(None)
    bench_close = data['Close'][benchmark].dropna()

    raw_candidates = []

    for item in stock_list:
        sym = item['symbol']
        yf_sym = f"{sym}.NS" if market_type == "INDIA" else sym
        
        try:
            if isinstance(data.columns, pd.MultiIndex):
                if yf_sym not in data.columns.get_level_values(1): continue
                stock_close = data['Close'][yf_sym].dropna()
            else:
                stock_close = data['Close'].dropna()

            if len(stock_close) < LB_6M or len(bench_close) < LB_6M: continue

            current_price = stock_close.iloc[-1]
            
            # ✂️ STAGE 2 FILTER REMOVED: All stocks from the selected universe are processed by default

            # --- ⚡ THE 4-DAY RS LINE SLOPE EDGE CHECK ---
            aligned_stock = stock_close.tail(10).reindex(bench_close.tail(10).index, method='ffill').dropna()
            aligned_bench = bench_close.reindex(aligned_stock.index)
            
            rs_ratio_series = aligned_stock / aligned_bench
            rs_deltas = rs_ratio_series.diff().dropna()
            
            if len(rs_deltas) >= 4:
                last_4_days = rs_deltas.tail(4)
                if not (last_4_days > 0).all():
                    continue
            else:
                continue

            # --- 3M Momentum Metrics ---
            s_3m = stock_close.iloc[-LB_3M]
            b_3m = bench_close.reindex(stock_close.index, method='ffill').iloc[-LB_3M]
            perf_3m = (current_price / s_3m) - 1
            bench_perf_3m = (bench_close.iloc[-1] / b_3m) - 1
            rs_raw_3m = perf_3m - bench_perf_3m
            volar_3m = compute_volar_metric(stock_close.iloc[-LB_3M:])

            # --- 6M Momentum Metrics ---
            s_6m = stock_close.iloc[-LB_6M]
            b_6m = bench_close.reindex(stock_close.index, method='ffill').iloc[-LB_6M]
            perf_6m = (current_price / s_6m) - 1
            bench_perf_6m = (bench_close.iloc[-1] / b_6m) - 1
            rs_raw_6m = perf_6m - bench_perf_6m
            volar_6m = compute_volar_metric(stock_close.iloc[-LB_6M:])

            raw_candidates.append({
                "symbol": sym,
                "sector": sector_map.get(sym, "Unknown"),
                "price": round(current_price, 2),
                "rs_raw_3m": rs_raw_3m,
                "rs_raw_6m": rs_raw_6m,
                "volar_3m": volar_3m,
                "volar_6m": volar_6m,
                "perf_3m": round(perf_3m * 100, 2),
                "perf_6m": round(perf_6m * 100, 2)
            })
        except Exception: continue

    if not raw_candidates: return []

    df = pd.DataFrame(raw_candidates)
    df['rs_pct_3m'] = df['rs_raw_3m'].rank(pct=True).mul(100).round(0).astype(int)
    df['rs_pct_6m'] = df['rs_raw_6m'].rank(pct=True).mul(100).round(0).astype(int)

    existing_history = {}
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            try:
                old_data = json.load(f).get('stocks', [])
                existing_history = {s['symbol']: {
                    'rs3_h': s.get('rs3_h', []), 'rs6_h': s.get('rs6_h', [])
                } for s in old_data}
            except Exception: pass

    def inject_historical_trends(row):
        h = existing_history.get(row['symbol'], {'rs3_h': [], 'rs6_h': []})
        row['rs3_h'] = (h['rs3_h'] + [row['rs_pct_3m']])[-5:]
        row['rs6_h'] = (h['rs6_h'] + [row['rs_pct_6m']])[-5:]
        row['rs3_up'] = len(row['rs3_h']) > 1 and all(x < y for x, y in zip(row['rs3_h'], row['rs3_h'][1:]))
        row['rs6_up'] = len(row['rs6_h']) > 1 and all(x < y for x, y in zip(row['rs6_h'], row['rs6_h'][1:]))
        return row

    df = df.apply(inject_historical_trends, axis=1)
    df.sort_values(by="rs_pct_3m", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    
    for idx, row in df.iterrows():
        df.at[idx, "rank"] = idx + 1

    return df.to_dict(orient="records")

# --- VIEWS CONTROLLER ---

@adaptive_4d_bp.route("/adaptive-rs-4d", methods=["GET", "POST"])
def adaptive_4d_process():
    stocks = []
    last_time = None
    market = request.args.get('market', 'US').upper()
    
    user_cache_file = os.path.join(HISTORY_CACHE_DIR, f'meta_history_{market.lower()}.json')
    history_meta = []
    if os.path.exists(user_cache_file):
        with open(user_cache_file, 'r') as f:
            history_meta = json.load(f)

    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            try:
                cache = json.load(f)
                if cache.get('market') == market:
                    stocks = cache.get('stocks', [])
                    last_time = cache.get('time')
            except Exception: pass

    if request.method == "POST":
        file = request.files.get('file')
        
        # 📂 PERSISTENT MARKET-SPECIFIC FILE UPLOAD LOGIC
        if file and file.filename != '':
            filename = f"{market.lower()}_{uuid.uuid4().hex[:6]}_{file.filename}"
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            session[f'adaptive_csv_path_{market}'] = filepath
            session[f'adaptive_csv_name_{market}'] = file.filename
            session.modified = True

        saved_path = session.get(f'adaptive_csv_path_{market}')

        # Choose between the custom user-uploaded CSV file or the broad index defaults
        if saved_path and os.path.exists(saved_path):
            print(f"📋 Loading custom universe CSV layout: {saved_path}")
            df_input = pd.read_csv(saved_path)
            col_name = 'Symbol' if 'Symbol' in df_input.columns else ('symbol' if 'symbol' in df_input.columns else df_input.columns[0])
            
            # Map sectors seamlessly if present; otherwise default to "Custom Pool"
            stock_list = []
            for s in df_input[col_name].dropna().unique():
                sym_str = str(s).strip().upper().replace('.NS', '')
                sect_str = str(df_input['Sector'].iloc[0]) if 'Sector' in df_input.columns else "Custom Pool"
                stock_list.append({"symbol": sym_str, "sector": sect_str})
            source_display = session.get(f'adaptive_csv_name_{market}', 'Custom Upload')
        else:
            # 🌐 AUTOMATED BROAD INDEX BACKUPS INGESTION LAYER
            print(f"🌐 Fetching default full baseline market rosters for: {market}")
            stock_list = fetch_nifty500_with_sectors() if market == "INDIA" else fetch_snp500_with_sectors()
            source_display = "Nifty 500 Index (Default)" if market == "INDIA" else "S&P 500 Index (Default)"

        if stock_list:
            # Slice processing boundary window size to 100 tickers for fast runtime executions
            results = process_4d_screening_pipeline(stock_list[:100], market_type=market)
            last_time = datetime.now().strftime("%d %b %Y %I:%M %p")
            
            snapshot_id = f"snap_4d_{uuid.uuid4().hex}"
            snapshot_file_path = os.path.join(HISTORY_CACHE_DIR, f"{snapshot_id}.json")
            with open(snapshot_file_path, 'w') as f:
                json.dump(results, f)
                
            history_meta.insert(0, {"snapshot_id": snapshot_id, "time": last_time, "count": len(results)})
            history_meta = history_meta[:5]
            with open(user_cache_file, 'w') as f:
                json.dump(history_meta, f)

            with open(RESULTS_JSON, 'w') as f:
                json.dump({'stocks': results, 'time': last_time, 'market': market, 'source_name': source_display}, f)
            stocks = results

    # Re-read historical origin tag attributes from the local parameters cache file
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            source_display = json.load(f).get('source_name', 'Default Index Universe')
    else:
        source_display = "Nifty 500 Index (Default)" if market == "INDIA" else "S&P 500 Index (Default)"

    # Sector Alpha Tracker calculations
    sector_counts = {}
    for s in stocks:
        sector_counts[s['sector']] = sector_counts.get(s['sector'], 0) + 1
    sorted_sectors = sorted(sector_counts.items(), key=lambda x: x[1], reverse=True)
    top_3_sectors = [item[0] for item in sorted_sectors[:3]]

    return render_template(
        "adaptive_rs_4d_screener.html", 
        stocks=stocks, 
        last_time=last_time, 
        market=market, 
        history=history_meta,
        top_3_sectors=top_3_sectors,
        active_file=session.get(f'adaptive_csv_name_{market}'),
        source_name=source_display
    )

@adaptive_4d_bp.route("/restore-adaptive-4d/<snapshot_id>")
def restore_adaptive_4d_snapshot(snapshot_id):
    market = request.args.get('market', 'US').upper()
    snapshot_file_path = os.path.join(HISTORY_CACHE_DIR, f"{snapshot_id}.json")
    if os.path.exists(snapshot_file_path):
        with open(snapshot_file_path, 'r') as f:
            restored_records = json.load(f)
        restored_time = datetime.now().strftime("%d %b %Y %I:%M %p") + " (Restored)"
        with open(RESULTS_JSON, 'w') as f:
            json.dump({'stocks': restored_records, 'time': restored_time, 'market': market, 'source_name': 'Snapshot Recovery'}, f)
            
    return redirect(f"/adaptive-rs-4d?market={market}")