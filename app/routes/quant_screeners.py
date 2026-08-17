import os
import json
import uuid
import threading
import numpy as np
import pandas as pd
from datetime import datetime
from sklearn.decomposition import PCA
from hmmlearn.hmm import GaussianHMM
from flask import Blueprint, render_template, request, send_file, redirect, url_for, jsonify

from app.services.market_data_cache import ind_cache, latest_bar_date

quant_screeners_bp = Blueprint("quant_screeners", __name__)

# Directory and JSON file path setup
_PROJECT_ROOT     = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER     = os.path.join(_PROJECT_ROOT, 'uploads', 'quant_screeners')
SNAPSHOT_DIR      = os.path.join(UPLOAD_FOLDER, 'snapshots')
RESULTS_JSON      = os.path.join(UPLOAD_FOLDER, 'last_quant_screeners_results.json')
HISTORY_JSON      = os.path.join(UPLOAD_FOLDER, 'scan_history_quant_screeners.json')
LAST_CSV_CONFIG   = os.path.join(UPLOAD_FOLDER, 'last_csv_quant_config.json')
DEFAULT_IND_CSV   = os.path.join(_PROJECT_ROOT, 'data', 'nifty_500.csv')
DEFAULT_IND_LABEL = "Nifty 500 Default (nifty_500.csv)"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SNAPSHOT_DIR,  exist_ok=True)

HISTORY_LIMIT = 5
PRIMARY_BENCHMARK  = ("^CRSLDX", "Nifty 500")
FALLBACK_BENCHMARK = ("^NSEI",   "Nifty 50")

_progress_lock = threading.Lock()
_SCAN_PROGRESS = {
    "active": False, "processed": 0, "total": 0,
    "current_symbol": "", "stage": "idle", "error": None,
}

def _set_progress(**kwargs):
    with _progress_lock:
        _SCAN_PROGRESS.update(kwargs)

def _get_progress():
    with _progress_lock:
        return dict(_SCAN_PROGRESS)

def _get_active_source():
    if os.path.exists(LAST_CSV_CONFIG):
        try:
            with open(LAST_CSV_CONFIG) as f:
                cfg = json.load(f)
            fp   = cfg.get('path', '')
            name = cfg.get('name', os.path.basename(fp))
            if fp and os.path.exists(fp):
                return fp, name, False
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_IND_CSV, DEFAULT_IND_LABEL, True

