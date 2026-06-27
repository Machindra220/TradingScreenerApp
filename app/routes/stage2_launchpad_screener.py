import os
import json
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Blueprint, render_template, request, send_file

stage2_launchpad_bp = Blueprint("stage2_launchpad", __name__)

# --- PATH COMPARTMENTALIZATION ---
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'stage2_launchpad'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_launchpad_results.json')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def calculate_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def verify_chart_geometry(df, sym):
    """
    Decodes technical chart patterns with high-conviction strict structural filters:
    1. Perfect Current Sequential Moving Average Fan-out (Enforcing 10, 20, 50, 100, 200 EMAs).
    2. Tight Volatility Squeeze Base Check (Hard ceiling capped at 10%).
    3. Strict Proximity Filter: 10 EMA must be within 5% of 50 EMA (Pivot buy zone).
    4. Institutional Volume Acceleration Signature (Minimum 1.3x breakout multiplier).
    """
    if len(df) < 200:
        return None
        
    close = pd.to_numeric(df['Close'], errors='coerce').dropna()
    volume = pd.to_numeric(df['Volume'], errors='coerce').fillna(1.0)
    
    if len(close) < 200:
        return None
        
    # Calculate full strict EMA spectrum matching user chart indicators
    ema10 = calculate_ema(close, 10)
    ema20 = calculate_ema(close, 20)
    ema50 = calculate_ema(close, 50)
    ema100 = calculate_ema(close, 100)
    ema200 = calculate_ema(close, 200)
    
    current_price = close.iloc[-1]
    
    # 1. 🔥 STRICT TREND TEMPLATE STACK (Reintroduced 20 EMA)
    is_fanned_out = (
        current_price > ema10.iloc[-1] and
        ema10.iloc[-1] > ema20.iloc[-1] and
        ema20.iloc[-1] > ema50.iloc[-1] and
        ema50.iloc[-1] > ema100.iloc[-1] and
        ema100.iloc[-1] > ema200.iloc[-1]
    )
    
    if not is_fanned_out:
        return None

    # 2. 🛡️ TIGHT VOLATILITY SQUEEZE BASE CHECK
    # Compressed down from 20% to 10% to ensure a flat, solid accumulation base
    if len(ema50) >= 40:
        historical_ema50 = ema50.iloc[-40:-20]
        historical_ema200 = ema200.iloc[-40:-20]
        ema_spreads = (historical_ema50 - historical_ema200).abs() / historical_ema200
        is_base_compressed = ema_spreads.mean() <= 0.10  
        if not is_base_compressed:
            return None

    # 3. 🎯 TIGHT PROXIMITY GUARDRAIL
    # Enforces that the stock is sitting right at the launchpad pivot point.
    # The 10 EMA must be within 5% of the 50 EMA.
    current_ema_spread = (ema10.iloc[-1] - ema50.iloc[-1]) / ema50.iloc[-1]
    if current_ema_spread > 0.05:  
        return None

    # 4. 🔥 INSTITUTIONAL VOLUME ACCELERATION SIGNATURE
    # Raised from 0.5x up to 1.3x to confirm large-scale institutional accumulation
    current_vol = volume.iloc[-1]
    avg_20d_vol = volume.iloc[-21:-1].mean() if len(volume) >= 21 else 1.0
    volume_ratio = current_vol / avg_20d_vol if avg_20d_vol > 0 else 1.0
    
    if volume_ratio < 1.3:  
        return None

    high_52w = close.max()
    pullback = (high_52w - current_price) / high_52w
    
    # Tightened maximum pullback ceiling down to 25% to filter out broken setups
    if pullback > 0.25:
        return None

    return {
        "price": round(current_price, 2),
        "volume_ratio": round(volume_ratio, 2),
        "pullback_pct": round(pullback * 100, 2),
        "ema200": round(ema200.iloc[-1], 2)
    }

