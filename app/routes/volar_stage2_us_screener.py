import os
import json
import uuid
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Blueprint, render_template, request, send_file, session, redirect, url_for

volar_us_bp = Blueprint("volar_us", __name__)

# --- PATH LOGIC & SRE SEGREGATION ---
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'volar_us'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_volar_us_results.json')
HISTORY_CACHE_DIR = os.path.join(UPLOAD_FOLDER, 'history_cache')
os.makedirs(HISTORY_CACHE_DIR, exist_ok=True)

def fetch_daily_data(symbol, days=252):
    ticker = yf.Ticker(symbol)
    return ticker.history(period=f"{days}d", interval="1d")

def compute_volar(close_series):
    total_return = (close_series.iloc[-1] / close_series.iloc[0]) - 1
    volatility = close_series.pct_change(fill_method=None).std()
    return total_return / volatility if volatility != 0 else None

def compute_relative_strength(stock_close, index_close):
    stock_return = (stock_close.iloc[-1] / stock_close.iloc[0]) - 1
    index_return = (index_close.iloc[-1] / index_close.iloc[0]) - 1
    return stock_return / index_return if index_return != 0 else None

def is_volar_candidate(symbol, index_symbol="^GSPC"): 
    try:
        stock_df = fetch_daily_data(symbol)
        index_df = fetch_daily_data(index_symbol)
        if stock_df.empty or index_df.empty or len(stock_df) < 200:
            return None

        close = stock_df["Close"]
        high_52w = close[-252:].max()
        current_price = close.iloc[-1]
        pullback = (high_52w - current_price) / high_52w
        ema_200 = close.ewm(span=200).mean().iloc[-1]
        
        volar_val = compute_volar(close)
        rs_val = compute_relative_strength(close, index_df["Close"])
        performance = (close.iloc[-1] / close.iloc[0]) - 1

        if pullback < 0.3 and current_price > ema_200 and volar_val and rs_val:
            return {
                "symbol": symbol,
                "price": round(current_price, 2),
                "pullback_pct": round(pullback * 100, 2),
                "ema_200": round(ema_200, 2),
                "volar": round(volar_val, 2),
                "relative_strength": round(rs_val, 2),
                "performance": round(performance * 100, 2)
            }
    except Exception:
        return None
    return None

