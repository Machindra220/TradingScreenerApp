import os
import json
import uuid
import threading
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, send_file, jsonify, redirect, url_for

from app.services.market_data_cache import ind_cache, latest_bar_date

ipo_screener_bp = Blueprint("ipo_screener", __name__)

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER = os.path.join(_PROJECT_ROOT, 'uploads', 'ipo_screener')
SNAPSHOT_DIR = os.path.join(UPLOAD_FOLDER, 'snapshots')
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_ipo_results.json')
HISTORY_JSON = os.path.join(UPLOAD_FOLDER, 'scan_history_ipo.json')
DEFAULT_CSV = os.path.join(_PROJECT_ROOT, 'data', 'nifty_500.csv')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SNAPSHOT_DIR, exist_ok=True)

PRIMARY_BENCHMARK = ("^CRSLDX", "Nifty 500")
FALLBACK_BENCHMARK = ("^NSEI", "Nifty 50")

# Background Progress Tracker
_progress_lock = threading.Lock()
_SCAN_PROGRESS = {"active": False, "processed": 0, "total": 0, "current_symbol": "", "stage": "idle", "error": None}

def _set_progress(**kwargs):
    with _progress_lock:
        _SCAN_PROGRESS.update(kwargs)

def _get_progress():
    with _progress_lock:
        return dict(_SCAN_PROGRESS)

# ---------------------------------------------------------------------------
# Quantitative Core & Indicator Logic
# ---------------------------------------------------------------------------

def calculate_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs.fillna(0)))

def detect_pivots_and_structure(df):
    """Detects 5-bar pivot highs/lows and determines HH/HL structure."""
    if len(df) < 15:
        return {"hh": False, "hl": False, "consecutive_hh_hl": 0, "last_sh": 0, "last_sl": 0, "structure": "Neutral"}

    highs = df['High']
    lows = df['Low']
    
    # 5-Bar Pivots
    is_pivot_low = (lows < lows.shift(1)) & (lows < lows.shift(2)) & (lows < lows.shift(-1)) & (lows < lows.shift(-2))
    is_pivot_high = (highs > highs.shift(1)) & (highs > highs.shift(2)) & (highs > highs.shift(-1)) & (highs > highs.shift(-2))

    pivot_lows = df[is_pivot_low]['Low'].dropna()
    pivot_highs = df[is_pivot_high]['High'].dropna()

    if len(pivot_lows) < 2 or len(pivot_highs) < 2:
        return {"hh": False, "hl": False, "consecutive_hh_hl": 0, "last_sh": float(highs.iloc[-1]), "last_sl": float(lows.iloc[-1]), "structure": "Developing"}

    sl1, sl2 = pivot_lows.iloc[-1], pivot_lows.iloc[-2]
    sh1, sh2 = pivot_highs.iloc[-1], pivot_highs.iloc[-2]

    hl = sl1 > sl2
    hh = sh1 > sh2

    consecutive = 0
    if hh and hl:
        consecutive = 2 if (len(pivot_lows) >= 3 and pivot_lows.iloc[-2] > pivot_lows.iloc[-3]) else 1

    structure = "Strong Bullish (HH+HL)" if (hh and hl) else ("Higher Low Detected" if hl else ("Higher High Detected" if hh else "Consolidating"))

    return {
        "hh": bool(hh),
        "hl": bool(hl),
        "consecutive_hh_hl": consecutive,
        "last_sh": round(float(sh1), 2),
        "last_sl": round(float(sl1), 2),
        "structure": structure
    }

def calculate_anchored_vwap(df):
    """Calculates VWAP anchored from the first available trading bar (Listing Date)."""
    if 'Volume' not in df.columns or df['Volume'].sum() == 0:
        return float(df['Close'].iloc[-1])
    typical_price = (df['High'] + df['Low'] + df['Close']) / 3.0
    vp = typical_price * df['Volume']
    cum_vp = vp.cumsum()
    cum_vol = df['Volume'].cumsum()
    avwap = cum_vp / cum_vol.replace(0, np.nan)
    return round(float(avwap.iloc[-1]), 2)

