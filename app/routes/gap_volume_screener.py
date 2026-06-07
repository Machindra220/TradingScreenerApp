import os
import json
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Blueprint, render_template, request, send_file, session
from werkzeug.utils import secure_filename

gap_vol_bp = Blueprint("gap_volume", __name__)

# --- SRE PATH COMPARTMENTALIZATION ---
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'gap_volume'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_gap_vol_results.json')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def fetch_screener_data(symbol, days=252):
    """Fetches historical timeframe streams safely."""
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=f"{days}d", interval="1d")
    return df

def check_gap_up_history(df, lookback_days=7, gap_threshold=0.01):
    """Verifies if the stock opened with a gap-up over the previous high within the lookback window."""
    if len(df) < (lookback_days + 1):
        return False
        
    # Isolate the most recent trailing window
    recent_df = df.tail(lookback_days + 1)
    
    for i in range(1, len(recent_df)):
        current_open = recent_df['Open'].iloc[i]
        prev_high = recent_df['High'].iloc[i-1]
        
        if prev_high > 0 and (current_open - prev_high) / prev_high >= gap_threshold:
            return True
            
    return False

def check_volume_breakout(df):
    """Checks if today's volume breaks out past the 5-day high and flags abnormal volume spikes."""
    if len(df) < 21:
        return False, False, 1.0
        
    current_vol = df['Volume'].iloc[-1]
    prev_5d_vol_max = df['Volume'].iloc[-6:-1].max()
    avg_20d_vol = df['Volume'].iloc[-21:-1].mean()
    
    is_breakout = current_vol > prev_5d_vol_max
    is_abnormal = current_vol > (avg_20d_vol * 2.5) if avg_20d_vol > 0 else False
    vol_ratio = current_vol / avg_20d_vol if avg_20d_vol > 0 else 1.0
    
    return is_breakout, is_abnormal, round(vol_ratio, 2)

def compute_3m_relative_strength(stock_df, index_df):
    """Calculates 3-Month Trend Relative Strength against the S&P 500 baseline index."""
    # 3 Months matches roughly 63 active trading sessions
    if len(stock_df) < 63 or len(index_df) < 63:
        return 0.0
        
    stock_return = (stock_df['Close'].iloc[-1] / stock_df['Close'].iloc[-63]) - 1
    index_return = (index_df['Close'].iloc[-1] / index_df['Close'].iloc[-63]) - 1
    
    return stock_return - index_return

def run_technical_screening(symbol, index_df):
    """Evaluates the composite technical criteria checklist."""
    try:
        df = fetch_screener_data(symbol, days=252)
        if df.empty or len(df) < 200:
            return None
            
        close = df["Close"]
        current_price = close.iloc[-1]
        high_52w = close.max()
        pullback = (high_52w - current_price) / high_52w
        ema_200 = close.ewm(span=200).mean().iloc[-1]
        
        # 1. Evaluate Stage 2 Base Framework
        is_stage_2 = pullback < 0.30 and current_price > ema_200
        if not is_stage_2:
            return None
            
        # 2. Evaluate Gap Up Conditions
        has_gap = check_gap_up_history(df, lookback_days=7, gap_threshold=0.01)
        if not has_gap:
            return None
            
        # 3. Evaluate Volume Parameters
        is_vol_breakout, is_high_vol, volume_ratio = check_volume_breakout(df)
        if not is_vol_breakout:
            return None
            
        # 4. Extract 3-Month Alpha Score
        rs_3m_score = compute_3m_relative_strength(df, index_df)
        
        return {
            "symbol": symbol,
            "price": round(current_price, 2),
            "pullback_pct": round(pullback * 100, 2),
            "volume_ratio": volume_ratio,
            "high_volume_alert": is_high_vol,
            "rs_3m_score": float(rs_3m_score)
        }
    except Exception:
        return None

def execute_pipeline_scan(symbols):
    """Orchestrates cross-sectional sorting layers over raw matrix matches."""
    try:
        index_df = fetch_screener_data("^GSPC", days=252)
    except Exception:
        return []
        
    results = []
    for sym in symbols:
        res = run_technical_screening(sym, index_df)
        if res:
            results.append(res)
            
    if not results:
        return []
        
    df = pd.DataFrame(results)
    # Generate the cross-sectional percentile ranks relative to all scanned matches
    df['rs_percentile'] = df['rs_3m_score'].rank(pct=True).mul(100).round(0).astype(int)
    
    df.sort_values(by="rs_percentile", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    df["rank"] = df.index + 1
    return df.to_dict(orient="records")

# --- FLASK APP INTERACTION CONTROLLERS ---

@gap_vol_bp.route("/gap-volume-scan", methods=["GET", "POST"])
def gap_volume_scan_process():
    stocks = []
    last_processed_time = None
    source_name = "None"
    
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cache = json.load(f)
            stocks = cache.get('stocks', [])
            last_processed_time = cache.get('time')
            source_name = cache.get('source', 'Cached Scan')

    if request.method == "POST":
        file = request.files.get('file')
        
        if file and file.filename != '':
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            source_name = filename
        else:
            filepath = os.path.abspath(os.path.join(os.getcwd(), 'data', 'sp500.csv'))
            source_name = "S&P 500 Default"

        if not os.path.exists(filepath):
            return render_template("gap_volume_screener.html", error=f"Universe path target missing: {filepath}")

        df_input = pd.read_csv(filepath)
        col_name = 'Symbol' if 'Symbol' in df_input.columns else 'symbol'
        symbols = [str(s).strip().upper() for s in df_input[col_name].dropna().unique()]
        
        stocks = execute_pipeline_scan(symbols)
        last_processed_time = datetime.now().strftime("%d %b %Y %I:%M %p")
        
        with open(RESULTS_JSON, 'w') as f:
            json.dump({'stocks': stocks, 'time': last_processed_time, 'source': source_name}, f)

    return render_template("gap_volume_screener.html", stocks=stocks, 
                           last_processed_time=last_processed_time, 
                           source_name=source_name)