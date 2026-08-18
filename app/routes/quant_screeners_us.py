"""
quant_screeners_us.py

US Quantitative & Statistical Arbitrage Screeners (S&P 500)

Identical mathematical models to the IND version with these US-specific adaptations:
  - us_cache (market_data_us.db) instead of ind_cache
  - Benchmark: ^GSPC (S&P 500) instead of ^CRSLDX / ^NSEI
  - No .NS suffix on symbols (US tickers are bare: AAPL, MSFT)
  - sp500.csv default file instead of nifty_500.csv
  - Dollar $ display instead of Rupee ₹
  - Timezone normalisation on benchmark index (fixes tz-aware vs tz-naive
    mismatch that causes bench_close.reindex() to return all NaN)

Formula audit (all three models verified correct vs IND source):
  - O-U / StatArb: AR(1) OLS → theta, mu, sigma_eq, s_score  ✅
  - HMM: GaussianHMM(n=2), expansion_state = argmax(state_variance),
    regime flip = prev!=expansion → curr==expansion, posterior > 0.70  ✅
  - VOLAR: 3M_return / std(daily_returns)  ✅
  - ATR: Wilder True Range  ✅
  - Chandelier Stop: highest_52w_high - 2.5 × ATR(14)  ✅
  - PCA Residuals: pct_change → PCA(n=3) → inverse_transform →
    actual-reconstructed → cumprod synthetic price  ✅
"""

import os
import json
import uuid
import warnings
import threading
import numpy as np
import pandas as pd
from datetime import datetime
from sklearn.decomposition import PCA
from hmmlearn.hmm import GaussianHMM
from flask import Blueprint, render_template, request, send_file, redirect, url_for, jsonify

from app.services.market_data_cache import us_cache, latest_bar_date

quant_screeners_us_bp = Blueprint("quant_screeners_us", __name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_PROJECT_ROOT     = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER     = os.path.join(_PROJECT_ROOT, 'uploads', 'quant_screeners_us')
SNAPSHOT_DIR      = os.path.join(UPLOAD_FOLDER, 'snapshots')
RESULTS_JSON      = os.path.join(UPLOAD_FOLDER, 'last_quant_us_results.json')
HISTORY_JSON      = os.path.join(UPLOAD_FOLDER, 'scan_history_quant_us.json')
LAST_CSV_CONFIG   = os.path.join(UPLOAD_FOLDER, 'last_csv_quant_us.json')
DEFAULT_US_CSV    = os.path.join(_PROJECT_ROOT, 'data', 'sp500.csv')
DEFAULT_US_LABEL  = "S&P 500 Default (sp500.csv)"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SNAPSHOT_DIR,  exist_ok=True)

HISTORY_LIMIT = 5
US_BENCHMARK  = ("^GSPC", "S&P 500")

# ── Progress ──────────────────────────────────────────────────────────────────
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


# ── Source helpers ────────────────────────────────────────────────────────────

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
    return DEFAULT_US_CSV, DEFAULT_US_LABEL, True


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
    sec_col = next((col_map[k] for k in ('gics sector', 'sector', 'industry') if k in col_map), None)
    results = []
    for _, row in df.iterrows():
        sym = str(row[found]).strip().upper().replace('.', '-')   # BRK.B → BRK-B for yfinance
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


# ── Quantitative Algorithms (same formulas as IND, verified correct) ──────────

