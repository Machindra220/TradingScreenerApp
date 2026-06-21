import os
import json
import uuid
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Blueprint, render_template, request, send_file

ibd_engine_ind_bp = Blueprint("ibd_engine_ind", __name__)

# --- PATH COMPARTMENTALIZATION ---
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'ibd_india'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_ibd_india_results.json')
HISTORY_CACHE_DIR = os.path.join(UPLOAD_FOLDER, 'history_cache')
os.makedirs(HISTORY_CACHE_DIR, exist_ok=True)

def calculate_ad_rating(df, lookback=20):
    if len(df) < lookback:
        return "C"
    recent = df.tail(lookback)
    up_vol_sum = 0
    down_vol_sum = 0
    
    for i in range(len(recent)):
        close = recent['Close'].iloc[i]
        open_p = recent['Open'].iloc[i]
        vol = recent['Volume'].iloc[i]
        if close > open_p:
            up_vol_sum += vol
        else:
            down_vol_sum += vol
            
    if down_vol_sum == 0: return "A"
    ratio = up_vol_sum / down_vol_sum
    if ratio >= 1.6: return "A"
    elif ratio >= 1.3: return "B"
    elif ratio >= 0.9: return "C"
    elif ratio >= 0.6: return "D"
    else: return "E"

def compute_weighted_composite_score(rs_pct, eps_score, ad_grade):
    ad_map = {"A": 95, "B": 80, "C": 60, "D": 40, "E": 20}
    ad_score = ad_map.get(ad_grade, 60)
    composite = (rs_pct * 0.50) + (eps_score * 0.35) + (ad_score * 0.15)
    return int(round(composite, 0))

def run_ibd_india_screening_pipeline(symbols):
    try:
        # ✅ BENCHMARK UPDATE: Swapped ^NSEI for Nifty 500 Index vector (^CRSLDX)
        index_data = yf.download("^CRSLDX", period="1y", interval="1d", auto_adjust=True)
        if index_data.empty:
            print("❌ Failed to fetch broad-market index data (^CRSLDX).")
            return []
        index_df = index_data['Close']
        index_df.index = index_df.index.tz_localize(None)
    except Exception as e:
        print(f"❌ Index fetch exception: {e}")
        return []

    fundamentals = {}
    mock_fundamentals_path = os.path.join(UPLOAD_FOLDER, 'mock_fundamentals_india.json')
    if os.path.exists(mock_fundamentals_path):
        try:
            with open(mock_fundamentals_path, 'r') as f:
                fundamentals = json.load(f)
        except Exception as e:
            print(f"⚠️ Error parsing fundamentals json: {e}")

    raw_candidates = []
    print(f"📥 Fetching market data matrices for {len(symbols)} NSE tickers...")
    
    data = yf.download(symbols, period="1y", interval="1d", auto_adjust=True)
    if data.empty:
        return []

    data.index = data.index.tz_localize(None)

    for sym in symbols:
        try:
            if isinstance(data.columns, pd.MultiIndex):
                if sym not in data.columns.get_level_values(1):
                    continue
                stock_close = data.xs(sym, level=1, axis=1)['Close'].dropna()
                stock_open = data.xs(sym, level=1, axis=1)['Open'].dropna()
                stock_vol = data.xs(sym, level=1, axis=1)['Volume'].dropna()
            else:
                if len(symbols) == 1 or sym in data.columns:
                    stock_close = data['Close'].dropna()
                    stock_open = data['Open'].dropna()
                    stock_vol = data['Volume'].dropna()
                else:
                    continue

            if stock_close.empty or len(stock_close) < 150:
                continue
            
            current_price = stock_close.iloc[-1]
            ema_200 = stock_close.ewm(span=200, adjust=False).mean().iloc[-1]
            
            # Stage 2 Structural Guardrail Filter
            if current_price < ema_200:
                continue
            
            aligned_stock = stock_close.reindex(index_df.index, method='ffill').dropna()
            aligned_index = index_df.reindex(aligned_stock.index)
            
            if len(aligned_stock) < 20:
                continue
                
            # Calculate Technical RS Line Slope Intensity vs Nifty 500
            rs_ratio = aligned_stock.tail(20).values / aligned_index.tail(20).values
            slope_res, _ = np.polyfit(np.arange(len(rs_ratio)), rs_ratio, 1)
            
            if isinstance(slope_res, (np.ndarray, list)):
                raw_slope = float(slope_res[0])
            else:
                raw_slope = float(slope_res)
                
            is_rs_line_up = bool(raw_slope > 0)
            perf_1y = (current_price / stock_close.iloc[0]) - 1
            
            sym_clean = sym.replace(".NS", "")
            eps_score = fundamentals.get(sym_clean, {}).get("eps_rating", 75)
            
            stock_df = pd.DataFrame({"Open": stock_open, "Close": stock_close, "Volume": stock_vol})
            ad_grade = calculate_ad_rating(stock_df)
            
            raw_candidates.append({
                "symbol": sym_clean,
                "price": round(current_price, 2),
                "perf_1y": perf_1y,
                "eps_rating": eps_score,
                "ad_rating": ad_grade,
                "rs_line_up": is_rs_line_up
            })
        except Exception:
            continue

    if not raw_candidates:
        return []
    
    df = pd.DataFrame(raw_candidates)
    df['rs_percentile'] = df['perf_1y'].rank(pct=True).mul(100).round(0).astype(int)
    
    final_results = []
    for item in df.to_dict(orient="records"):
        comp_score = compute_weighted_composite_score(item['rs_percentile'], item['eps_rating'], item['ad_rating'])
        item["composite_rating"] = comp_score
        final_results.append(item)
        
    final_results.sort(key=lambda x: x["composite_rating"], reverse=True)
    
    # ✅ SERIAL NUMBER GENERATOR: Injects sequential ranks explicitly into dictionaries
    for idx, item in enumerate(final_results):
        item["serial_no"] = idx + 1
        
    return final_results