def evaluate_ipo_candidate(df, bench_close, symbol, sector="N/A", listing_date_str=None):
    """Core quantitative evaluation engine for an individual IPO stock."""
    bars = len(df)
    if bars < 40:  # Minimum 40 trading days required (~2 months)
        return None

    close = df['Close']
    curr_price = float(close.iloc[-1])
    curr_vol = int(df['Volume'].iloc[-1])
    
    # Calculate Listing Age in Days
    if listing_date_str:
        try:
            l_date = datetime.strptime(listing_date_str, "%Y-%m-%d")
            age_days = (datetime.today() - l_date).days
        except Exception:
            age_days = int(bars * 1.45)
    else:
        age_days = int(bars * 1.45)

    # 1. Moving Averages
    ema10 = calculate_ema(close, 10)
    ema20 = calculate_ema(close, 20)
    ema50 = calculate_ema(close, 50)
    
    c_ema10 = float(ema10.iloc[-1])
    c_ema20 = float(ema20.iloc[-1])
    c_ema50 = float(ema50.iloc[-1]) if bars >= 50 else c_ema20
    
    ema10_slope = (c_ema10 - float(ema10.iloc[-3])) / float(ema10.iloc[-3]) * 100
    ema20_slope = (c_ema20 - float(ema20.iloc[-3])) / float(ema20.iloc[-3]) * 100

    # 2. Volume Metrics & Liquidity
    vol20 = df['Volume'].rolling(20).mean()
    vol50 = df['Volume'].rolling(50).mean() if bars >= 50 else vol20
    c_vol20 = float(vol20.iloc[-1]) if not np.isnan(vol20.iloc[-1]) else 1.0
    rvol = round(curr_vol / c_vol20, 2) if c_vol20 > 0 else 1.0
    daily_turnover_cr = round((curr_price * curr_vol) / 10000000.0, 2)

    # 3. Relative Strength vs Benchmark
    bench_aligned = bench_close.reindex(close.index).ffill()
    stock_ret_3m = (curr_price / float(close.iloc[-min(bars, 55)])) - 1
    bench_ret_3m = (float(bench_aligned.iloc[-1]) / float(bench_aligned.iloc[-min(bars, 55)])) - 1
    rs_raw_3m = ((1 + stock_ret_3m) / (1 + bench_ret_3m)) - 1 if (1 + bench_ret_3m) != 0 else 0

    stock_ret_6m = (curr_price / float(close.iloc[-min(bars, 122)])) - 1
    bench_ret_6m = (float(bench_aligned.iloc[-1]) / float(bench_aligned.iloc[-min(bars, 122)])) - 1
    rs_raw_6m = ((1 + stock_ret_6m) / (1 + bench_ret_6m)) - 1 if (1 + bench_ret_6m) != 0 else 0

    # 4. Price Structure & Pivots
    pivots = detect_pivots_and_structure(df)
    high_50d = float(df['High'].tail(min(bars, 50)).max())
    high_ipo = float(df['High'].max())
    dist_50d_high = round(((high_50d - curr_price) / high_50d) * 100, 1)
    dist_ipo_high = round(((high_ipo - curr_price) / high_ipo) * 100, 1)
    
    # 5. Gap-Up Detection
    prev_close = float(close.iloc[-2])
    today_open = float(df['Open'].iloc[-1])
    gap_pct = round(((today_open - prev_close) / prev_close) * 100, 2)
    has_gap = gap_pct >= 2.0
    gap_holding = has_gap and (curr_price >= prev_close)

    # 6. Consolidation Base (Tight Range Check)
    recent_range_pct = ((df['High'].tail(15).max() - df['Low'].tail(15).min()) / df['Low'].tail(15).min()) * 100
    is_tight_base = recent_range_pct <= 12.0

    # 7. Anchored VWAP
    avwap = calculate_anchored_vwap(df)
    rsi14 = round(float(calculate_rsi(close).iloc[-1]), 1)

    return {
        "symbol": symbol,
        "sector": sector,
        "listing_age_days": age_days,
        "price": round(curr_price, 2),
        "volume": curr_vol,
        "vol20_avg": int(c_vol20),
        "rvol": rvol,
        "turnover_cr": daily_turnover_cr,
        "ema10": round(c_ema10, 2),
        "ema20": round(c_ema20, 2),
        "ema50": round(c_ema50, 2),
        "ema10_slope": round(ema10_slope, 2),
        "ema20_slope": round(ema20_slope, 2),
        "rs_raw_3m": rs_raw_3m,
        "rs_raw_6m": rs_raw_6m,
        "rsi14": rsi14,
        "hh": pivots["hh"],
        "hl": pivots["hl"],
        "last_sh": pivots["last_sh"],
        "last_sl": pivots["last_sl"],
        "structure": pivots["structure"],
        "dist_50d_high": dist_50d_high,
        "dist_ipo_high": dist_ipo_high,
        "breakout_50d": curr_price >= high_50d,
        "gap_pct": gap_pct,
        "has_gap": has_gap,
        "gap_holding": gap_holding,
        "is_tight_base": is_tight_base,
        "avwap": avwap,
        "above_avwap": curr_price > avwap,
        "extended": curr_price > (1.20 * c_ema20)
    }

