import os
import json
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Blueprint, render_template, request, session, current_app

volar_ind_adaptive_bp = Blueprint('volar_ind_adaptive_bp', __name__)

# Folder for Indian market uploads
UPLOAD_FOLDER = os.path.join(os.getcwd(), 'uploads', 'volar_ind_adaptive')
os.makedirs(UPLOAD_FOLDER, exist_ok=True) 

RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'volar_results_ind_adaptive.json')

def compute_volar(prices):
    if len(prices) < 2: return 0
    returns = prices.pct_change().dropna()
    std = returns.std()
    total_ret = (prices.iloc[-1] / prices.iloc[0]) - 1
    return round(total_ret / std, 2) if std != 0 else 0

def is_volar_adaptive_ind(symbol, lookback):
    try:
        # Append .NS for NSE symbols if not present
        ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
        ticker = yf.Ticker(ticker_symbol)
        index_ticker = yf.Ticker("^NSEI") # Benchmark for India: NIFTY 50
        
        df = ticker.history(period=f"{lookback + 50}d")
        idx_df = index_ticker.history(period=f"{lookback + 50}d")

        if len(df) < lookback or len(idx_df) < lookback: return None

        close = df['Close']
        curr = close.iloc[-1]
        start = close.iloc[-lookback]
        # Using 200 EMA for Stage 2 Trend filtering
        ema200 = close.ewm(span=200).mean().iloc[-1]
        
        perf = (curr / start) - 1
        idx_perf = (idx_df['Close'].iloc[-1] / idx_df['Close'].iloc[-lookback]) - 1
        
        # Adaptive RS Ratio relative to Nifty 50
        rs_ratio = perf / idx_perf if idx_perf != 0 else 0
        volar_val = compute_volar(close.iloc[-lookback:])

        # Stage 2 Condition: Price above 200 EMA
        if curr > ema200:
            return {
                "symbol": symbol.replace(".NS", ""),
                "price": round(curr, 2),
                "volar": volar_val,
                "relative_strength": round(rs_ratio, 4),
                "performance": round(perf * 100, 2)
            }
    except:
        return None

@volar_ind_adaptive_bp.route("/volar-ind-adaptive", methods=["GET", "POST"])
def volar_ind_process():
    stocks = []
    last_processed_time = None
    source_name = "None"
    
    if 'scan_history_ind' not in session:
        session['scan_history_ind'] = []

    if request.method == "POST":
        lookback = int(request.form.get('lookback', 50))
        file = request.files.get('file')
        
        if file and file.filename != '':
            filepath = os.path.join(UPLOAD_FOLDER, file.filename)
            file.save(filepath)
            session['last_uploaded_csv_ind'] = filepath
            session['last_filename_ind'] = file.filename
        
        saved_path = session.get('last_uploaded_csv_ind')
        
        if saved_path and os.path.exists(saved_path):
            df_input = pd.read_csv(saved_path)
            symbols = df_input['symbol'].dropna().unique().tolist()
            
            raw_results = []
            for sym in symbols:
                res = is_volar_adaptive_ind(sym, lookback)
                if res: raw_results.append(res)
            
            if raw_results:
                df = pd.DataFrame(raw_results)
                df['rs_percentile'] = df['relative_strength'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)
                df = df.sort_values('relative_strength', ascending=False).reset_index(drop=True)
                df['rank'] = df.index + 1
                stocks = df.to_dict(orient='records')
            
            last_processed_time = datetime.now().strftime("%Y-%m-%d %H:%M")
            source_name = f"{session.get('last_filename_ind')} ({lookback}d)"
            
            with open(RESULTS_JSON, 'w') as f:
                json.dump({'stocks': stocks, 'time': last_processed_time, 'source': source_name}, f)
            
            history = session['scan_history_ind']
            history.insert(0, {"time": last_processed_time, "source": session.get('last_filename_ind'), "lookback": f"{lookback}d", "count": len(stocks)})
            session['scan_history_ind'] = history[:5]
            session.modified = True

    elif os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cache = json.load(f)
            stocks = cache.get('stocks', [])
            last_processed_time = cache.get('time')
            source_name = cache.get('source')

    return render_template("stage2_adaptive_volar_ind_scr.html", 
                           stocks=stocks, 
                           last_processed_time=last_processed_time, 
                           source_name=source_name, 
                           history=session.get('scan_history_ind', []),
                           active_file=session.get('last_filename_ind'))