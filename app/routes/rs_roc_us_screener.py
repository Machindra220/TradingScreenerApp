import os
import json
import uuid
import threading
import pandas as pd
import requests
from io import StringIO
from datetime import datetime
from flask import Blueprint, render_template, request, send_file, redirect, url_for, jsonify

from app.services.market_data_cache import us_cache, latest_bar_date  # shared US SQLite cache

rs_roc_us_bp = Blueprint("rs_roc_us", __name__)

# Anchor all paths to __file__ so they resolve correctly regardless of where
# Flask was started — os.getcwd() at module-import time is fragile because the
# Werkzeug reloader can run in a different working directory (Memory #12).
_PROJECT_ROOT     = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER     = os.path.join(_PROJECT_ROOT, 'uploads', 'rs_roc_us')
HISTORY_CACHE_DIR = os.path.join(UPLOAD_FOLDER, 'history_cache')
SNAPSHOT_DIR      = os.path.join(UPLOAD_FOLDER, 'snapshots')
RESULTS_JSON      = os.path.join(UPLOAD_FOLDER, 'last_rs_roc_us_results.json')
HISTORY_JSON      = os.path.join(UPLOAD_FOLDER, 'scan_history_rs_roc_us.json')
DEFAULT_SP500_CSV = os.path.join(_PROJECT_ROOT, 'data', 'sp500.csv')
os.makedirs(UPLOAD_FOLDER,     exist_ok=True)
os.makedirs(HISTORY_CACHE_DIR, exist_ok=True)
os.makedirs(SNAPSHOT_DIR,      exist_ok=True)

HISTORY_LIMIT = 5
US_BENCHMARK  = "^GSPC"

# ---------------------------------------------------------------------------
# In-memory scan progress
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# S&P 500 symbol list (local CSV → Wikipedia fallback)
# ---------------------------------------------------------------------------