def calculate_composite_score(stock):
    """Calculates the 0-100 Composite IPO Strength Score and compiles signal reasons."""
    score = 0
    reasons = []

    # Trend & MA Alignment (20 Pts)
    if stock['price'] > stock['ema20']:
        score += 7
        reasons.append("Price > 20 EMA")
    if stock['price'] > stock['ema50']:
        score += 7
        reasons.append("Price > 50 EMA")
    if stock['ema20_slope'] > 0:
        score += 6
        reasons.append("20 EMA Slope Positive")

    # Relative Strength (25 Pts)
    rs3_pct = stock.get('rs3_pct', 50)
    rs6_pct = stock.get('rs6_pct', 50)
    if rs3_pct >= 80:
        score += 15
        reasons.append(f"Strong 3M Relative Strength ({rs3_pct}th %tile)")
    elif rs3_pct >= 60:
        score += 8
        reasons.append(f"Improving 3M Relative Strength ({rs3_pct}th %tile)")
    
    if rs6_pct >= 70:
        score += 10
        reasons.append(f"Sustained 6M Relative Strength ({rs6_pct}th %tile)")

    # Volume & Institutional Demand (20 Pts)
    if stock['rvol'] >= 2.5:
        score += 12
        reasons.append(f"Institutional Volume Surge ({stock['rvol']}x RVOL)")
    elif stock['rvol'] >= 1.5:
        score += 8
        reasons.append(f"Volume Expansion ({stock['rvol']}x RVOL)")
    if stock['turnover_cr'] >= 1.0:
        score += 8
        reasons.append("High Liquidity (> ₹1Cr Turnover)")

    # Price Structure & Breakouts (25 Pts)
    if stock['hh'] and stock['hl']:
        score += 15
        reasons.append("Higher High + Higher Low Confirmed")
    elif stock['hl']:
        score += 8
        reasons.append("Higher Low Support Formed")

    if stock['breakout_50d']:
        score += 10
        reasons.append("50-Day High Breakout")
    elif stock['dist_50d_high'] <= 5.0:
        score += 5
        reasons.append("Near Breakout Zone (<=5% from High)")

    # Momentum & Quality (10 Pts)
    if 50 <= stock['rsi14'] <= 70:
        score += 5
        reasons.append(f"RSI Bullish Zone ({stock['rsi14']})")
    if stock['above_avwap']:
        score += 5
        reasons.append("Price > Listing Date Anchored VWAP")

    # Final Classification
    if score >= 80:
        signal = "Strong Bullish"
    elif score >= 70:
        signal = "Bullish"
    elif score >= 60:
        signal = "Developing"
    elif score >= 50:
        signal = "Watch"
    else:
        signal = "Weak"

    stock['score'] = score
    stock['signal'] = signal
    stock['reasons'] = reasons
    return stock