def _read_ticker_file(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    try:
        df = pd.read_excel(filepath) if ext in ('.xlsx', '.xls') else pd.read_csv(filepath)
    except Exception as e:
        raise ValueError(f"Could not read file: {e}")
    col_map = {c.lower(): c for c in df.columns}
    found = next((col_map[k] for k in ('symbol', 'ticker', 'symbols', 'tickers') if k in col_map), None)
    if found is None:
        raise ValueError(f"File must have a Symbol/Ticker column. Found: {', '.join(df.columns.tolist())}")
    sec_col = next((col_map[k] for k in ('industry', 'sector', 'gics sector') if k in col_map), None)
    results = []
    for _, row in df.iterrows():
        sym = str(row[found]).strip().upper()
        sec = str(row[sec_col]).strip() if sec_col else 'Unknown'
        if sym:
            results.append({'Symbol': sym, 'Sector': sec})
    return results

def load_symbols():
    source_path, _, _ = _get_active_source()
    if os.path.exists(source_path):
        try:
            return _read_ticker_file(source_path)
        except Exception:
            pass
    try:
        url = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
        df = pd.read_csv(url)
        col_map = {c.lower(): c for c in df.columns}
        sym_col = col_map.get('symbol')
        sec_col = col_map.get('industry', col_map.get('sector'))
        return [{'Symbol': str(row[sym_col]).strip().upper(),
                 'Sector': str(row[sec_col]).strip() if sec_col else 'Unknown'}
                for _, row in df.iterrows() if str(row[sym_col]).strip()]
    except Exception:
        return []

def _load_history():
    if os.path.exists(HISTORY_JSON):
        try:
            with open(HISTORY_JSON) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []

def _prune_snapshots(keep_filenames):
    keep = set(keep_filenames)
    for fname in os.listdir(SNAPSHOT_DIR):
        if fname not in keep:
            try: os.remove(os.path.join(SNAPSHOT_DIR, fname))
            except OSError: pass

# ---------------------------------------------------------------------------
# Quantitative Algorithms
# ---------------------------------------------------------------------------

def calculate_atr(df, period=14):
    high_low = df['High'] - df['Low']
    high_close = (df['High'] - df['Close'].shift()).abs()
    low_close = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / (loss + 1e-9)
    return 100 - (100 / (1 + rs))

def fit_ou_process(residuals: pd.Series):
    """
    Fits continuous Ornstein-Uhlenbeck: dX_t = theta * (mu - X_t) dt + sigma dW_t
    via AR(1) discrete regression: X_t = a * X_{t-1} + b + eps
    Returns (s_score, half_life, equilibrium)
    """
    if len(residuals) < 30:
        return 0.0, 0.0, 0.0
    x = residuals.shift(1).dropna().values
    y = residuals.iloc[1:].values
    A = np.vstack([x, np.ones(len(x))]).T
    a, b = np.linalg.lstsq(A, y, rcond=None)[0]
    
    if a >= 1.0 or a <= 0.0:
        return 0.0, 0.0, 0.0  # Non-stationary / non mean-reverting
        
    dt = 1.0
    theta = -np.log(a) / dt
    mu = b / (1.0 - a)
    half_life = np.log(2.0) / (theta + 1e-9)
    residuals_std = np.std(y - (a * x + b))
    sigma_eq = np.sqrt((residuals_std ** 2) / (2 * theta + 1e-9))
    
    current_val = residuals.iloc[-1]
    s_score = (current_val - mu) / (sigma_eq + 1e-9)
    return round(float(s_score), 2), round(float(half_life), 1), round(float(mu), 2)

def compute_pca_residuals(price_matrix: pd.DataFrame, n_components=3):
    """
    Extracts cross-sectional residuals after removing Top N principal market/sector factors.
    """
    returns = price_matrix.pct_change().dropna()
    clean_returns = returns.dropna(axis=1)
    if clean_returns.shape[1] < n_components or len(clean_returns) < 60:
        return {}
    
    pca = PCA(n_components=n_components)
    factors = pca.fit_transform(clean_returns)
    reconstructed = pca.inverse_transform(factors)
    residual_returns = clean_returns.values - reconstructed
    
    residual_df = pd.DataFrame(residual_returns, index=clean_returns.index, columns=clean_returns.columns)
    # Cumulate into synthetic cointegrated asset price series
    return {col: (1 + residual_df[col]).cumprod() for col in residual_df.columns}

def fit_hmm_regime(returns: pd.Series):
    """
    Fits a 2-State Gaussian Hidden Markov Model:
    State 0: Low Volatility / Consolidation
    State 1: High Volatility / Momentum Expansion
    Returns True if current day transitioned into Regime 1 (Expansion) with high posterior probability.
    """
    if len(returns) < 120:
        return False, 0.0, "Stable"
    try:
        X = returns.dropna().values.reshape(-1, 1)
        model = GaussianHMM(n_components=2, covariance_type="diag", n_iter=100, random_state=42)
        model.fit(X)
        
        hidden_states = model.predict(X)
        probs = model.predict_proba(X)
        
        # Identify higher variance state
        state_vars = [np.var(X[hidden_states == s]) if np.sum(hidden_states == s) > 0 else 0 for s in range(2)]
        expansion_state = int(np.argmax(state_vars))
        
        current_state = hidden_states[-1]
        prev_state = hidden_states[-2]
        current_prob = probs[-1, expansion_state]
        
        is_breakout_transition = (prev_state != expansion_state) and (current_state == expansion_state) and (current_prob > 0.70)
        regime_label = "Expansion Regime" if current_state == expansion_state else "Accumulation / Range"
        return is_breakout_transition, round(float(current_prob), 2), regime_label
    except Exception:
        return False, 0.0, "Unknown"

# ---------------------------------------------------------------------------
# Background Scan Execution
# ---------------------------------------------------------------------------

def run_scan(source_path, source_name):
    _set_progress(active=True, processed=0, total=0, current_symbol="", stage="loading_symbols", error=None)
    
    stock_list = load_symbols()
    if not stock_list:
        _set_progress(active=False, stage="error", error="Could not load symbols.")
        return

    yf_symbols = [s['Symbol'] if s['Symbol'].endswith('.NS') else f"{s['Symbol']}.NS" for s in stock_list]
    sym_to_item = {yf: item for yf, item in zip(yf_symbols, stock_list)}

    # Fetch benchmark
    _set_progress(stage="fetching_benchmark", total=len(yf_symbols))
    bench_data = {}
    for ticker, label in (PRIMARY_BENCHMARK, FALLBACK_BENCHMARK):
        result, _ = ind_cache.get_price_history_bulk([ticker], interval='1d', lookback_days=500)
        df = result.get(ticker)
        if df is not None and not df.empty and len(df) >= 200:
            bench_data = {'close': df['Close'].dropna(), 'label': f"{label} ({ticker})"}
            break
            
    if not bench_data:
        _set_progress(active=False, stage="error", error="Could not fetch benchmark.")
        return

    # Bulk fetch stock data
    price_data, fetch_report = ind_cache.get_price_history_bulk(
        yf_symbols, interval='1d', lookback_days=500,
        progress_callback=lambda i, t, s: _set_progress(stage="fetching_prices", processed=i, total=t, current_symbol=s)
    )
    price_data_asof = latest_bar_date(price_data)

    # Assemble close price matrix for PCA StatArb
    _set_progress(stage="calculating_pca", processed=0, total=len(yf_symbols))
    closes_dict = {sym: df['Close'].dropna() for sym, df in price_data.items() if df is not None and len(df) >= 200}
    price_matrix = pd.DataFrame(closes_dict).dropna(axis=1)
    pca_residuals = compute_pca_residuals(price_matrix, n_components=3)

    _set_progress(stage="screening", processed=0, total=len(yf_symbols))

    statarb_results = []
    hmm_results = []
    vwap_results = []
    
    seen_symbols = set()  # Ensure strict non-overlapping assignment

    for i, yf_sym in enumerate(yf_symbols):
        _set_progress(processed=i, current_symbol=yf_sym)
        item = sym_to_item[yf_sym]
        df = price_data.get(yf_sym)
        if df is None or len(df) < 200:
            continue

        try:
            close = df['Close'].dropna()
            current_price = float(close.iloc[-1])
            high_52w = float(close.max())
            pullback = (high_52w - current_price) / high_52w
            
            # Common baseline indicators
            df['EMA_20'] = close.ewm(span=20, adjust=False).mean()
            df['EMA_50'] = close.ewm(span=50, adjust=False).mean()
            df['EMA_200'] = close.ewm(span=200, adjust=False).mean()
            df['ATR_14'] = calculate_atr(df, 14)
            df['RSI_14'] = calculate_rsi(close, 14)
            df['Vol_SMA_20'] = df['Volume'].rolling(20).mean()
            
            latest = df.iloc[-1]
            atr = float(latest['ATR_14'])
            chandelier_stop = round(float(high_52w - (2.5 * atr)), 2)
            ema20_stop = round(float(latest['EMA_20']), 2)

            # ------------------------------------------------------------------
            # Screener 1: StatArb O-U Mean Reversion / Oversold Residual Swings
            # ------------------------------------------------------------------
            if yf_sym in pca_residuals and yf_sym not in seen_symbols:
                s_score, half_life, _ = fit_ou_process(pca_residuals[yf_sym])
                # Entry trigger: Statistical deviation s-score < -1.75 (underpriced vs factor basket)
                # with half-life between 2 and 25 days, in structural uptrend above EMA200
                if s_score <= -1.75 and 2.0 <= half_life <= 25.0 and current_price > latest['EMA_200']:
                    seen_symbols.add(yf_sym)
                    statarb_results.append({
                        "symbol": item['Symbol'],
                        "sector": item.get('Sector', 'Unknown'),
                        "price": round(current_price, 2),
                        "s_score": s_score,
                        "half_life": half_life,
                        "rsi": round(float(latest['RSI_14']), 1),
                        "ema20_stop": ema20_stop,
                        "chandelier_stop": chandelier_stop,
                        "exit_reason": f"Take profit at s-score > 0.0 (Equilibrium). Stop: ₹{ema20_stop}"
                    })
                    continue

            # ------------------------------------------------------------------
            # Screener 2: Hidden Markov Model (HMM) Regime Shift
            # ------------------------------------------------------------------
            if yf_sym not in seen_symbols:
                returns = close.pct_change().dropna()
                is_transition, prob, regime = fit_hmm_regime(returns)
                if is_transition and current_price > latest['EMA_50']:
                    seen_symbols.add(yf_sym)
                    hmm_results.append({
                        "symbol": item['Symbol'],
                        "sector": item.get('Sector', 'Unknown'),
                        "price": round(current_price, 2),
                        "regime": regime,
                        "regime_prob": prob,
                        "rsi": round(float(latest['RSI_14']), 1),
                        "ema20_stop": ema20_stop,
                        "chandelier_stop": chandelier_stop,
                        "exit_reason": f"Exit on regime flip or drop below Chandelier ₹{chandelier_stop}"
                    })
                    continue

            # ------------------------------------------------------------------
            # Screener 3: Volume-Weighted Trend Breakout (VOLAR + VWAP Alignment)
            # ------------------------------------------------------------------
            if yf_sym not in seen_symbols:
                c63 = close.tail(63)
                volar_3m = round(((c63.iloc[-1] / c63.iloc[0]) - 1) / (c63.pct_change().std() + 1e-9), 2)
                
                trend_aligned = current_price > latest['EMA_20'] > latest['EMA_50'] > latest['EMA_200']
                vol_surge = latest['Volume'] >= 1.5 * latest['Vol_SMA_20']
                near_high = pullback <= 0.10
                rsi_momentum = 55.0 <= latest['RSI_14'] <= 72.0

                if trend_aligned and vol_surge and near_high and rsi_momentum:
                    seen_symbols.add(yf_sym)
                    vwap_results.append({
                        "symbol": item['Symbol'],
                        "sector": item.get('Sector', 'Unknown'),
                        "price": round(current_price, 2),
                        "volar_3m": volar_3m,
                        "vol_ratio": round(float(latest['Volume'] / latest['Vol_SMA_20']), 2),
                        "pullback_pct": round(pullback * 100, 2),
                        "rsi": round(float(latest['RSI_14']), 1),
                        "ema20_stop": ema20_stop,
                        "chandelier_stop": chandelier_stop,
                        "exit_reason": f"Trail Chandelier ₹{chandelier_stop}. Exit on EMA 20 breach."
                    })
        except Exception as e:
            print(f"Error evaluating {yf_sym}: {e}")

    # Sorting sections by quantitative conviction
    statarb_results.sort(key=lambda x: x['s_score'])  # Most undervalued first
    hmm_results.sort(key=lambda x: x['regime_prob'], reverse=True)
    vwap_results.sort(key=lambda x: x['vol_ratio'], reverse=True)

    all_passed = statarb_results + hmm_results + vwap_results
    last_time = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    scanned_count = len(yf_symbols)

    payload = {
        'sections': {
            'statarb': statarb_results,
            'hmm': hmm_results,
            'vwap_breakout': vwap_results
        },
        'time': last_time,
        'source': source_name,
        'benchmark_label': bench_data.get('label', 'Nifty 500'),
        'scanned_count': scanned_count,
        'passed_count': len(all_passed),
        'excluded_count': scanned_count - len(all_passed),
        'price_data_asof': price_data_asof,
        'cache_hits': fetch_report['from_cache'],
        'yf_fetches': fetch_report['fetched'],
    }

    # Save to dedicated results JSON
    snapshot_filename = f"snapshot_quant_{uuid.uuid4().hex}.json"
    with open(os.path.join(SNAPSHOT_DIR, snapshot_filename), 'w') as f:
        json.dump(payload, f)

    with open(RESULTS_JSON, 'w') as f:
        json.dump(payload, f)

    # Save last 5 scans history
    history = _load_history()
    history.insert(0, {
        "time": last_time,
        "source": source_name,
        "count": len(all_passed),
        "count_statarb": len(statarb_results),
        "count_hmm": len(hmm_results),
        "count_vwap": len(vwap_results),
        "benchmark_label": bench_data.get('label', 'Nifty 500'),
        "price_data_asof": price_data_asof,
        "snapshot_file": snapshot_filename,
    })
    history = history[:HISTORY_LIMIT]
    with open(HISTORY_JSON, 'w') as f:
        json.dump(history, f)

    _prune_snapshots([h['snapshot_file'] for h in history if h.get('snapshot_file')])
    _set_progress(active=False, stage="done", current_symbol="")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@quant_screeners_bp.route("/quant-screeners", methods=["GET", "POST"])
def quant_screeners_view():
    if request.method == "POST":
        if _get_progress()["active"]:
            return redirect(url_for('quant_screeners.quant_screeners_view', scanning=1))

        file = request.files.get('file')
        use_default = request.form.get('use_default') == '1'

        if file and file.filename != '':
            from werkzeug.utils import secure_filename
            filename = secure_filename(file.filename)
            ext = os.path.splitext(filename)[1].lower()
            save_filename = f"uploaded_quant_tickers{ext}"
            filepath = os.path.join(UPLOAD_FOLDER, save_filename)
            file.save(filepath)
            with open(LAST_CSV_CONFIG, 'w') as f:
                json.dump({'path': filepath, 'name': filename}, f)
            source_path, source_name = filepath, filename
        elif use_default:
            source_path, source_name = DEFAULT_IND_CSV, DEFAULT_IND_LABEL
        else:
            source_path, source_name, _ = _get_active_source()

        if not source_path or not os.path.exists(source_path):
            err = f"Ticker source not found: {DEFAULT_IND_CSV}."
            _set_progress(active=False, stage="error", error=err)
            return redirect(url_for('quant_screeners.quant_screeners_view'))

        thread = threading.Thread(target=run_scan, args=(source_path, source_name), daemon=True)
        thread.start()
        return redirect(url_for('quant_screeners.quant_screeners_view', scanning=1))

    # --- GET Handler ---
    sections = {'statarb': [], 'hmm': [], 'vwap_breakout': []}
    last_time = benchmark_label = source_name = price_data_asof = None
    scanned_count = passed_count = excluded_count = 0
    cache_hits = yf_fetches = None

    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON) as f:
                cache = json.load(f)
            raw_secs = cache.get('sections', {})
            sections = {
                'statarb': raw_secs.get('statarb', []),
                'hmm': raw_secs.get('hmm', []),
                'vwap_breakout': raw_secs.get('vwap_breakout', [])
            }
            last_time       = cache.get('time')
            benchmark_label = cache.get('benchmark_label')
            source_name     = cache.get('source')
            scanned_count   = cache.get('scanned_count', 0)
            passed_count    = cache.get('passed_count', 0)
            excluded_count  = cache.get('excluded_count', 0)
            price_data_asof = cache.get('price_data_asof')
            cache_hits      = cache.get('cache_hits', 0)
            yf_fetches      = cache.get('yf_fetches', 0)
        except (json.JSONDecodeError, OSError):
            pass

    history = _load_history()
    progress = _get_progress()
    is_scanning = progress["active"] or request.args.get('scanning') == '1'
    _, active_file, is_default_source = _get_active_source()

    return render_template(
        "quant_screeners.html",
        statarb_stocks=sections['statarb'],
        hmm_stocks=sections['hmm'],
        vwap_stocks=sections['vwap_breakout'],
        last_time=last_time,
        benchmark_label=benchmark_label,
        source_name=source_name,
        scanned_count=scanned_count,
        passed_count=passed_count,
        excluded_count=excluded_count,
        price_data_asof=price_data_asof,
        cache_hits=cache_hits,
        yf_fetches=yf_fetches,
        history=history,
        active_file=active_file,
        is_default_source=is_default_source,
        default_label=DEFAULT_IND_LABEL,
        is_scanning=is_scanning,
        scan_error=progress.get("error"),
        restored=request.args.get('restored') == '1',
        restore_error=request.args.get('restore_error') == '1',
    )