# --- ROUTE CONTROLLERS ---

@ibd_engine_ind_bp.route("/ibd-smartselect-india-scan", methods=["GET", "POST"])
def ibd_india_scan_process():
    stocks = []
    last_time = None
    
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cache = json.load(f)
            stocks = cache.get('stocks', [])
            last_time = cache.get('time')

    if request.method == "POST":
        filepath = os.path.abspath(os.path.join(os.getcwd(), 'data', 'nifty_500.csv'))
        if os.path.exists(filepath):
            df_input = pd.read_csv(filepath)
            col_name = 'Symbol' if 'Symbol' in df_input.columns else 'symbol'
            symbols = [str(s).strip().upper() + ".NS" for s in df_input[col_name].dropna().unique()]
            
            # Analyze a 100-stock sample pool for optimized performance runtime execution
            test_pool = symbols[:100]
            stocks = run_ibd_india_screening_pipeline(test_pool)
            last_time = datetime.now().strftime("%d %b %Y %I:%M %p")
            
            with open(RESULTS_JSON, 'w') as f:
                json.dump({'stocks': stocks, 'time': last_time}, f)
            
    return render_template("ibd_smartselect_ind.html", stocks=stocks, last_time=last_time)

@ibd_engine_ind_bp.route("/export-ibd-india")
def export_ibd_india():
    """✅ EXPORT CHANNEL FEATURE: Packages data into single structured downloadable documents"""
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            stocks = json.load(f).get('stocks', [])
        if stocks:
            df = pd.DataFrame(stocks)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_path = os.path.join(UPLOAD_FOLDER, 'temp_india_ibd_export.csv')
            df.to_csv(temp_path, index=False)
            return send_file(temp_path, as_attachment=True, download_name=f"India_IBD_SmartSelect_{timestamp}.csv")
    return "No database assets found to compile.", 404