# ---------------------------------------------------------------------------
# Background Scan Task
# ---------------------------------------------------------------------------

def run_ipo_scan(symbols_data, source_name):
    _set_progress(active=True, processed=0, total=len(symbols_data), current_symbol="", stage="loading", error=None)
    
    yf_symbols = [s['Symbol'] if s['Symbol'].endswith('.NS') else f"{s['Symbol']}.NS" for s in symbols_data]
    sym_map = {yf_sym: item for yf_sym, item in zip(yf_symbols, symbols_data)}

    # Fetch Benchmark Data
    _set_progress(stage="benchmark")
    bench_df = None
    bench_label = "Nifty 500"
    for b_ticker, b_label in (PRIMARY_BENCHMARK, FALLBACK_BENCHMARK):
        res, _ = ind_cache.get_price_history_bulk([b_ticker], interval='1d', lookback_days=400)
        df_b = res.get(b_ticker)
        if df_b is not None and not df_b.empty and len(df_b) >= 100:
            bench_df = df_b
            bench_label = f"{b_label} ({b_ticker})"
            break

    if bench_df is None:
        _set_progress(active=False, stage="error", error="Failed to fetch benchmark price history.")
        return

    # Fetch IPO Universe Bulk Prices
    def _progress_cb(i, total, sym):
        _set_progress(stage="fetching", processed=i, total=total, current_symbol=sym)

    price_data, report = ind_cache.get_price_history_bulk(yf_symbols, interval='1d', lookback_days=400, progress_callback=_progress_cb)
    as_of_date = latest_bar_date(price_data)

    _set_progress(stage="evaluating", processed=0, total=len(yf_symbols))

    candidates = []
    for i, yf_sym in enumerate(yf_symbols):
        _set_progress(processed=i, current_symbol=yf_sym)
        item = sym_map[yf_sym]
        df_stock = price_data.get(yf_sym)
        if df_stock is None or df_stock.empty:
            continue

        res = evaluate_ipo_candidate(df_stock, bench_df['Close'].dropna(), item['Symbol'], sector=item.get('Sector', 'N/A'), listing_date_str=item.get('ListingDate'))
        if res:
            candidates.append(res)

    if not candidates:
        _set_progress(active=False, stage="done", current_symbol="")
        return

    # Relative Strength Percentile Ranking
    cand_df = pd.DataFrame(candidates)
    cand_df['rs3_pct'] = cand_df['rs_raw_3m'].rank(pct=True).mul(100).round(0).astype(int)
    cand_df['rs6_pct'] = cand_df['rs_raw_6m'].rank(pct=True).mul(100).round(0).astype(int)
    
    evaluated_stocks = cand_df.to_dict(orient='records')
    scored_stocks = [calculate_composite_score(s) for s in evaluated_stocks]
    
    # Sort by Composite Score Descending
    scored_stocks.sort(key=lambda x: x['score'], reverse=True)
    for idx, s in enumerate(scored_stocks):
        s['rank'] = idx + 1

    last_time = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    
    payload = {
        "stocks": scored_stocks,
        "time": last_time,
        "source": source_name,
        "benchmark_label": bench_label,
        "scanned_count": len(symbols_data),
        "passed_count": len(scored_stocks),
        "price_data_asof": as_of_date
    }

    # Snapshot Management
    snap_file = f"snapshot_{uuid.uuid4().hex[:8]}.json"
    with open(os.path.join(SNAPSHOT_DIR, snap_file), 'w') as f:
        json.dump(payload, f)

    with open(RESULTS_JSON, 'w') as f:
        json.dump(payload, f)

    # History Update
    history = []
    if os.path.exists(HISTORY_JSON):
        try:
            with open(HISTORY_JSON) as f: history = json.load(f)
        except Exception: pass

    history.insert(0, {
        "time": last_time,
        "source": source_name,
        "count": len(scored_stocks),
        "strong_count": len([s for s in scored_stocks if s['score'] >= 70]),
        "snapshot_file": snap_file
    })
    
    with open(HISTORY_JSON, 'w') as f:
        json.dump(history[:5], f)

    _set_progress(active=False, stage="done", current_symbol="")