@quant_screeners_bp.route("/quant-screeners/progress")
def quant_screeners_progress():
    return jsonify(_get_progress())

@quant_screeners_bp.route("/restore-quant-scan/<snapshot_file>", methods=["POST"])
def restore_quant_scan(snapshot_file):
    safe_name = os.path.basename(snapshot_file)
    snapshot_path = os.path.join(SNAPSHOT_DIR, safe_name)
    if not (safe_name.startswith('snapshot_quant_') and safe_name.endswith('.json') and os.path.exists(snapshot_path)):
        return redirect(url_for('quant_screeners.quant_screeners_view', restore_error=1))
    try:
        with open(snapshot_path) as f:
            payload = json.load(f)
        with open(RESULTS_JSON, 'w') as f:
            json.dump(payload, f)
    except Exception:
        return redirect(url_for('quant_screeners.quant_screeners_view', restore_error=1))
    return redirect(url_for('quant_screeners.quant_screeners_view', restored=1))

@quant_screeners_bp.route("/export-quant-screeners")
def export_quant_screeners():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON) as f:
            data = json.load(f)
        all_records = []
        for label, items in data.get('sections', {}).items():
            for item in items:
                rec = item.copy()
                rec['screener_category'] = label.upper()
                all_records.append(rec)
        if all_records:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_path = os.path.join(UPLOAD_FOLDER, 'temp_export_quant.csv')
            pd.DataFrame(all_records).to_csv(temp_path, index=False)
            return send_file(temp_path, as_attachment=True, download_name=f"Quant_Screeners_{timestamp}.csv")
    return "No scan data available.", 404

@quant_screeners_bp.route("/quant-screeners/guide")
def quant_screeners_guide():
    return render_template("quant_screeners_guide.html")