import os
import json
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Blueprint, render_template, request, send_file

gap_vol_india_bp = Blueprint("gap_volume_india", __name__)

# --- SRE PATH COMPARTMENTALIZATION ---
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'gap_volume_india'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_gap_vol_india_results.json')
LAST_CSV_CONFIG = os.path.join(UPLOAD_FOLDER, 'last_csv_india_config.json')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def fetch_screener_data(symbol, days=252):
    """Fetches historical daily data lines cleanly from yfinance."""
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=f"{days}d", interval="1d")
    return df

def check_gap_up_history(df, lookback_days=7, gap_threshold=0.01):
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
    if len(df) < 21:
        return False, False, 1.0
    current_vol = df['Volume'].iloc[-1]
    prev_5d_vol_max = df['Volume'].iloc[-6:-1].max()
    avg_20d_vol = df['Volume'].iloc[-21:-1].mean()
    is_breakout = current_vol > prev_5d_vol_max
    is_abnormal = current_vol > (avg_20d_vol * 2.5) if avg_20d_vol > 0 else False
    vol_ratio = current_vol / avg_20d_vol if avg_20d_vol > 0 else 1.0
    return is_breakout, is_abnormal, round(vol_ratio, 2)

def compute_3m_volar_strength(stock_df, index_df):
    """
    LOCALIZED FIX: Calculates 3-Month Volatility-Adjusted Alpha (VOLAR Strategy)
    instead of basic raw index outperformance returns.
    """
    if len(stock_df) < 63 or len(index_df) < 63:
        return 0.0
        
    # Isolate trailing 3-month window vectors (approx 63 trading sessions)
    stock_cls = stock_df['Close'].tail(63)
    
    total_return = (stock_cls.iloc[-1] / stock_cls.iloc[0]) - 1
    volatility = stock_cls.pct_change(fill_method=None).std()
    
    # Return volatility-adjusted return ratio score
    return total_return / volatility if volatility != 0 else 0.0

def run_technical_screening(symbol, index_df):
    """Evaluates indicators with localized Indian market structural guardrails."""
    try:
        df = fetch_screener_data(symbol, days=252)
        if df.empty or len(df) < 200:
            return None
            
        close = df["Close"].iloc[-1]
        high = df["High"].iloc[-1]
        low = df["Low"].iloc[-1]
        
        # 🛡️ LOCALIZED GUARDRAIL A: THE INTRADAY CLOSING RANGE SECURITY FILTER
        # Ensures institutions actively sustained the breakout into the closing bell.
        daily_range = high - low
        if daily_range > 0:
            closing_pct = (close - low) / daily_range
            if closing_pct < 0.75:  # Must close in the upper 25% of the daily boundary candlestick
                return None
        else:
            return None
            
        high_52w = df["Close"].max()
        pullback = (high_52w - close) / high_52w
        ema_200 = df["Close"].ewm(span=200).mean().iloc[-1]
        
        is_stage_2 = pullback < 0.30 and close > ema_200
        if not is_stage_2:
            return None
            
        has_gap = check_gap_up_history(df, lookback_days=7, gap_threshold=0.01)
        is_vol_breakout, is_high_vol, volume_ratio = check_volume_breakout(df)
        if not has_gap and not is_vol_breakout:
            return None
            
        # Compute localized Volatility-Adjusted Alpha score values
        volar_alpha = compute_3m_volar_strength(df, index_df)
        
        return {
            "symbol": symbol.replace(".NS", ""),
            "price": round(close, 2),
            "pullback_pct": round(pullback * 100, 2),
            "volume_ratio": volume_ratio,
            "high_volume_alert": is_high_vol,
            "volar_alpha": float(volar_alpha),
            "has_gap": has_gap,
            "has_vol": is_vol_breakout
        }
    except Exception:
        return None

def execute_pipeline_scan(symbols):
    try:
        index_df = fetch_screener_data("^CRSLDX", days=252)
    except Exception:
        return [], [], []
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
    # Generate unified cross-sectional ranks utilizing the new VOLAR safety values
    df['rs_percentile'] = df['volar_alpha'].rank(pct=True).mul(100).round(0).astype(int)
    
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

# --- CONTROLLERS ---

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
            with open(LAST_CSV_CONFIG, 'w') as f:
                json.dump({'path': filepath, 'name': filename}, f)
            source_name = filename
            
            df_input = pd.read_csv(filepath)
            col_name = 'Symbol' if 'Symbol' in df_input.columns else 'symbol'
            symbols = [str(s).strip().upper() for s in df_input[col_name].dropna().unique()]
        else:
            if os.path.exists(LAST_CSV_CONFIG):
                with open(LAST_CSV_CONFIG, 'r') as f:
                    cfg = json.load(f)
                filepath = cfg.get('path')
                source_name = cfg.get('name')
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

@gap_vol_india_bp.route("/export-gap-volume-india")
def export_gap_volume_india():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            data = json.load(f)
        sections = data.get('sections', {})
        all_records = []
        for label, items in sections.items():
            for item in items:
                record = item.copy()
                record["screener_section"] = label.upper()
                all_records.append(record)
        if all_records:
            df = pd.DataFrame(all_records)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            export_filename = f"India_Gap_Volume_Combined_{timestamp}.csv"
            export_path = os.path.join(UPLOAD_FOLDER, 'temp_export_gap_vol_india.csv')
            df.to_csv(export_path, index=False)
            return send_file(export_path, as_attachment=True, download_name=export_filename)
    return "No dataset found.", 404