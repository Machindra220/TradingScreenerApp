import os
import json
import numpy as np
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, request

ibd_engine_bp = Blueprint("ibd_engine", __name__)

UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'volar_us'))
FUNDAMENTALS_JSON = os.path.join(UPLOAD_FOLDER, 'mock_fundamentals.json')

def calculate_ad_rating(df, lookback=20):
    """
    Approximates an IBD Accumulation/Distribution Score from A to E.
    Measures buying pressure intensity on high-volume days.
    """
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
    """Combines individual pillars into an authentic 1-99 scoring scale."""
    # Map A-E grades onto a numerical multiplier spectrum
    ad_map = {"A": 95, "B": 80, "C": 60, "D": 40, "E": 20}
    ad_score = ad_map.get(ad_grade, 60)
    
    # Apply weighted calculation metrics
    composite = (rs_pct * 0.50) + (eps_score * 0.35) + (ad_score * 0.15)
    return int(round(composite, 0))

def run_ibd_screening_pipeline(symbols):
    """Processes tickers through the combined quantitative rating grid matrix."""
    try:
        index_df = yf.download("^GSPC", period="1y", interval="1d", auto_adjust=True)['Close']
    except Exception:
        return []

    # Initialize or load an EPS scoring database tracking array
    fundamentals = {}
    if os.path.exists(FUNDAMENTALS_JSON):
        with open(FUNDAMENTALS_JSON, 'r') as f:
            fundamentals = json.load(f)

    raw_candidates = []
    # Batch-download prices concurrently for speed
    data = yf.download(symbols, period="1y", interval="1d", auto_adjust=True)
    
    for sym in symbols:
        try:
            # Handle multi-index columns safely from yfinance batch downloads
            if isinstance(data.columns, pd.MultiIndex):
                stock_close = data['Close'][sym].dropna()
                stock_open = data['Open'][sym].dropna()
                stock_vol = data['Volume'][sym].dropna()
            else:
                stock_close = data['Close'].dropna()
                stock_open = data['Open'].dropna()
                stock_vol = data['Volume'].dropna()
                
            if len(stock_close) < 200: continue
            
            # 1. Analyze Technical RS Line Slope Intensity (Trailing 20 Days)
            rs_ratio = stock_close.tail(20).values / index_df.tail(20).values
            slope, _ = np.polyfit(np.arange(20), rs_ratio, 1)
            is_rs_line_up = slope > 0
            
            # Calculate standard 1Y performance return
            perf_1y = (stock_close.iloc[-1] / stock_close.iloc[0]) - 1
            
            # 2. Extract Fundamental EPS Pillar Score (Default fallback to 70 if unlisted)
            eps_score = fundamentals.get(sym, {}).get("eps_rating", 70)
            
            # 3. Compute Buying Volume Pressure Footprint
            stock_df = pd.DataFrame({"Open": stock_open, "Close": stock_close, "Volume": stock_vol})
            ad_grade = calculate_ad_rating(stock_df)
            
            raw_candidates.append({
                "symbol": sym,
                "price": round(stock_close.iloc[-1], 2),
                "perf_1y": perf_1y,
                "eps_rating": eps_score,
                "ad_rating": ad_grade,
                "rs_line_up": is_rs_line_up
            })
        except Exception:
            continue

    if not raw_candidates: return []
    
    df = pd.DataFrame(raw_candidates)
    # Generate Cross-Sectional RS Percentile compared to all other processed equities
    df['rs_percentile'] = df['perf_1y'].rank(pct=True).mul(100).round(0).astype(int)
    
    # 4. Generate Final Composite Rank Scores
    final_results = []
    for item in df.to_dict(orient="records"):
        comp_score = compute_weighted_composite_score(item['rs_percentile'], item['eps_rating'], item['ad_rating'])
        item["composite_rating"] = comp_score
        final_results.append(item)
        
    # Sort dashboard outputs prioritizing highest overall Quality Leaders
    final_results.sort_values(by="composite_rating", ascending=False, inplace=True)
    return final_results

# --- FLASK VIEW INTERFACE CONTROLLER ---

@ibd_engine_bp.route("/ibd-smartselect-scan", methods=["GET", "POST"])
def ibd_scan_process():
    stocks = []
    if request.method == "POST":
        # Pull symbols from your default local S&P 500 reference file
        filepath = os.path.abspath(os.path.join(os.getcwd(), 'data', 'sp500.csv'))
        if os.path.exists(filepath):
            df_input = pd.read_csv(filepath)
            col_name = 'Symbol' if 'Symbol' in df_input.columns else 'symbol'
            symbols = [str(s).strip().upper() for s in df_input[col_name].dropna().unique()][:50] # Limit to 50 for fast initial runtime testing
            stocks = run_ibd_screening_pipeline(symbols)
            
    return render_template("ibd_smartselect.html", stocks=stocks)