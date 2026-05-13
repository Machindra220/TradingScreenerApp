import os
import json
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Blueprint, render_template, request, send_file, session
from werkzeug.utils import secure_filename

volar_bp = Blueprint("volar", __name__)

# --- PATH LOGIC (Root Level) ---
# Ensures the 'uploads' folder is created in the project root
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'volar'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_volar_results.json')
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
    """Processes a list of symbols and calculates RS Percentiles"""
    results = []
    for sym in symbols:
        data = is_volar_candidate(sym)
        if data:
            results.append(data)
    
    if not results:
        return []
    
    df = pd.DataFrame(results)
    
    # --- RS Percentile Calculation ---
    # Ranks stocks against the current scanned universe
    df['rs_percentile'] = df['relative_strength'].rank(pct=True).mul(100).round(0).astype(int)
    
    df.sort_values(by="relative_strength", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    df["rank"] = df.index + 1
    return df.to_dict(orient="records")

@volar_bp.route("/volar", methods=["GET"])
def volar_view():
    stocks = []
    last_processed_time = None
    source_name = "None"
    compare_mode = request.args.get('compare') == 'true'
    
    # Load last results from JSON cache
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cache = json.load(f)
            stocks = cache.get('stocks', [])
            last_processed_time = cache.get('time')
            source_name = cache.get('source', 'Cached Scan')
    
    # Retrieve scan history from session
    history = session.get('volar_history', [])
    
    # Logic: Highlight consistent 90+ RS leaders across last 3 scans
    if compare_mode and len(history) >= 3:
        # Intersect leader sets from the last 3 sessions
        leader_sets = [set(h.get('leaders_90', [])) for h in history[:3]]
        consistent_symbols = set.intersection(*leader_sets) if leader_sets else set()
        
        for s in stocks:
            if s['symbol_clean'] in consistent_symbols:
                s['is_consistent'] = True

    return render_template("stage2_volar.html", 
                           stocks=stocks, 
                           last_processed_time=last_processed_time, 
                           source_name=source_name, 
                           history=history,
                           compare_mode=compare_mode)

@volar_bp.route("/volar", methods=["POST"])
def volar_process():
    file = request.files.get('file')
    # Default to nifty_500.csv in project data folder
    filepath = os.path.abspath(os.path.join(os.getcwd(), 'data', 'nifty_500.csv'))
    source_name = "Nifty 500"

    # Load Old Ranks to calculate Relative Performance
    old_ranks = {}
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            old_data = json.load(f)
            old_ranks = {s['symbol_clean']: s['rank'] for s in old_data.get('stocks', [])}

    # Handle custom file upload
    if file and file.filename != '':
        filename = secure_filename(file.filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        source_name = filename

    if not os.path.exists(filepath):
        return render_template("stage2_volar.html", error=f"File not found: {filepath}")

    df_input = pd.read_csv(filepath)
    col_name = 'Symbol' if 'Symbol' in df_input.columns else 'symbol'
    symbols = [str(s).strip() + ".NS" for s in df_input[col_name].dropna().unique()]
    
    results = screen_volar(symbols)
    enriched = []
    leaders_90 = []
    
    for stock in results:
        sym_clean = stock["symbol"].replace(".NS", "")
        stock["symbol_clean"] = sym_clean
        
        # Track 90+ Percentile Stocks for History Comparison
        if stock.get('rs_percentile', 0) >= 90:
            leaders_90.append(sym_clean)

        # Calculate Rank Difference
        prev_rank = old_ranks.get(sym_clean)
        if prev_rank is None:
            stock["rank_status"], stock["rank_diff"] = "new", 0
        else:
            diff = prev_rank - stock["rank"]
            stock["rank_diff"] = diff
            stock["rank_status"] = "up" if diff > 0 else ("down" if diff < 0 else "stable")
        enriched.append(stock)

    last_processed_time = datetime.now().strftime("%d %b %Y %I:%M %p")
    
    # --- UPDATE SESSION HISTORY ---
    # Stores metadata for the last 5 scans
    history = session.get('volar_history', [])
    history.insert(0, {
        "time": last_processed_time,
        "source": source_name,
        "count": len(enriched),
        "leaders_90": leaders_90  # Stored for comparison logic
    })
    session['volar_history'] = history[:5]
    session.modified = True

    # Cache current results to JSON
    with open(RESULTS_JSON, 'w') as f:
        json.dump({'stocks': enriched, 'time': last_processed_time, 'source': source_name}, f)

    return render_template("stage2_volar.html", 
                           stocks=enriched, 
                           last_processed_time=last_processed_time, 
                           source_name=source_name, 
                           history=session['volar_history'])

@volar_bp.route("/export-volar")
def export_volar():
    """Generates a timestamped CSV export of the last scan"""
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            data = json.load(f)
        stocks = data.get('stocks', [])
        if stocks:
            df = pd.DataFrame(stocks)
            # Dynamic Timestamped Filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            export_filename = f"Volar_Screener_{timestamp}.csv"
            export_path = os.path.join(UPLOAD_FOLDER, 'temp_export.csv')
            df.to_csv(export_path, index=False)
            
            return send_file(export_path, as_attachment=True, download_name=export_filename)
    return "No data to export", 404

@volar_bp.route("/add-favorite-india", methods=["POST"])
def add_favorite_india():
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
    fav_path = os.path.join(UPLOAD_FOLDER, 'favorites_india.json')
    stocks = []
    
    if os.path.exists(fav_path):
        with open(fav_path, 'r') as f:
            symbols = json.load(f)
        
        # Fetch fresh data for each favorite
        for sym in symbols:
            # Ensure .NS suffix for India stocks
            yf_sym = sym if sym.endswith(".NS") else f"{sym}.NS"
            data = is_volar_candidate(yf_sym)
            if data:
                data['symbol_clean'] = sym
                stocks.append(data)
                
    return render_template("view_favorites.html", stocks=stocks, market="India", currency="₹")

@volar_bp.route("/remove-favorite-india", methods=["POST"])
def remove_favorite_india():
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