def screen_volar_us(symbols):
    results = [is_volar_candidate(sym) for sym in symbols if is_volar_candidate(sym)]
    if not results: return []
    
    df = pd.DataFrame(results)
    df['relative_strength'] = pd.to_numeric(df['relative_strength'], errors='coerce')
    df = df.dropna(subset=['relative_strength'])
    df['rs_percentile'] = df['relative_strength'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)
    
    existing_history = {}
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            old_cache = json.load(f).get('stocks', [])
            existing_history = {s['symbol']: {
                'rs_h': s.get('rs_h', []),
                'vol_h': s.get('vol_h', []),
                'perf_h': s.get('perf_h', [])
            } for s in old_cache}

    def inject_trends_us(row):
        sym = row['symbol']
        h = existing_history.get(sym, {'rs_h': [], 'vol_h': [], 'perf_h': []})
        row['rs_h'] = (h['rs_h'] + [row['rs_percentile']])[-5:]
        row['vol_h'] = (h['vol_h'] + [row['volar']])[-5:]
        row['perf_h'] = (h['perf_h'] + [row['performance']])[-5:]
        row['rs_up'] = len(row['rs_h']) > 1 and all(x < y for x, y in zip(row['rs_h'], row['rs_h'][1:]))
        return row

    df = df.apply(inject_trends_us, axis=1)
    df.sort_values(by="relative_strength", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    df["rank"] = df.index + 1
    return df.to_dict(orient="records")

# --- APP INTERACTION ROUTERS ---

@volar_us_bp.route("/volar-us", methods=["GET", "POST"])
def volar_us_process():
    stocks = []
    last_processed_time = None
    source_name = "None"
    last_csv_config = os.path.join(UPLOAD_FOLDER, 'last_csv_path.json')
    compare_mode = request.args.get('compare') == 'true'
    
    # Track metadata summary variables securely in a standalone local tracking json file
    meta_history_file = os.path.join(HISTORY_CACHE_DIR, 'meta_history.json')
    history_meta = []
    if os.path.exists(meta_history_file):
        with open(meta_history_file, 'r') as f:
            history_meta = json.load(f)

    old_ranks = {}
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cache = json.load(f)
            stocks = cache.get('stocks', [])
            old_ranks = {s['symbol']: s['rank'] for s in stocks}
            last_processed_time = cache.get('time')
            source_name = cache.get('source', 'Cached Scan')

    if request.method == "POST":
        file = request.files.get('file')
        
        if file and file.filename != '':
            from werkzeug.utils import secure_filename
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            with open(last_csv_config, 'w') as f:
                json.dump({'path': filepath, 'name': filename}, f)
            source_name = filename
        elif os.path.exists(last_csv_config):
            with open(last_csv_config, 'r') as f:
                cfg = json.load(f)
                filepath = cfg.get('path')
                source_name = cfg.get('name')
        else:
            filepath = os.path.abspath(os.path.join(os.getcwd(), 'data', 'sp500.csv'))
            source_name = "S&P 500 Default"

        if not os.path.exists(filepath):
            return render_template("stage2_volar_us.html", error=f"File not found: {filepath}")

        df_input = pd.read_csv(filepath)
        col_name = 'Symbol' if 'Symbol' in df_input.columns else 'symbol'
        symbols = [str(s).strip().upper() for s in df_input[col_name].dropna().unique()]
        
        results = screen_volar_us(symbols)
        enriched = []
        leaders_90 = []
        
        for stock in results:
            sym = stock["symbol"]
            stock["symbol_clean"] = sym
            if stock.get('rs_percentile', 0) >= 90:
                leaders_90.append(sym)

            prev_rank = old_ranks.get(sym)
            if prev_rank is None:
                stock["rank_status"], stock["rank_diff"] = "new", 0
            else:
                diff = prev_rank - stock["rank"]
                stock["rank_diff"] = diff
                stock["rank_status"] = "up" if diff > 0 else ("down" if diff < 0 else "stable")
            enriched.append(stock)

        last_processed_time = datetime.now().strftime("%d %b %Y %I:%M %p")
        
        # 💾 THE DISK-CACHE WORKAROUND LOGIC: Write massive payload file states directly to local workspace folders
        snapshot_id = f"snapshot_{uuid.uuid4().hex}"
        snapshot_file_path = os.path.join(HISTORY_CACHE_DIR, f"{snapshot_id}.json")
        with open(snapshot_file_path, 'w') as f:
            json.dump(enriched, f)
            
        # Append lightweight text tags into metadata array lists
        history_meta.insert(0, {
            "snapshot_id": snapshot_id,
            "time": last_processed_time,
            "source": source_name,
            "count": len(enriched),
            "leaders_90": leaders_90
        })
        history_meta = history_meta[:5]
        with open(meta_history_file, 'w') as f:
            json.dump(history_meta, f)

        with open(RESULTS_JSON, 'w') as f:
            json.dump({'stocks': enriched, 'time': last_processed_time, 'source': source_name}, f)
        stocks = enriched

    if compare_mode and len(history_meta) >= 3:
        leader_sets = [set(h.get('leaders_90', [])) for h in history_meta[:3]]
        consistent = set.intersection(*leader_sets) if leader_sets else set()
        for s in stocks:
            if s['symbol'] in consistent: s['is_consistent'] = True

    return render_template("stage2_volar_us.html", stocks=stocks, 
                           last_processed_time=last_processed_time, 
                           source_name=source_name, history=history_meta, 
                           compare_mode=compare_mode)

@volar_us_bp.route("/restore-volar-us/<snapshot_id>")
def restore_volar_us_snapshot(snapshot_id):
    """🛡️ LOCAL DISK RECOVERY ROUTE: Instantly loads heavy array records without cookie limits"""
    snapshot_file_path = os.path.join(HISTORY_CACHE_DIR, f"{snapshot_id}.json")
    meta_history_file = os.path.join(HISTORY_CACHE_DIR, 'meta_history.json')
    
    if os.path.exists(snapshot_file_path):
        with open(snapshot_file_path, 'r') as f:
            restored_records = json.load(f)
            
        restored_time = datetime.now().strftime("%d %b %Y %I:%M %p") + " (Restored Snapshot)"
        restored_source = "Snapshot"
        
        if os.path.exists(meta_history_file):
            with open(meta_history_file, 'r') as f:
                meta_list = json.load(f)
            for m in meta_list:
                if m.get('snapshot_id') == snapshot_id:
                    restored_time = m.get('time') + " (Restored Snapshot)"
                    restored_source = m.get('source')
                    break
                    
        with open(RESULTS_JSON, 'w') as f:
            json.dump({'stocks': restored_records, 'time': restored_time, 'source': restored_source}, f)
            
    return redirect(url_for('volar_us.volar_us_process'))

# ... (Keep the remaining functions like export_volar_us, add_favorite_us, etc. exactly the same below)
@volar_us_bp.route("/export-volar-us")
def export_volar_us():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            data = json.load(f)
        stocks = data.get('stocks', [])
        if stocks:
            df = pd.DataFrame(stocks)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            export_filename = f"US_Volar_Screener_{timestamp}.csv"
            export_path = os.path.join(UPLOAD_FOLDER, 'temp_export_us.csv')
            df.to_csv(export_path, index=False)
            return send_file(export_path, as_attachment=True, download_name=export_filename)
    return "No data", 404

@volar_us_bp.route("/add-favorite-us", methods=["POST"])
def add_favorite_us():
    symbol = request.form.get('symbol')
    fav_path = os.path.join(UPLOAD_FOLDER, 'favorites_us.json')
    favorites = []
    if os.path.exists(fav_path):
        with open(fav_path, 'r') as f:
            favorites = json.load(f)
    if symbol not in favorites:
        favorites.append(symbol)
        with open(fav_path, 'w') as f:
            json.dump(favorites, f)
    return {"status": "success", "message": f"{symbol} added to US Watchlist"}

@volar_us_bp.route("/view-favorites-us")
def view_favorites_us():
    fav_path = os.path.join(UPLOAD_FOLDER, 'favorites_us.json')
    stocks = []
    if os.path.exists(fav_path):
        with open(fav_path, 'r') as f:
            symbols = json.load(f)
        for sym in symbols:
            data = is_volar_candidate(sym, index_symbol="^GSPC")
            if data: stocks.append(data)
    return render_template("view_favorites.html", stocks=stocks, market="US", currency="$")

@volar_us_bp.route("/remove-favorite-us", methods=["POST"])
def remove_favorite_us():
    symbol = request.form.get('symbol')
    fav_path = os.path.join(UPLOAD_FOLDER, 'favorites_us.json')
    if os.path.exists(fav_path):
        with open(fav_path, 'r') as f:
            favorites = json.load(f)
        if symbol in favorites:
            favorites.remove(symbol)
            with open(fav_path, 'w') as f:
                json.dump(favorites, f)
    return {"status": "success"}

@volar_us_bp.route("/add-to-strategy", methods=["POST"])
def add_to_strategy():
    symbol = request.form.get('symbol')
    strategy = request.form.get('strategy')
    market = request.form.get('market')
    folder = UPLOAD_FOLDER if market == 'india' else os.path.join(os.getcwd(), 'uploads', 'volar_us')
    fav_path = os.path.join(folder, f'strategy_{strategy.lower().replace(" ", "_")}.json')
    yf_sym = symbol if market == 'us' else (symbol if symbol.endswith(".NS") else f"{symbol}.NS")
    ticker = yf.Ticker(yf_sym)
    current_price = ticker.history(period="1d")['Close'].iloc[-1]
    new_entry = {
        "symbol": symbol,
        "entry_date": datetime.now().strftime("%Y-%m-%d"),
        "entry_price": round(current_price, 2)
    }
    data = []
    if os.path.exists(fav_path):
        with open(fav_path, 'r') as f:
            data = json.load(f)
    if not any(item['symbol'] == symbol for item in data):
        data.append(new_entry)
        with open(fav_path, 'w') as f:
            json.dump(data, f)
    return {"status": "success", "message": f"{symbol} added to {strategy} strategy at ${round(current_price, 2)}"}

@volar_us_bp.route("/view-strategy-us/<name>")
def view_strategy_us(name):
    file_path = os.path.join(UPLOAD_FOLDER, f'strategy_{name.lower()}.json')
    performance_data = []
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            entries = json.load(f)
        for item in entries:
            ticker = yf.Ticker(item['symbol'])
            hist = ticker.history(period="1d")
            if not hist.empty:
                current_price = hist['Close'].iloc[-1]
                ret_pct = ((current_price - item['entry_price']) / item['entry_price']) * 100
                performance_data.append({
                    "symbol": item['symbol'],
                    "entry_date": item['entry_date'],
                    "entry_price": item['entry_price'],
                    "current_price": round(current_price, 2),
                    "return_pct": round(ret_pct, 2)
                })
    return render_template("strategy_watchlist.html", stocks=performance_data, strategy_name=name.upper(), currency="$", market="US")