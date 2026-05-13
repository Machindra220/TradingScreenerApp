import os
import json
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Blueprint, render_template, request, session, current_app

volar_us_adaptive_bp = Blueprint('volar_us_adaptive_bp', __name__)

UPLOAD_FOLDER = os.path.join(os.getcwd(), 'uploads', 'volar_us_adaptive')
os.makedirs(UPLOAD_FOLDER, exist_ok=True) 

RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'volar_results_adaptive.json')

def compute_volar(prices):
    if len(prices) < 2: return 0
    returns = prices.pct_change().dropna()
    std = returns.std()
    total_ret = (prices.iloc[-1] / prices.iloc[0]) - 1
    return round(total_ret / std, 2) if std != 0 else 0

def is_volar_adaptive(symbol, lookback):
    try:
        ticker = yf.Ticker(symbol)
        index_ticker = yf.Ticker("^GSPC")
        df = ticker.history(period=f"{lookback + 55}d")
        idx_df = index_ticker.history(period=f"{lookback + 55}d")
        if len(df) < lookback or len(idx_df) < lookback: return None
        close = df['Close']
        curr, start = close.iloc[-1], close.iloc[-lookback]
        ema200 = close.ewm(span=200).mean().iloc[-1]
        perf = (curr / start) - 1
        idx_perf = (idx_df['Close'].iloc[-1] / idx_df['Close'].iloc[-lookback]) - 1
        rs_ratio = perf / idx_perf if idx_perf != 0 else 0
        volar_val = compute_volar(close.iloc[-lookback:])
        if curr > ema200:
            return {"symbol": symbol, "price": round(curr, 2), "volar": volar_val, "relative_strength": round(rs_ratio, 4), "performance": round(perf * 100, 2)}
    except: return None

@volar_us_adaptive_bp.route("/volar-us-adaptive", methods=["GET", "POST"])
def volar_us_process():
    stocks, last_processed_time, source_name = [], None, "None"
    if 'scan_history_us' not in session: session['scan_history_us'] = []
    if request.method == "POST":
        lookback = int(request.form.get('lookback', 55))
        file = request.files.get('file')
        if file and file.filename != '':
            filepath = os.path.join(UPLOAD_FOLDER, file.filename)
            file.save(filepath)
            session['last_uploaded_csv_us'], session['last_filename_us'] = filepath, file.filename
        saved_path = session.get('last_uploaded_csv_us')
        if saved_path and os.path.exists(saved_path):
            symbols = pd.read_csv(saved_path)['symbol'].dropna().unique().tolist()
            raw_results = [res for res in [is_volar_adaptive(s, lookback) for s in symbols] if res]
            if raw_results:
                df = pd.DataFrame(raw_results)
                df['rs_percentile'] = df['relative_strength'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)
                
                # Persistence Logic
                existing_history = {}
                if os.path.exists(RESULTS_JSON):
                    with open(RESULTS_JSON, 'r') as f:
                        old_cache = json.load(f).get('stocks', [])
                        existing_history = {s['symbol']: {'rs_h': s.get('rs_h', []), 'vol_h': s.get('vol_h', []), 'perf_h': s.get('perf_h', [])} for s in old_cache}

                def inject_history(row):
                    sym = row['symbol']
                    h = existing_history.get(sym, {'rs_h': [], 'vol_h': [], 'perf_h': []})
                    row['rs_h'], row['vol_h'], row['perf_h'] = (h['rs_h'] + [row['rs_percentile']])[-5:], (h['vol_h'] + [row['volar']])[-5:], (h['perf_h'] + [row['performance']])[-5:]
                    row['rs_up'] = len(row['rs_h']) > 1 and all(x < y for x, y in zip(row['rs_h'], row['rs_h'][1:]))
                    return row

                df = df.apply(inject_history, axis=1).sort_values('relative_strength', ascending=False).reset_index(drop=True)
                df['rank'] = df.index + 1
                stocks = df.to_dict(orient='records')
            last_processed_time, source_name = datetime.now().strftime("%Y-%m-%d %H:%M"), f"{session.get('last_filename_us')} ({lookback}d)"
            with open(RESULTS_JSON, 'w') as f: json.dump({'stocks': stocks, 'time': last_processed_time, 'source': source_name}, f)
            history = session['scan_history_us']
            history.insert(0, {"time": last_processed_time, "source": session.get('last_filename_us'), "lookback": f"{lookback}d", "count": len(stocks)})
            session['scan_history_us'] = history[:5]
            session.modified = True
    elif os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cache = json.load(f)
            stocks, last_processed_time, source_name = cache.get('stocks', []), cache.get('time'), cache.get('source')
    return render_template("stage2_adaptive_volar_us_scr.html", stocks=stocks, last_processed_time=last_processed_time, source_name=source_name, history=session.get('scan_history_us', []), active_file=session.get('last_filename_us'))