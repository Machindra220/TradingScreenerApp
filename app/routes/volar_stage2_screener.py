import os
import json
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Blueprint, render_template, request, send_file, session
from werkzeug.utils import secure_filename

volar_bp = Blueprint("volar_ind", __name__)

# --- PATH LOGIC (Root Level) ---
# Ensures the 'uploads' folder is created in the project root
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'volar_ind'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_volar_results.json')
# Path to store which CSV was last used
LAST_CSV_CONFIG = os.path.join(UPLOAD_FOLDER, 'last_csv_path.json')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def fetch_daily_data(symbol, days=252):
    """Fetches 1 year (252 trading days) of daily data"""
    ticker = yf.Ticker(symbol)
    return ticker.history(period=f"{days}d", interval="1d")

def compute_volar(close_series):
    """Calculates the VOLAR metric (Total Return / Volatility)"""
    total_return = (close_series.iloc[-1] / close_series.iloc[0]) - 1
    volatility = close_series.pct_change(fill_method=None).std()
    return total_return / volatility if volatility != 0 else None

def compute_relative_strength(stock_close, index_close):
    """Calculates Relative Strength against the benchmark index"""
    stock_return = (stock_close.iloc[-1] / stock_close.iloc[0]) - 1
    index_return = (index_close.iloc[-1] / index_close.iloc[0]) - 1
    return stock_return / index_return if index_return != 0 else None

def is_volar_candidate(symbol, index_symbol="^NSEI"):
    """Evaluates if a stock meets Stage 2 VOLAR criteria"""
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

        # Stage 2 Criteria: Pullback < 30% and Price > 200 EMA
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
    except Exception as e:
        print(f"Error screening {symbol}: {e}")
    return None