# ---------------------------------------------------------------------------
# Flask Blueprint Routes
# ---------------------------------------------------------------------------

@ipo_screener_bp.route("/ipo-screener", methods=["GET", "POST"])
def ipo_screener_dashboard():
    if request.method == "POST":
        if _get_progress()["active"]:
            return redirect(url_for('ipo_screener.ipo_screener_dashboard', scanning=1))

        file = request.files.get('file')
        symbols_data = []
        source_name = "Nifty 500 Default"

        if file and file.filename != '':
            ext = os.path.splitext(file.filename)[1].lower()
            if ext in ['.csv', '.xlsx', '.xls']:
                df = pd.read_excel(file) if ext in ['.xlsx', '.xls'] else pd.read_csv(file)
                col_map = {c.lower(): c for c in df.columns}
                sym_col = next((col_map[k] for k in ('symbol', 'ticker') if k in col_map), None)
                sec_col = next((col_map[k] for k in ('sector', 'industry') if k in col_map), None)
                date_col = next((col_map[k] for k in ('listingdate', 'listing_date', 'date') if k in col_map), None)

                if sym_col:
                    for _, row in df.iterrows():
                        sym = str(row[sym_col]).strip().upper()
                        if sym:
                            symbols_data.append({
                                "Symbol": sym,
                                "Sector": str(row[sec_col]).strip() if sec_col and pd.notna(row[sec_col]) else "IPO / New Listing",
                                "ListingDate": str(row[date_col]).strip() if date_col and pd.notna(row[date_col]) else None
                            })
                    source_name = f"Uploaded ({file.filename})"

        if not symbols_data and os.path.exists(DEFAULT_CSV):
            df_def = pd.read_csv(DEFAULT_CSV)
            sym_col = 'symbol' if 'symbol' in df_def.columns else df_def.columns[0]
            for _, row in df_def.iterrows():
                symbols_data.append({"Symbol": str(row[sym_col]).strip().upper(), "Sector": "IPO Candidate"})

        if symbols_data:
            thread = threading.Thread(target=run_ipo_scan, args=(symbols_data, source_name), daemon=True)
            thread.start()
            return redirect(url_for('ipo_screener.ipo_screener_dashboard', scanning=1))

    # GET Request Processing
    cache_payload = {}
    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON) as f: cache_payload = json.load(f)
        except Exception: pass

    history = []
    if os.path.exists(HISTORY_JSON):
        try:
            with open(HISTORY_JSON) as f: history = json.load(f)
        except Exception: pass

    progress = _get_progress()

    return render_template(
        "ipo_screener.html",
        stocks=cache_payload.get('stocks', []),
        last_time=cache_payload.get('time'),
        source_name=cache_payload.get('source'),
        benchmark_label=cache_payload.get('benchmark_label'),
        scanned_count=cache_payload.get('scanned_count', 0),
        passed_count=cache_payload.get('passed_count', 0),
        price_data_asof=cache_payload.get('price_data_asof'),
        history=history,
        is_scanning=progress["active"] or request.args.get('scanning') == '1',
        scan_error=progress.get("error")
    )

@ipo_screener_bp.route("/ipo-screener/progress")
def ipo_progress_api():
    return jsonify(_get_progress())

@ipo_screener_bp.route("/ipo-screener/export")
def export_ipo_csv():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON) as f: data = json.load(f)
        stocks = data.get('stocks', [])
        if stocks:
            df = pd.DataFrame(stocks)
            df_export = df.drop(columns=['reasons'], errors='ignore')
            export_path = os.path.join(UPLOAD_FOLDER, 'ipo_screener_export.csv')
            df_export.to_csv(export_path, index=False)
            return send_file(export_path, as_attachment=True, download_name=f"IPO_Swing_Screener_{datetime.now().strftime('%Y%m%d')}.csv")
    return "No scan data available", 404