def load_sp500_symbols():
    """
    Load S&P 500 symbols + sector from the static CSV in data/sp500.csv
    (same file used by the Stage 2 US screener). Falls back to Wikipedia
    scraping if the CSV is absent, but prints a warning so you know it happened.

    Expected CSV columns: Symbol, Sector (or GICS Sector).
    Wikipedia-scraped columns are normalised to match.
    """
    if os.path.exists(DEFAULT_SP500_CSV):
        try:
            df = pd.read_csv(DEFAULT_SP500_CSV)
            col_map = {c.lower(): c for c in df.columns}
            sym_col = col_map.get('symbol', col_map.get('ticker'))
            sec_col = col_map.get('sector', col_map.get('gics sector', col_map.get('industry')))
            if sym_col:
                result = []
                for _, row in df.iterrows():
                    sym = str(row[sym_col]).strip().upper().replace('.', '-')
                    sec = str(row[sec_col]).strip() if sec_col else 'Unknown'
                    if sym:
                        result.append({'Symbol': sym, 'Industry': sec})
                print(f"[RS+ROC] Loaded {len(result)} symbols from {DEFAULT_SP500_CSV}")
                return result
        except Exception as e:
            print(f"[RS+ROC] Could not read sp500.csv: {e} — trying Wikipedia fallback")

    print("[RS+ROC] WARNING: sp500.csv not found — fetching from Wikipedia (fragile, internet required)")
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(
            'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies',
            headers=headers, timeout=15
        )
        tables = pd.read_html(StringIO(response.text))
        df = tables[0]
        df['Symbol'] = df['Symbol'].str.replace('.', '-', regex=False)
        return df[['Symbol', 'GICS Sector']].rename(columns={'GICS Sector': 'Industry'}).to_dict('records')
    except Exception as e:
        print(f"[RS+ROC] Wikipedia fallback also failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Schema normalisation (Memory #3 — never let template crash on missing keys)
# ---------------------------------------------------------------------------

def _normalize_stock(s):
    s.setdefault('rs_h',         [])
    s.setdefault('rs_up',        False)
    s.setdefault('rank_diff',    0)
    s.setdefault('rank_status',  'stable')
    s.setdefault('sector',       '')
    s.setdefault('roc_3m',       0.0)
    s.setdefault('roc_6m',       0.0)
    s.setdefault('rs_percentile', 0)
    return s


def _load_history():
    if os.path.exists(HISTORY_JSON):
        try:
            with open(HISTORY_JSON, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _prune_snapshots(keep_filenames):
    keep = set(keep_filenames)
    for fname in os.listdir(SNAPSHOT_DIR):
        if fname not in keep:
            try: os.remove(os.path.join(SNAPSHOT_DIR, fname))
            except OSError: pass


# ---------------------------------------------------------------------------
# Screening logic
# ---------------------------------------------------------------------------

def _compute_rs(stock_close, bench_close):
    """
    Ratio-of-relatives relative strength — consistent with every other
    screener in this codebase (Memory #1). Unlike simple subtraction
    (stock_return - bench_return), this stays monotonic in stock_return
    even when the benchmark return is negative.

    Old code used: rs = stock_ret_1y - bench_ret_1y
    Correct form:  rs = (1 + stock_ret) / (1 + bench_ret) - 1
    """
    stock_ret = (stock_close.iloc[-1] / stock_close.iloc[0]) - 1
    bench_ret = (bench_close.iloc[-1] / bench_close.iloc[0]) - 1
    if (1 + bench_ret) == 0:
        return None
    return ((1 + stock_ret) / (1 + bench_ret)) - 1


def screen_rs_roc(stock_list, price_data, bench_close):
    """
    Screens stock_list against pre-fetched DataFrames from the shared
    us_cache (no direct yfinance calls inside this function).

    Lookback periods:
      EMA200  : 200 sessions (Stage-2 uptrend filter)
      ROC 3M  : 63 sessions  (~3 calendar months, 21 trading days/month)
      ROC 6M  : 126 sessions (~6 calendar months)
      RS      : full available history in the cached DataFrame
    """
    results = []
    bench_close_clean = bench_close.dropna()

    for item in stock_list:
        sym = item['Symbol']
        df  = price_data.get(sym)
        if df is None or df.empty:
            continue
        try:
            close = df['Close'].dropna()

            # Need at least 200 sessions for EMA200 + 126 for ROC 6M lookback
            if len(close) < 200:
                continue

            # Align benchmark to this stock's trading dates
            bench_aligned = bench_close_clean.reindex(close.index).ffill()
            if bench_aligned.isna().any():
                continue

            current_price = float(close.iloc[-1])
            ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1]

            # Stage-2 uptrend filter: price must be above 200-day EMA
            if current_price <= ema200:
                continue

            rs_val = _compute_rs(close, bench_aligned)
            if rs_val is None:
                continue

            # ROC: (current / price_N_sessions_ago) - 1, expressed as %
            # iloc[-63]  = ~3 months ago, iloc[-126] = ~6 months ago
            # Guard against missing bars on short-history symbols
            roc_3m = ((current_price / float(close.iloc[-63]))  - 1) * 100 if len(close) >= 63  else None
            roc_6m = ((current_price / float(close.iloc[-126])) - 1) * 100 if len(close) >= 126 else None

            if roc_3m is None:
                continue

            results.append({
                "symbol":  sym,
                "sector":  item['Industry'],
                "price":   round(current_price, 2),
                "rs_raw":  round(rs_val, 4),
                "roc_3m":  round(roc_3m, 2),
                "roc_6m":  round(roc_6m, 2) if roc_6m is not None else None,
            })
        except Exception as e:
            print(f"  [RS+ROC] Error screening {sym}: {e}")

    if not results:
        return []

    df = pd.DataFrame(results)
    df['rs_raw'] = pd.to_numeric(df['rs_raw'], errors='coerce')
    df = df.dropna(subset=['rs_raw'])
    if df.empty:
        return []

    # RS percentile is ranked within the Stage-2 shortlist (above EMA200),
    # not the full S&P 500 universe.
    df['rs_percentile'] = df['rs_raw'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)

    return df.to_dict(orient='records')


# ---------------------------------------------------------------------------
# Background scan
# ---------------------------------------------------------------------------

def run_scan():
    _set_progress(active=True, processed=0, total=0, current_symbol="",
                  stage="loading_symbols", error=None)

    stock_list = load_sp500_symbols()
    if not stock_list:
        _set_progress(active=False, stage="error",
                      error="Could not load S&P 500 symbols. Check sp500.csv in data/ or internet connection.")
        return

    symbols = [s['Symbol'] for s in stock_list]
    _set_progress(stage="fetching_benchmark", total=len(symbols))

    # Fetch benchmark ONCE via the shared cache (Memory #8 — never per-stock)
    bench_data, _ = us_cache.get_price_history_bulk([US_BENCHMARK], interval='1d', lookback_days=500)
    bench_df = bench_data.get(US_BENCHMARK)
    if bench_df is None or bench_df.empty:
        _set_progress(active=False, stage="error",
                      error=f"Could not fetch benchmark {US_BENCHMARK}. Scan aborted.")
        return

    bench_close = bench_df['Close'].dropna()

    # Bulk-fetch all symbols through the shared US cache (Memory #8 + #13)
    def _fetch_progress(i, total, sym):
        _set_progress(stage="fetching_prices", processed=i, total=total, current_symbol=sym)

    price_data, fetch_report = us_cache.get_price_history_bulk(
        symbols, interval='1d', lookback_days=500, progress_callback=_fetch_progress
    )
    price_data_asof = latest_bar_date(price_data)

    # Cache source log (Memory #13)
    _n, _ch, _yf, _fl = len(symbols), fetch_report['from_cache'], fetch_report['fetched'], fetch_report['failed']
    sep = "=" * 55
    print(f"\n{sep}")
    print(f"  [CACHE] RS+ROC US — price data source summary")
    print(f"{sep}")
    print(f"  Total symbols   : {_n}")
    print(f"  From DB cache   : {_ch} ({round(_ch/_n*100) if _n else 0}%)  <- no yfinance call")
    print(f"  Fetched fresh   : {_yf}  ({round(_yf/_n*100) if _n else 0}%)  <- yfinance + DB updated")
    print(f"  Failed (429/err): {len(_fl)}")
    if _fl:
        extra = f" ...+{len(_fl)-10} more" if len(_fl) > 10 else ""
        print(f"  Failed symbols  : {', '.join(_fl[:10])}{extra}")
    print(f"  Price data as of: {price_data_asof}")
    print(f"{sep}\n")

    _set_progress(stage="scanning", processed=0, total=len(symbols), current_symbol="")

    # Load existing RS history and old ranks for trend tracking + rank delta
    old_ranks, existing_rs_h = {}, {}
    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON, 'r') as f:
                old_cache = json.load(f).get('stocks', [])
                old_ranks    = {s['symbol']: s['rank']   for s in old_cache}
                existing_rs_h = {s['symbol']: s.get('rs_h', []) for s in old_cache}
        except (json.JSONDecodeError, OSError):
            pass

    raw_results = screen_rs_roc(stock_list, price_data, bench_close)

    if raw_results:
        df = pd.DataFrame(raw_results)
        df['rs_percentile'] = df['rs_raw'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)

        # Sort by RS percentile DESC (primary), ROC 3M DESC (tiebreak — this
        # page's stated priority is RS first, then 3M momentum)
        df.sort_values(by=['rs_percentile', 'roc_3m'], ascending=[False, False], inplace=True)
        df.reset_index(drop=True, inplace=True)
        df['rank'] = df.index + 1

        def inject_history(row):
            h = existing_rs_h.get(row['symbol'], [])
            row['rs_h']  = (h + [row['rs_percentile']])[-5:]
            row['rs_up'] = len(row['rs_h']) > 1 and all(x < y for x, y in zip(row['rs_h'], row['rs_h'][1:]))
            prev = old_ranks.get(row['symbol'])
            if prev is None:
                row['rank_status'], row['rank_diff'] = 'new', 0
            else:
                diff = prev - row['rank']
                row['rank_diff']   = diff
                row['rank_status'] = 'up' if diff > 0 else ('down' if diff < 0 else 'stable')
            return row

        df = df.apply(inject_history, axis=1)
        stocks = df.to_dict(orient='records')
    else:
        stocks = []

    last_time = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    leaders_90 = [s['symbol'] for s in stocks if s.get('rs_percentile', 0) >= 90]
    scanned_count  = len(symbols)
    excluded_count = scanned_count - len(stocks)

    payload = {
        'stocks':               stocks,
        'time':                 last_time,
        'benchmark_label':      f"S&P 500 ({US_BENCHMARK})",
        'scanned_count':        scanned_count,
        'excluded_count':       excluded_count,
        'stale_symbols_count':  len(_fl),
        'stale_symbols_sample': _fl[:10],
        'price_data_asof':      price_data_asof,
        'cache_hits':           _ch,
        'yf_fetches':           _yf,
    }

    # Snapshot BEFORE overwriting active results (Memory #5 — always restorable)
    snapshot_filename = f"snapshot_{uuid.uuid4().hex}.json"
    with open(os.path.join(SNAPSHOT_DIR, snapshot_filename), 'w') as f:
        json.dump(payload, f)

    with open(RESULTS_JSON, 'w') as f:
        json.dump(payload, f)

    history = _load_history()
    history.insert(0, {
        "time":            last_time,
        "count":           len(stocks),
        "leaders_90":      leaders_90,
        "benchmark_label": f"S&P 500 ({US_BENCHMARK})",
        "price_data_asof": price_data_asof,
        "snapshot_file":   snapshot_filename,
    })
    history = history[:HISTORY_LIMIT]
    with open(HISTORY_JSON, 'w') as f:
        json.dump(history, f)

    _prune_snapshots([h['snapshot_file'] for h in history if h.get('snapshot_file')])
    _set_progress(active=False, stage="done", current_symbol="")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@rs_roc_us_bp.route("/rs-roc-us-momentum", methods=["GET", "POST"])