def run_launchpad_pipeline(symbols, market_type="US"):
    results = []
    print(f"📥 Running strict structural trend geometry scan over {len(symbols)} tickers...")
    
    benchmark = "^GSPC" if market_type == "US" else "^CRSLDX"
    
    data = yf.download(symbols + [benchmark], period="1y", interval="1d", auto_adjust=True)
    if data.empty:
        return []

    data.index = data.index.tz_localize(None)

    for sym in symbols:
        try:
            if isinstance(data.columns, pd.MultiIndex):
                if sym not in data.columns.get_level_values(1): 
                    continue
                stock_df = data.xs(sym, level=1, axis=1).dropna(subset=['Close'])
                if isinstance(stock_df.columns, pd.MultiIndex):
                    stock_df.columns = stock_df.columns.get_level_values(0)
            else:
                if sym in data.columns:
                    stock_df = data[[sym]].dropna()
                    stock_df.columns = ['Close']
                else:
                    continue

            if stock_df.empty or len(stock_df) < 100:
                continue

            metrics = verify_chart_geometry(stock_df, sym)
            if metrics:
                metrics["symbol"] = sym.replace(".NS", "")
                results.append(metrics)
                print(f"🔥 STRICT PASS: {sym} matches pattern.")
        except Exception as e:
            print(f"Error checking symbol {sym}: {e}")
            continue

    if not results:
        return []

    df_res = pd.DataFrame(results)
    df_res.sort_values(by=["volume_ratio", "pullback_pct"], ascending=[False, True], inplace=True)
    df_res.reset_index(drop=True, inplace=True)
    for idx, row in df_res.iterrows():
        df_res.at[idx, "rank"] = idx + 1
        
    return df_res.to_dict(orient="records")

# --- FLASK VIEW INTERFACE CONTROLLERS ---

@stage2_launchpad_bp.route("/stage2-launchpad-scan", methods=["GET", "POST"])
def launchpad_scan_process():
    stocks = []
    last_time = None
    market = request.args.get('market', 'US').upper()
    
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cache = json.load(f)
            if cache.get('market') == market:
                stocks = cache.get('stocks', [])
                last_time = cache.get('time')

    if request.method == "POST":
        if market == "INDIA":
            url = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
            df_input = pd.read_csv(url)
            col_name = 'Symbol' if 'Symbol' in df_input.columns else 'symbol'
            symbols = [str(s).strip().upper() + ".NS" for s in df_input[col_name].dropna().unique()]
        else:
            filepath = os.path.abspath(os.path.join(os.getcwd(), 'data', 'snp500.csv'))
            if not os.path.exists(filepath):
                url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
                tables = pd.read_html(url)
                symbols = tables[0]['Symbol'].str.replace('.', '-', regex=False).tolist()
            else:
                df_input = pd.read_csv(filepath)
                col_name = 'Symbol' if 'Symbol' in df_input.columns else 'symbol'
                symbols = [str(s).strip().upper().replace('.', '-') for s in df_input[col_name].dropna().unique()]

        # Scan the first 150 tickers concurrently for optimized performance
        stocks = run_launchpad_pipeline(symbols[:150], market_type=market)
        last_time = datetime.now().strftime("%d %b %Y %I:%M %p")
        
        with open(RESULTS_JSON, 'w') as f:
            json.dump({'stocks': stocks, 'time': last_time, 'market': market}, f)

    return render_template("stage2_launchpad.html", stocks=stocks, last_time=last_time, market=market)

@stage2_launchpad_bp.route("/export-launchpad")
def export_launchpad_csv():
    market = request.args.get('market', 'US').upper()
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            data = json.load(f)
        if data.get('market') == market:
            stocks = data.get('stocks', [])
            if stocks:
                df = pd.DataFrame(stocks)
                export_path = os.path.join(UPLOAD_FOLDER, 'temp_launchpad_export.csv')
                df.to_csv(export_path, index=False)
                return send_file(export_path, as_attachment=True, download_name=f"{market}_Stage2_Launchpad_Strict_Trends.csv")
    return "No active data layer cached to export", 404