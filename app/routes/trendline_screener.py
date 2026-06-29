import os
import json
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime
from scipy.signal import find_peaks
from flask import Blueprint, render_template, request, send_file

trendline_bp = Blueprint("trendline_screener", __name__)

UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'trendline'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_trendline_results.json')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def fetch_historical_matrix(symbol, period="1y"):
    ticker = yf.Ticker(symbol)
    return ticker.history(period=period, interval="1d")

def calculate_rsi(prices, period=14):
    """Calculates high-fidelity 14-Day RSI from raw price arrays."""
    if len(prices) < period + 1:
        return 50.0
    delta = np.diff(prices)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    
    avg_gain = np.mean(gain[:period])
    avg_loss = np.mean(loss[:period])
    
    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)

def detect_high_confidence_breakout(df):
    """
    Fits a linear resistance trendline, analyzes volume footprint against 
    prior technical peak volumes, and tracks RSI momentum parameters.
    """
    if len(df) < 60:
        return False, 0.0, 0.0, 1.0, 50.0, False
        
    highs = df['High'].values
    closes = df['Close'].values
    volumes = df['Volume'].values
    x_ticks = np.arange(len(df))
    
    # 1. Structural Peak Extraction
    peaks, _ = find_peaks(highs, distance=12, prominence=highs.mean() * 0.01)
    recent_peaks = [p for p in peaks if (len(df) - p) <= 180]
    
    if len(recent_peaks) < 2:
        return False, 0.0, 0.0, 1.0, 50.0, False
        
    peak_x = x_ticks[recent_peaks]
    peak_y = highs[recent_peaks]
    
    # 2. Geometry Fit (Linear Polynomial Regression)
    slope, intercept = np.polyfit(peak_x, peak_y, 1)
    if slope >= 0:
        return False, 0.0, 0.0, 1.0, 50.0, False
        
    today_idx = len(df) - 1
    yesterday_idx = len(df) - 2
    
    trendline_today = (slope * today_idx) + intercept
    trendline_yesterday = (slope * yesterday_idx) + intercept
    
    # Core Geometric Cross Verification
    is_price_breakout = (closes[yesterday_idx] <= trendline_yesterday) and (closes[today_idx] > trendline_today)
    
    if not is_price_breakout:
        return False, 0.0, 0.0, 1.0, 50.0, False

    # 3. 🛡️ Quantitative Institutional Volume Analysis
    today_volume = volumes[-1]
    avg_20d_volume = volumes[-21:-1].mean() if volumes[-21:-1].mean() > 0 else 1.0
    
    # Extract the volume recorded at previous false peak resistance turns
    historical_peak_volumes = volumes[recent_peaks]
    mean_peak_volume = historical_peak_volumes.mean() if len(historical_peak_volumes) > 0 else avg_20d_volume
    
    # Confirm volume expands past both normal trendlines AND previous peak limits
    is_volume_confirmed = (today_volume > avg_20d_volume * 1.5) and (today_volume >= mean_peak_volume * 0.9)
    volume_ratio = round(today_volume / avg_20d_volume, 2)
    
    # 4. Momentum Verification
    current_rsi = calculate_rsi(closes, period=14)
    is_momentum_confirmed = 55 <= current_rsi <= 72
    
    is_high_confidence = is_volume_confirmed and is_momentum_confirmed
    
    return True, round(trendline_today, 2), round(slope, 4), volume_ratio, current_rsi, is_high_confidence

def check_52w_high_breakout(df):
    if len(df) < 252:
        return False, 0.0
    closes = df['Close'].values
    highs = df['High'].values
    current_close = closes[-1]
    past_52w_high = highs[-252:-1].max()
    
    is_52w_breakout = current_close >= (past_52w_high * 0.985)
    return is_52w_breakout, round(past_52w_high, 2)