def rs_roc_us_momentum_process():
    if request.method == "POST":
        if _get_progress()["active"]:
            return redirect(url_for('rs_roc_us.rs_roc_us_momentum_process', scanning=1))
        thread = threading.Thread(target=run_scan, daemon=True)
        thread.start()
        return redirect(url_for('rs_roc_us.rs_roc_us_momentum_process', scanning=1))

    # --- GET ---
    stocks, last_time = [], None
    benchmark_label = None
    excluded_count = scanned_count = stale_symbols_count = 0
    stale_symbols_sample = []
    price_data_asof = cache_hits = yf_fetches = None

    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON, 'r') as f:
                cache = json.load(f)
                stocks              = [_normalize_stock(s) for s in cache.get('stocks', [])]
                last_time           = cache.get('time')
                benchmark_label     = cache.get('benchmark_label')
                excluded_count      = cache.get('excluded_count', 0)
                scanned_count       = cache.get('scanned_count', 0)
                stale_symbols_count = cache.get('stale_symbols_count', 0)
                stale_symbols_sample = cache.get('stale_symbols_sample', [])
                price_data_asof     = cache.get('price_data_asof')
                cache_hits          = cache.get('cache_hits', 0)
                yf_fetches          = cache.get('yf_fetches', 0)
        except (json.JSONDecodeError, OSError):
            pass

    history  = _load_history()
    progress = _get_progress()
    is_scanning = progress["active"] or request.args.get('scanning') == '1'

    return render_template(
        "rs_roc_us_momentum.html",
        stocks=stocks,
        last_time=last_time,
        benchmark_label=benchmark_label,
        excluded_count=excluded_count,
        scanned_count=scanned_count,
        stale_symbols_count=stale_symbols_count,
        stale_symbols_sample=stale_symbols_sample,
        price_data_asof=price_data_asof,
        cache_hits=cache_hits,
        yf_fetches=yf_fetches,
        history=history,
        is_scanning=is_scanning,
        scan_error=progress.get("error"),
        restored=request.args.get('restored') == '1',
        restore_error=request.args.get('restore_error') == '1',
    )