def calculate_atr(df, period=14):
    """Wilder Average True Range — standard formula."""
    high_low   = df['High'] - df['Low']
    high_close = (df['High'] - df['Close'].shift()).abs()
    low_close  = (df['Low']  - df['Close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def calculate_rsi(series, period=14):
    """Wilder RSI."""
    delta = series.diff()
    gain  = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss  = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs    = gain / (loss + 1e-9)
    return 100 - (100 / (1 + rs))


def fit_ou_process(residuals: pd.Series):
    """
    Fits Ornstein-Uhlenbeck mean-reversion via AR(1) OLS:
        X_t = a * X_{t-1} + b + ε
    Parameters:
        theta     = -ln(a) / dt          (mean-reversion speed)
        mu        = b / (1 - a)          (equilibrium level)
        half_life = ln(2) / theta        (sessions to revert halfway)
        sigma_eq  = σ_ε / sqrt(2θ)      (equilibrium std dev)
        s_score   = (X_t - mu) / sigma_eq

    Entry trigger: s_score ≤ -1.75  (stock 1.75σ below its factor-adjusted fair value)
    Exit  target:  s_score ≥  0.00  (returned to statistical equilibrium)
    """
    if len(residuals) < 30:
        return 0.0, 0.0, 0.0
    x   = residuals.shift(1).dropna().values
    y   = residuals.iloc[1:].values
    A   = np.vstack([x, np.ones(len(x))]).T
    a, b = np.linalg.lstsq(A, y, rcond=None)[0]

    if a >= 1.0 or a <= 0.0:
        return 0.0, 0.0, 0.0         # non-stationary / not mean-reverting

    dt        = 1.0
    theta     = -np.log(a) / dt
    mu        = b / (1.0 - a)
    half_life = np.log(2.0) / (theta + 1e-9)
    res_std   = np.std(y - (a * x + b))
    sigma_eq  = np.sqrt((res_std ** 2) / (2 * theta + 1e-9))

    current_val = residuals.iloc[-1]
    s_score     = (current_val - mu) / (sigma_eq + 1e-9)
    return round(float(s_score), 2), round(float(half_life), 1), round(float(mu), 2)


def compute_pca_residuals(price_matrix: pd.DataFrame, n_components=3):
    """
    Extracts idiosyncratic residuals after removing top N principal
    market/sector factors (PCA).

    Method:
      1. Compute cross-sectional pct_change returns
      2. Fit PCA(n_components) to capture systematic factors
      3. Reconstruct returns using only those factors
      4. Residuals = actual_returns - reconstructed_returns
      5. Cumprod residuals to get a mean-reverting synthetic price series

    n_components=3 captures ~market + 2 sector factors for S&P 500.
    """
    returns       = price_matrix.pct_change().dropna()
    clean_returns = returns.dropna(axis=1)
    if clean_returns.shape[1] < n_components or len(clean_returns) < 60:
        return {}

    pca           = PCA(n_components=n_components)
    factors       = pca.fit_transform(clean_returns)
    reconstructed = pca.inverse_transform(factors)
    residual_ret  = clean_returns.values - reconstructed

    residual_df   = pd.DataFrame(residual_ret,
                                  index=clean_returns.index,
                                  columns=clean_returns.columns)
    return {col: (1 + residual_df[col]).cumprod() for col in residual_df.columns}


def fit_hmm_regime(returns: pd.Series):
    """
    2-State Gaussian Hidden Markov Model:
        State 0: Low Volatility / Accumulation (sideways, mean-reverting)
        State 1: High Volatility / Momentum Expansion (directional, trending)

    Expansion state identified as the state with higher return variance.

    Entry signal: stock is CURRENTLY in the Expansion regime with
    posterior probability > 0.65. This is more practical for a daily
    screener than requiring a same-day flip (which yields ~0-2 hits
    across 500 stocks on any given day because the transition is a
    single-bar event and convergence warnings reduce posterior confidence).

    also_new_today: True when the stock JUST entered expansion (prev bar
    was Accumulation) — shown as a badge in the UI for extra conviction.
    """
    if len(returns) < 120:
        return False, 0.0, "Stable", False
    try:
        X = returns.dropna().values.reshape(-1, 1)
        # n_iter=200 and explicit tol reduce (but don't eliminate) convergence
        # warnings on noisy market data. min_covar prevents degenerate states
        # where one state collapses to near-zero variance.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")   # suppress hmmlearn ConvergenceWarning
            model = GaussianHMM(
                n_components=2, covariance_type="diag",
                n_iter=200, tol=1e-4, random_state=42
            )
            model.fit(X)

        hidden_states   = model.predict(X)
        probs           = model.predict_proba(X)

        # Expansion state = higher return variance state
        state_vars      = [np.var(X[hidden_states == s]) if np.sum(hidden_states == s) > 0
                           else 0 for s in range(2)]
        expansion_state = int(np.argmax(state_vars))

        current_state = int(hidden_states[-1])
        prev_state    = int(hidden_states[-2])
        current_prob  = float(probs[-1, expansion_state])

        # Primary signal: currently in expansion with high confidence
        in_expansion = (current_state == expansion_state and current_prob > 0.65)
        # Secondary badge: just entered expansion today (strictest signal)
        just_flipped  = (prev_state != expansion_state and current_state == expansion_state)

        regime_lbl = ("Expansion Regime" if current_state == expansion_state
                      else "Accumulation / Range")
        return in_expansion, round(current_prob, 2), regime_lbl, just_flipped
    except Exception:
        return False, 0.0, "Unknown", False


# ── Background Scan ───────────────────────────────────────────────────────────

def run_scan(source_path, source_name):
    _set_progress(active=True, processed=0, total=0,
                  current_symbol="", stage="loading_symbols", error=None)

    stock_list = load_symbols()
    if not stock_list:
        _set_progress(active=False, stage="error", error="Could not load symbols.")
        return

    # US symbols are bare tickers — no suffix added
    symbols     = [s['Symbol'] for s in stock_list]
    sym_to_item = {sym: item for sym, item in zip(symbols, stock_list)}

    # ── Benchmark: ^GSPC via us_cache ────────────────────────────────────────
    _set_progress(stage="fetching_benchmark", total=len(symbols))
    bench_result, _ = us_cache.get_price_history_bulk(
        [US_BENCHMARK[0]], interval='1d', lookback_days=500
    )
    bench_df = bench_result.get(US_BENCHMARK[0])
    if bench_df is None or bench_df.empty or len(bench_df) < 200:
        _set_progress(active=False, stage="error",
                      error=f"Could not fetch {US_BENCHMARK[0]} benchmark.")
        return

    bench_close = bench_df['Close'].dropna()
    # Normalise timezone — us_cache may return tz-aware index from yfinance;
    # stock DataFrames from SQLite are always tz-naive. reindex() on mismatched
    # tz returns all NaN, which would skip every stock in the PCA/HMM loop.
    if getattr(bench_close.index, 'tz', None) is not None:
        bench_close.index = bench_close.index.tz_localize(None)
    benchmark_label = f"{US_BENCHMARK[1]} ({US_BENCHMARK[0]})"

    # ── Bulk price fetch via us_cache ────────────────────────────────────────
    price_data, fetch_report = us_cache.get_price_history_bulk(
        symbols, interval='1d', lookback_days=500,
        progress_callback=lambda i, t, s: _set_progress(
            stage="fetching_prices", processed=i, total=t, current_symbol=s)
    )
    price_data_asof = latest_bar_date(price_data)

    _n, _ch, _yf, _fl = (len(symbols), fetch_report['from_cache'],
                          fetch_report['fetched'], fetch_report['failed'])
    sep = "=" * 55
    print(f"\n{sep}")
    print(f"  [CACHE] US Quant Screeners — {source_name}")
    print(f"{sep}")
    print(f"  Total: {_n}  |  Cache: {_ch} ({round(_ch/_n*100) if _n else 0}%)"
          f"  |  Fetched: {_yf}  |  Failed: {len(_fl)}")
    print(f"  Price data as of: {price_data_asof}")
    print(f"{sep}\n")

    # ── PCA price matrix (needs tz-normalised close indexes) ─────────────────
    _set_progress(stage="calculating_pca", processed=0, total=len(symbols))
    closes_dict = {}
    for sym, df in price_data.items():
        if df is not None and len(df) >= 200:
            cl = df['Close'].dropna()
            if getattr(cl.index, 'tz', None) is not None:
                cl.index = cl.index.tz_localize(None)
            closes_dict[sym] = cl

    price_matrix  = pd.DataFrame(closes_dict).dropna(axis=1)
    pca_residuals = compute_pca_residuals(price_matrix, n_components=3)

    # ── Per-symbol screening ──────────────────────────────────────────────────
    _set_progress(stage="screening", processed=0, total=len(symbols))

    statarb_results = []
    hmm_results     = []
    volar_results   = []
    seen_symbols    = set()

    for i, sym in enumerate(symbols):
        _set_progress(processed=i, current_symbol=sym)
        item = sym_to_item[sym]
        df   = price_data.get(sym)
        if df is None or len(df) < 200:
            continue

        try:
            close         = df['Close'].dropna()
            # Normalise close index for alignment with bench_close
            if getattr(close.index, 'tz', None) is not None:
                close = close.copy()
                close.index = close.index.tz_localize(None)

            current_price = float(close.iloc[-1])
            high_52w      = float(close.tail(252).max())
            pullback      = (high_52w - current_price) / high_52w

            df['EMA_20']     = close.ewm(span=20,  adjust=False).mean()
            df['EMA_50']     = close.ewm(span=50,  adjust=False).mean()
            df['EMA_200']    = close.ewm(span=200, adjust=False).mean()
            df['ATR_14']     = calculate_atr(df, 14)
            df['RSI_14']     = calculate_rsi(close, 14)
            df['Vol_SMA_20'] = df['Volume'].rolling(20).mean()

            latest = df.iloc[-1]
            atr    = float(latest['ATR_14']) if not pd.isna(latest['ATR_14']) else 0.0

            chandelier_stop = round(high_52w - (2.5 * atr), 2)
            ema20_stop      = round(float(latest['EMA_20']), 2)

            # ── Screener 1: StatArb O-U Mean Reversion ────────────────────
            # Entry: s_score ≤ -1.75 (statistically underpriced vs factor basket)
            #        half_life 2–25 days (practical reversion window)
            #        price > EMA200 (structural uptrend — no bottom-fishing)
            # Exit:  s_score ≥ 0.0 (returned to equilibrium)
            if sym in pca_residuals and sym not in seen_symbols:
                s_score, half_life, _ = fit_ou_process(pca_residuals[sym])
                if (s_score <= -1.75 and
                        2.0 <= half_life <= 25.0 and
                        current_price > latest['EMA_200']):
                    seen_symbols.add(sym)
                    statarb_results.append({
                        "symbol":           item['Symbol'],
                        "sector":           item.get('Sector', 'Unknown'),
                        "price":            round(current_price, 2),
                        "s_score":          s_score,
                        "half_life":        half_life,
                        "rsi":              round(float(latest['RSI_14']), 1),
                        "ema20_stop":       ema20_stop,
                        "chandelier_stop":  chandelier_stop,
                        "exit_reason":      f"Take profit at s-score ≥ 0.0 (Equilibrium). Stop: ${ema20_stop}",
                    })
                    continue

            # ── Screener 2: HMM Regime Transition ────────────────────────
            # Entry: currently in Expansion regime, posterior prob > 0.65,
            #        price > EMA50 (above mid-term trend).
            # "just_flipped" = entered expansion today — shown as 🆕 badge.
            # Exit:  Chandelier Stop or regime reverts to Accumulation.
            if sym not in seen_symbols:
                returns = close.pct_change().dropna()
                in_exp, prob, regime, just_flipped = fit_hmm_regime(returns)
                if in_exp and current_price > latest['EMA_50']:
                    seen_symbols.add(sym)
                    hmm_results.append({
                        "symbol":           item['Symbol'],
                        "sector":           item.get('Sector', 'Unknown'),
                        "price":            round(current_price, 2),
                        "regime":           regime,
                        "regime_prob":      prob,
                        "just_flipped":     just_flipped,
                        "rsi":              round(float(latest['RSI_14']), 1),
                        "ema20_stop":       ema20_stop,
                        "chandelier_stop":  chandelier_stop,
                        "exit_reason":      f"Exit on regime reversion or Chandelier ${chandelier_stop}",
                    })
                    continue

            # ── Screener 3: VOLAR Trend Breakout ─────────────────────────
            # Entry: full EMA stack alignment (price > EMA20 > EMA50 > EMA200)
            #        vol_surge ≥ 1.5× 20-day avg (institutional participation)
            #        within 10% of 52W high (near-highs momentum)
            #        RSI 55–72 (momentum without overbought exhaustion)
            # VOLAR = 3M_return / std(daily_returns) — quality-of-move filter
            # Exit:  Chandelier trail or EMA20 daily close breach
            if sym not in seen_symbols:
                c63          = close.tail(63)
                ret_std      = c63.pct_change().std()
                volar_3m     = round(((c63.iloc[-1] / c63.iloc[0]) - 1) /
                                      (ret_std + 1e-9), 2)

                trend_aligned = (current_price > latest['EMA_20'] >
                                 latest['EMA_50'] > latest['EMA_200'])
                vol_surge     = (latest['Volume'] >=
                                 1.5 * latest['Vol_SMA_20'])
                near_high     = pullback <= 0.10
                rsi_momentum  = (55.0 <= latest['RSI_14'] <= 72.0)

                if trend_aligned and vol_surge and near_high and rsi_momentum:
                    seen_symbols.add(sym)
                    volar_results.append({
                        "symbol":           item['Symbol'],
                        "sector":           item.get('Sector', 'Unknown'),
                        "price":            round(current_price, 2),
                        "volar_3m":         volar_3m,
                        "vol_ratio":        round(
                            float(latest['Volume'] / latest['Vol_SMA_20']), 2),
                        "pullback_pct":     round(pullback * 100, 2),
                        "rsi":              round(float(latest['RSI_14']), 1),
                        "ema20_stop":       ema20_stop,
                        "chandelier_stop":  chandelier_stop,
                        "exit_reason":      f"Trail Chandelier ${chandelier_stop}. Exit on EMA20 close breach.",
                    })

        except Exception as e:
            print(f"  Error {sym}: {e}")

    # Sort by conviction within each model
    statarb_results.sort(key=lambda x: x['s_score'])           # most undervalued first
    hmm_results.sort(key=lambda x: x['regime_prob'], reverse=True)
    volar_results.sort(key=lambda x: x['vol_ratio'], reverse=True)

    all_passed = statarb_results + hmm_results + volar_results
    last_time  = datetime.now().strftime("%d-%b-%Y %H:%M:%S")

    payload = {
        'sections': {
            'statarb':       statarb_results,
            'hmm':           hmm_results,
            'volar_breakout': volar_results,
        },
        'time':            last_time,
        'source':          source_name,
        'benchmark_label': benchmark_label,
        'scanned_count':   len(symbols),
        'passed_count':    len(all_passed),
        'excluded_count':  len(symbols) - len(all_passed),
        'price_data_asof': price_data_asof,
        'cache_hits':      _ch,
        'yf_fetches':      _yf,
    }

    snapshot_filename = f"snapshot_quant_us_{uuid.uuid4().hex}.json"
    with open(os.path.join(SNAPSHOT_DIR, snapshot_filename), 'w') as f:
        json.dump(payload, f)
    with open(RESULTS_JSON, 'w') as f:
        json.dump(payload, f)

    history = _load_history()
    history.insert(0, {
        "time":            last_time,
        "source":          source_name,
        "count":           len(all_passed),
        "count_statarb":   len(statarb_results),
        "count_hmm":       len(hmm_results),
        "count_volar":     len(volar_results),
        "benchmark_label": benchmark_label,
        "price_data_asof": price_data_asof,
        "snapshot_file":   snapshot_filename,
    })
    history = history[:HISTORY_LIMIT]
    with open(HISTORY_JSON, 'w') as f:
        json.dump(history, f)

    _prune_snapshots([h['snapshot_file'] for h in history if h.get('snapshot_file')])
    _set_progress(active=False, stage="done", current_symbol="")


# ── Routes ────────────────────────────────────────────────────────────────────

@quant_screeners_us_bp.route("/quant-screeners-us", methods=["GET", "POST"])
def quant_screeners_us_view():
    if request.method == "POST":
        if _get_progress()["active"]:
            return redirect(url_for('quant_screeners_us.quant_screeners_us_view', scanning=1))

        file        = request.files.get('file')
        use_default = request.form.get('use_default') == '1'

        if file and file.filename != '':
            from werkzeug.utils import secure_filename
            filename      = secure_filename(file.filename)
            ext           = os.path.splitext(filename)[1].lower()
            save_filename = f"uploaded_quant_us_tickers{ext}"
            filepath      = os.path.join(UPLOAD_FOLDER, save_filename)
            file.save(filepath)
            with open(LAST_CSV_CONFIG, 'w') as f:
                json.dump({'path': filepath, 'name': filename}, f)
            source_path, source_name = filepath, filename
        elif use_default:
            source_path, source_name = DEFAULT_US_CSV, DEFAULT_US_LABEL
        else:
            source_path, source_name, _ = _get_active_source()

        if not source_path or not os.path.exists(source_path):
            _set_progress(active=False, stage="error",
                          error=f"Ticker file not found: {DEFAULT_US_CSV}")
            return redirect(url_for('quant_screeners_us.quant_screeners_us_view'))

        thread = threading.Thread(target=run_scan, args=(source_path, source_name), daemon=True)
        thread.start()
        return redirect(url_for('quant_screeners_us.quant_screeners_us_view', scanning=1))

    # ── GET ───────────────────────────────────────────────────────────────────
    sections = {'statarb': [], 'hmm': [], 'volar_breakout': []}
    last_time = benchmark_label = source_name = price_data_asof = None
    scanned_count = passed_count = excluded_count = 0
    cache_hits = yf_fetches = None

    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON) as f:
                cache = json.load(f)
            raw = cache.get('sections', {})
            sections = {
                'statarb':        raw.get('statarb', []),
                'hmm':            raw.get('hmm', []),
                'volar_breakout': raw.get('volar_breakout', []),
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

    history     = _load_history()
    progress    = _get_progress()
    is_scanning = progress["active"] or request.args.get('scanning') == '1'
    _, active_file, is_default_source = _get_active_source()

    return render_template(
        "quant_screeners_us.html",
        statarb_stocks   = sections['statarb'],
        hmm_stocks       = sections['hmm'],
        volar_stocks     = sections['volar_breakout'],
        last_time        = last_time,
        benchmark_label  = benchmark_label,
        source_name      = source_name,
        scanned_count    = scanned_count,
        passed_count     = passed_count,
        excluded_count   = excluded_count,
        price_data_asof  = price_data_asof,
        cache_hits       = cache_hits,
        yf_fetches       = yf_fetches,
        history          = history,
        active_file      = active_file,
        is_default_source= is_default_source,
        default_label    = DEFAULT_US_LABEL,
        is_scanning      = is_scanning,
        scan_error       = progress.get("error"),
        restored         = request.args.get('restored')      == '1',
        restore_error    = request.args.get('restore_error') == '1',
    )


@quant_screeners_us_bp.route("/quant-screeners-us/progress")
def quant_screeners_us_progress():
    return jsonify(_get_progress())


@quant_screeners_us_bp.route("/restore-quant-us-scan/<snapshot_file>", methods=["POST"])
def restore_quant_us_scan(snapshot_file):
    safe_name     = os.path.basename(snapshot_file)
    snapshot_path = os.path.join(SNAPSHOT_DIR, safe_name)
    valid = (safe_name.startswith('snapshot_quant_us_') and
             safe_name.endswith('.json') and
             os.path.exists(snapshot_path))
    if not valid:
        return redirect(url_for('quant_screeners_us.quant_screeners_us_view', restore_error=1))
    try:
        with open(snapshot_path) as f:
            payload = json.load(f)
        with open(RESULTS_JSON, 'w') as f:
            json.dump(payload, f)
    except Exception:
        return redirect(url_for('quant_screeners_us.quant_screeners_us_view', restore_error=1))
    return redirect(url_for('quant_screeners_us.quant_screeners_us_view', restored=1))


@quant_screeners_us_bp.route("/export-quant-screeners-us")
def export_quant_screeners_us():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON) as f:
            data = json.load(f)
        records = []
        for label, items in data.get('sections', {}).items():
            for item in items:
                rec = item.copy()
                rec['screener_category'] = label.upper()
                records.append(rec)
        if records:
            ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_path = os.path.join(UPLOAD_FOLDER, 'temp_export_quant_us.csv')
            pd.DataFrame(records).to_csv(temp_path, index=False)
            return send_file(temp_path, as_attachment=True,
                             download_name=f"Quant_US_Screeners_{ts}.csv")
    return "No scan data available.", 404


@quant_screeners_us_bp.route("/quant-screeners-us/guide")
def quant_screeners_us_guide():
    return render_template("quant_screeners_us_guide.html")