def execute_algorithmic_scan(symbols, market_type="US"):
    results = []
    for sym in symbols:
        sym_clean = str(sym).strip().upper()
        if market_type == "INDIA" and not sym_clean.endswith((".NS", ".BO")):
            sym_clean = f"{sym_clean}.NS"
            
        try:
            df = fetch_historical_matrix(sym_clean, period="1y")
            if df.empty or len(df) < 100:
                continue
                
            current_price = df['Close'].iloc[-1]
            has_tl_break, tl_val, tl_slope, vol_ratio, rsi_val, is_high_conf = detect_high_confidence_breakout(df)
            has_52w_break, past_high = check_52w_high_breakout(df)
            
            if not has_tl_break and not has_52w_break:
                continue
                
            results.append({
                "symbol": sym_clean.replace(".NS", ""),
                "price": round(current_price, 2),
                "has_trendline_break": has_tl_break,
                "trendline_value": tl_val,
                "trendline_slope": tl_slope,
                "volume_ratio": vol_ratio,
                "rsi": rsi_val,
                "high_confidence": is_high_conf,
                "has_52w_break": has_52w_break,
                "past_52w_high": past_high
            })
        except Exception:
            continue
            
    if not results:
        return [], [], []
        
    df_res = pd.DataFrame(results)
    df_both = df_res[df_res['has_trendline_break'] & df_res['has_52w_break']].copy()
    df_tl_only = df_res[df_res['has_trendline_break'] & ~df_res['has_52w_break']].copy()
    df_52w_only = df_res[df_res['has_52w_break'] & ~df_res['has_trendline_break']].copy()
    
    def format_records(target_df):
        if target_df.empty: return []
        # Sort by high confidence profile markers first
        target_df.sort_values(by=["high_confidence", "volume_ratio"], ascending=[False, False], inplace=True)
        target_df.reset_index(drop=True, inplace=True)
        target_df["rank"] = target_df.index + 1
        return target_df.to_dict(orient="records")
        
    return format_records(df_both), format_records(df_tl_only), format_records(df_52w_only)

# --- FLASK APP INTERACTION ROUTERS ---

@trendline_bp.route("/trendline-scan", methods=["GET", "POST"])
def trendline_scan_process():
    sections = {"both": [], "tl_only": [], "high_52w_only": []}
    last_processed_time = None
    source_name = "None"
    market = request.args.get('market', 'US').upper()
    
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cache = json.load(f)
            if cache.get('market') == market:
                sections = cache.get('sections', sections)
                last_processed_time = cache.get('time')
                source_name = cache.get('source', 'Cached Scan')

    if request.method == "POST":
        file = request.files.get('file')
        if file and file.filename != '':
            from werkzeug.utils import secure_filename
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            source_name = filename
            
            df_input = pd.read_csv(filepath)
            col_name = 'Symbol' if 'Symbol' in df_input.columns else 'symbol'
            symbols = [str(s).strip().upper() for s in df_input[col_name].dropna().unique()]
        else:
            if market == "INDIA":
                source_name = "Nifty 500 Live List"
                url = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
                symbols = pd.read_csv(url)['Symbol'].dropna().tolist()
            else:
                source_name = "S&P 500 Default"
                filepath = os.path.abspath(os.path.join(os.getcwd(), 'data', 'snp500.csv'))
                df_input = pd.read_csv(filepath)
                col_name = 'Symbol' if 'Symbol' in df_input.columns else 'symbol'
                symbols = df_input[col_name].dropna().tolist()

        both, tl, h52w = execute_algorithmic_scan(symbols, market_type=market)
        sections = {"both": both, "tl_only": tl, "high_52w_only": h52w}
        last_processed_time = datetime.now().strftime("%d %b %Y %I:%M %p")
        
        with open(RESULTS_JSON, 'w') as f:
            json.dump({'sections': sections, 'time': last_processed_time, 'source': source_name, 'market': market}, f)

    return render_template("trendline_screener.html", 
                           both_stocks=sections["both"],
                           tl_stocks=sections["tl_only"],
                           high_52w_stocks=sections["high_52w_only"],
                           last_processed_time=last_processed_time, 
                           source_name=source_name, market=market)

@trendline_bp.route("/export-trendline-csv")
def export_trendline_csv():
    market = request.args.get('market', 'US').upper()
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            data = json.load(f)
        if data.get('market') == market:
            sections = data.get('sections', {})
            all_records = []
            for label, items in sections.items():
                for item in items:
                    record = item.copy()
                    record["category_profile"] = label.upper()
                    all_records.append(record)
            if all_records:
                df = pd.DataFrame(all_records)
                export_path = os.path.join(UPLOAD_FOLDER, 'temp_trendline_export.csv')
                df.to_csv(export_path, index=False)
                return send_file(export_path, as_attachment=True, download_name=f"{market}_High_Confidence_Breakouts.csv")
    return "No active data layer", 404