@rs_roc_us_bp.route("/rs-roc-us-momentum/progress")
def rs_roc_us_progress():
    return jsonify(_get_progress())


@rs_roc_us_bp.route("/restore-rs-roc-us/<snapshot_file>", methods=["POST"])
def restore_rs_roc_us_snapshot(snapshot_file):
    """POST-only restore — prevents stray click/prefetch from overwriting live
    results with an old scan (Memory #11)."""
    safe_name     = os.path.basename(snapshot_file)
    snapshot_path = os.path.join(SNAPSHOT_DIR, safe_name)
    valid = safe_name.startswith('snapshot_') and safe_name.endswith('.json') and os.path.exists(snapshot_path)
    if not valid:
        return redirect(url_for('rs_roc_us.rs_roc_us_momentum_process', restore_error=1))
    try:
        with open(snapshot_path, 'r') as f:
            payload = json.load(f)
        with open(RESULTS_JSON, 'w') as f:
            json.dump(payload, f)
    except (json.JSONDecodeError, OSError):
        return redirect(url_for('rs_roc_us.rs_roc_us_momentum_process', restore_error=1))
    return redirect(url_for('rs_roc_us.rs_roc_us_momentum_process', restored=1))


@rs_roc_us_bp.route("/export-rs-roc-us")
def export_rs_roc_us():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            stocks = json.load(f).get('stocks', [])
        if stocks:
            df = pd.DataFrame(stocks)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_path = os.path.join(UPLOAD_FOLDER, 'temp_export.csv')
            df.to_csv(temp_path, index=False)
            return send_file(temp_path, as_attachment=True,
                             download_name=f"US_RS_ROC_Screener_{timestamp}.csv")
    return "No scan data available to export", 404