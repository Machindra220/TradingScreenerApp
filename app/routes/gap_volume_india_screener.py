import os
import json
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Blueprint, render_template, request

gap_vol_india_bp = Blueprint("gap_volume_india", __name__)

# --- SRE PATH COMPARTMENTALIZATION ---
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'gap_volume_india'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_gap_vol_india_results.json')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def fetch_screener_data(symbol, days=252):
    """Fetches historical daily data lines cleanly from yfinance."""
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=f"{days}d", interval="1d")
    return df

def check_gap_up_history(df, lookback_days=7, gap_threshold=0.01):
    """Verifies if the stock opened with a gap-up over the previous high within the last 7 days."""
    if len(df) < (lookback_days + 1):
        return False
        
    recent_df = df.tail(lookback_days + 1)
    for i in range(1, len(recent_df)):
        current_open = recent_df['Open'].iloc[i]
        prev_high = recent_df['High'].iloc[i-1]
        
        if prev_high > 0 and (current_open - prev_high) / prev_high >= gap_threshold:
            return True
    return False

def check_volume_breakout(df):
    """Checks for a 5-day volume breakout and flags abnormal institutional volume."""
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
    """Calculates 3-Month Trend Relative Strength against the Nifty 500 baseline (^CRSLDX)."""
    if len(stock_df) < 63 or len(index_df) < 63:
        return 0.0
        
    stock_return = (stock_df['Close'].iloc[-1] / stock_df['Close'].iloc[-63]) - 1
    index_return = (index_df['Close'].iloc[-1] / index_df['Close'].iloc[-63]) - 1
    
    return stock_return - index_return

def run_technical_screening(symbol, index_df):
    """Evaluates technical indicators and categorizes candidate vectors."""
    try:
        df = fetch_screener_data(symbol, days=252)
        if df.empty or len(df) < 200:
            return None
            
        close = df["Close"]
        current_price = close.iloc[-1]
        high_52w = close.max()
        pullback = (high_52w - current_price) / high_52w
        ema_200 = close.ewm(span=200).mean().iloc[-1]
        
        # 1. Base Stage 2 Structural Verification
        is_stage_2 = pullback < 0.30 and current_price > ema_200
        if not is_stage_2:
            return None
            
        # 2. Extract Individual Trigger Components
        has_gap = check_gap_up_history(df, lookback_days=7, gap_threshold=0.01)
        is_vol_breakout, is_high_vol, volume_ratio = check_volume_breakout(df)
        
        # If it doesn't pass either indicator, skip it entirely
        if not has_gap and not is_vol_breakout:
            return None
            
        rs_3m_score = compute_3m_relative_strength(df, index_df)
        
        return {
            "symbol": symbol.replace(".NS", ""),
            "price": round(current_price, 2),
            "pullback_pct": round(pullback * 100, 2),
            "volume_ratio": volume_ratio,
            "high_volume_alert": is_high_vol,
            "rs_3m_score": float(rs_3m_score),
            "has_gap": has_gap,
            "has_vol": is_vol_breakout
        }
    except Exception:
        return None

def execute_pipeline_scan(symbols):
    """Orchestrates segments and applies cross-sectional percentile scaling layers."""
    try:
        index_df = fetch_screener_data("^CRSLDX", days=252)
    except Exception:
        return {}, {}, {}
        
    results = []
    for sym in symbols:
        sym_clean = str(sym).strip().upper()
        if not sym_clean.endswith((".NS", ".BO")):
            sym_clean = f"{sym_clean}.NS"
            
        res = run_technical_screening(sym_clean, index_df)
        if res:
            results.append(res)
            
    if not results:
        return [], [], []
        
    df = pd.DataFrame(results)
    # Generate unified cross-sectional percentile scaling across the total matches
    df['rs_percentile'] = df['rs_3m_score'].rank(pct=True).mul(100).round(0).astype(int)
    
    # Segment matrices into explicit buckets based on trigger criteria
    df_both = df[df['has_gap'] & df['has_vol']].copy()
    df_vol_only = df[df['has_vol'] & ~df['has_gap']].copy()
    df_gap_only = df[df['has_gap'] & ~df['has_vol']].copy()
    
    def process_section(section_df):
        if section_df.empty: return []
        section_df.sort_values(by="rs_percentile", ascending=False, inplace=True)
        section_df.reset_index(drop=True, inplace=True)
        section_df["rank"] = section_df.index + 1
        return section_df.to_dict(orient="records")

    return process_section(df_both), process_section(df_vol_only), process_section(df_gap_only)

# --- FLASK INTERACTION CONTROLLERS ---

@gap_vol_india_bp.route("/gap-volume-india-scan", methods=["GET", "POST"])
def gap_volume_india_process():
    sections = {"both": [], "vol_only": [], "gap_only": []}
    last_processed_time = None
    source_name = "None"
    
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cache = json.load(f)
            sections = cache.get('sections', sections)
            last_processed_time = cache.get('time')
            source_name = cache.get('source', 'Cached Scan')

    if request.method == "POST":
        file = request.files.get('file')
        
        if file and file.filename != '':
            import werkzeug
            filename = werkzeug.utils.secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            source_name = filename
            
            df_input = pd.read_csv(filepath)
            col_name = 'Symbol' if 'Symbol' in df_input.columns else 'symbol'
            symbols = [str(s).strip().upper() for s in df_input[col_name].dropna().unique()]
        else:
            source_name = "Nifty 500 Live Archive"
            try:
                url = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
                nse_df = pd.read_csv(url)
                symbols = nse_df['Symbol'].dropna().tolist()
            except Exception as e:
                return render_template("gap_volume_india_screener.html", error=f"NSE link exception: {e}")

        both, vol, gap = execute_pipeline_scan(symbols)
        sections = {"both": both, "vol_only": vol, "gap_only": gap}
        last_processed_time = datetime.now().strftime("%d %b %Y %I:%M %p")
        
        with open(RESULTS_JSON, 'w') as f:
            json.dump({'sections': sections, 'time': last_processed_time, 'source': source_name}, f)

    return render_template("gap_volume_india_screener.html", 
                           both_stocks=sections["both"],
                           vol_stocks=sections["vol_only"],
                           gap_stocks=sections["gap_only"],
                           last_processed_time=last_processed_time, 
                           source_name=source_name)