def screen_volar(symbols):
    results = []
    for sym in symbols:
        data = is_volar_candidate(sym)
        if data:
            results.append(data)
    
    if not results: return []
    
    df = pd.DataFrame(results)
    df['rs_percentile'] = df['relative_strength'].rank(pct=True).mul(100).round(0).astype(int)
    
    # --- TREND PERSISTENCE LOGIC ---
    existing_history = {}
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            old_cache = json.load(f).get('stocks', [])
            # Map symbol to its historical data lists
            existing_history = {s['symbol']: {
                'rs_h': s.get('rs_h', []),
                'vol_h': s.get('vol_h', []),
                'perf_h': s.get('perf_h', [])
            } for s in old_cache}

    def inject_trends(row):
        sym = row['symbol']
        h = existing_history.get(sym, {'rs_h': [], 'vol_h': [], 'perf_h': []})
        # Append new value and keep last 5
        row['rs_h'] = (h['rs_h'] + [row['rs_percentile']])[-5:]
        row['vol_h'] = (h['vol_h'] + [row['volar']])[-5:]
        row['perf_h'] = (h['perf_h'] + [row['performance']])[-5:]
        
        # Check if RS is strictly increasing over available history
        row['rs_up'] = len(row['rs_h']) > 1 and all(x < y for x, y in zip(row['rs_h'], row['rs_h'][1:]))
        return row

    df = df.apply(inject_trends, axis=1)
    # -------------------------------

    df.sort_values(by="relative_strength", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    df["rank"] = df.index + 1
    return df.to_dict(orient="records")

@volar_bp.route("/volar-ind", methods=["GET", "POST"])
def volar_process():
    stocks = []
    last_processed_time = None
    source_name = "None"
    compare_mode = request.args.get('compare') == 'true'
    
    # 1. Initialize variables outside blocks to prevent NameError
    old_ranks = {}
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cache = json.load(f)
            stocks = cache.get('stocks', [])
            old_ranks = {s['symbol_clean']: s['rank'] for s in stocks}
            last_processed_time = cache.get('time')
            source_name = cache.get('source', 'Cached Scan')

    if request.method == "POST":
        file = request.files.get('file')
        
        # 2. Persistent CSV Selection Logic
        if file and file.filename != '':
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            with open(LAST_CSV_CONFIG, 'w') as f:
                json.dump({'path': filepath, 'name': filename}, f)
            source_name = filename
        elif os.path.exists(LAST_CSV_CONFIG):
            with open(LAST_CSV_CONFIG, 'r') as f:
                cfg = json.load(f)
                filepath = cfg.get('path')
                source_name = cfg.get('name')
        else:
            filepath = os.path.abspath(os.path.join(os.getcwd(), 'data', 'nifty_500.csv'))
            source_name = "Nifty 500 Default"

        if not os.path.exists(filepath):
            return render_template("stage2_volar.html", error=f"File not found: {filepath}")

        # 3. Processing & Ranking
        df_input = pd.read_csv(filepath)
        col_name = 'Symbol' if 'Symbol' in df_input.columns else 'symbol'
        symbols = [str(s).strip().upper() + ".NS" for s in df_input[col_name].dropna().unique()]
        
        results = screen_volar(symbols)
        enriched = []
        leaders_90 = []
        
        for stock in results:
            sym_clean = stock["symbol"].replace(".NS", "")
            stock["symbol_clean"] = sym_clean
            if stock.get('rs_percentile', 0) >= 90:
                leaders_90.append(sym_clean)

            prev_rank = old_ranks.get(sym_clean)
            if prev_rank is None:
                stock["rank_status"], stock["rank_diff"] = "new", 0
            else:
                diff = prev_rank - stock["rank"]
                stock["rank_diff"] = diff
                stock["rank_status"] = "up" if diff > 0 else ("down" if diff < 0 else "stable")
            enriched.append(stock)

        last_processed_time = datetime.now().strftime("%d %b %Y %I:%M %p")
        
        # 4. Update Session History
        history = session.get('volar_history', [])
        history.insert(0, {"time": last_processed_time, "source": source_name, "count": len(enriched), "leaders_90": leaders_90})
        session['volar_history'] = history[:5]
        session.modified = True

        # 5. Save Results
        with open(RESULTS_JSON, 'w') as f:
            json.dump({'stocks': enriched, 'time': last_processed_time, 'source': source_name}, f)
        stocks = enriched
    
    # 6. Final logic for History and Comparison
    history = session.get('volar_history', [])
    if compare_mode and len(history) >= 3:
        leader_sets = [set(h.get('leaders_90', [])) for h in history[:3]]
        consistent_symbols = set.intersection(*leader_sets) if leader_sets else set()
        for s in stocks:
            if s.get('symbol_clean') in consistent_symbols:
                s['is_consistent'] = True

    return render_template("stage2_volar.html", 
                           stocks=stocks, 
                           last_processed_time=last_processed_time, 
                           source_name=source_name, 
                           history=history,
                           compare_mode=compare_mode)

@volar_bp.route("/export-volar")
def export_volar():
    """Generates a timestamped CSV export of the last scan"""
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            data = json.load(f)
        stocks = data.get('stocks', [])
        if stocks:
            df = pd.DataFrame(stocks)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            export_filename = f"Volar_Screener_{timestamp}.csv"
            export_path = os.path.join(UPLOAD_FOLDER, 'temp_export.csv')
            df.to_csv(export_path, index=False)
            return send_file(export_path, as_attachment=True, download_name=export_filename)
    return "No data to export", 404

@volar_bp.route("/add-favorite-india", methods=["POST"])
def add_favorite_india():
    """Saves symbol to favorites_india.json"""
    symbol = request.form.get('symbol')
    fav_path = os.path.join(UPLOAD_FOLDER, 'favorites_india.json')
    favorites = []
    if os.path.exists(fav_path):
        with open(fav_path, 'r') as f:
            favorites = json.load(f)
    if symbol not in favorites:
        favorites.append(symbol)
        with open(fav_path, 'w') as f:
            json.dump(favorites, f)
    return {"status": "success", "message": f"{symbol} added to India Watchlist"}

@volar_bp.route("/view-favorites-india")
def view_favorites_india():
    """Displays performance of saved India symbols"""
    fav_path = os.path.join(UPLOAD_FOLDER, 'favorites_india.json')
    stocks = []
    if os.path.exists(fav_path):
        with open(fav_path, 'r') as f:
            symbols = json.load(f)
        for sym in symbols:
            try:
                yf_sym = sym if sym.endswith(".NS") else f"{sym}.NS"
                data = is_volar_candidate(yf_sym)
                if data:
                    data['symbol_clean'] = sym
                    stocks.append(data)
            except Exception as e:
                print(f"Error loading favorite {sym}: {e}")
                continue
    return render_template("view_favorites.html", stocks=stocks, market="India", currency="₹")

@volar_bp.route("/remove-favorite-india", methods=["POST"])
def remove_favorite_india():
    """Removes symbol from favorites_india.json"""
    symbol = request.form.get('symbol')
    fav_path = os.path.join(UPLOAD_FOLDER, 'favorites_india.json')
    if os.path.exists(fav_path):
        with open(fav_path, 'r') as f:
            favorites = json.load(f)
        if symbol in favorites:
            favorites.remove(symbol)
            with open(fav_path, 'w') as f:
                json.dump(favorites, f)
    return {"status": "success"}

@volar_bp.route("/add-to-strategy", methods=["POST"])
def add_to_strategy():
    symbol = request.form.get('symbol')
    strategy = request.form.get('strategy')  # e.g., 'Volar Stage 2', 'Swing', 'Breakout'
    market = request.form.get('market')      # 'india' or 'us'
    
    # Determine file path based on market
    folder = UPLOAD_FOLDER if market == 'india' else os.path.join(os.getcwd(), 'uploads', 'volar_us')
    fav_path = os.path.join(folder, f'strategy_{strategy.lower().replace(" ", "_")}.json')
    
    # Fetch current price to lock it in as the Entry Price
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
    
    # Avoid duplicates
    if not any(item['symbol'] == symbol for item in data):
        data.append(new_entry)
        with open(fav_path, 'w') as f:
            json.dump(data, f)
            
    return {"status": "success", "message": f"{symbol} added to {strategy} strategy at ${round(current_price, 2)}"}

@volar_bp.route("/view-strategy/<name>")
def view_strategy(name):
    market = request.args.get('market', 'india')
    folder = UPLOAD_FOLDER if market == 'india' else os.path.join(os.getcwd(), 'uploads', 'volar_us')
    file_path = os.path.join(folder, f'strategy_{name.lower()}.json')
    
    performance_data = []
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            entries = json.load(f)
        
        for item in entries:
            yf_sym = item['symbol'] if market == 'us' else f"{item['symbol']}.NS"
            ticker = yf.Ticker(yf_sym)
            current_price = ticker.history(period="1d")['Close'].iloc[-1]
            
            # Calculate Return %
            # Formula: ((Current - Entry) / Entry) * 100
            ret_pct = ((current_price - item['entry_price']) / item['entry_price']) * 100
            
            performance_data.append({
                "symbol": item['symbol'],
                "entry_date": item['entry_date'],
                "entry_price": item['entry_price'],
                "current_price": round(current_price, 2),
                "return_pct": round(ret_pct, 2)
            })
            
    return render_template("strategy_watchlist.html", 
                           stocks=performance_data, 
                           strategy_name=name.upper(),
                           currency="₹" if market == 'india' else "$")