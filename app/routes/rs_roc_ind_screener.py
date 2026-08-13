import os
import json
import uuid
import threading
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Blueprint, render_template, request, send_file, redirect, url_for, jsonify

from app.services.market_data_cache import ind_cache, latest_bar_date  # shared IND SQLite cache

rs_roc_bp = Blueprint("rs_roc", __name__)

# Anchor all paths to __file__ — os.getcwd() at module level breaks after
# Werkzeug hot-reload because the reloader child process may have a different
# working directory than the main Flask process (Memory #12).
_PROJECT_ROOT     = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER     = os.path.join(_PROJECT_ROOT, 'uploads', 'rs_roc')
HISTORY_CACHE_DIR = os.path.join(UPLOAD_FOLDER, 'history_cache')
SNAPSHOT_DIR      = os.path.join(UPLOAD_FOLDER, 'snapshots')
RESULTS_JSON      = os.path.join(UPLOAD_FOLDER, 'last_rs_roc_results.json')
HISTORY_JSON      = os.path.join(UPLOAD_FOLDER, 'scan_history_rs_roc_ind.json')
LAST_CSV_CONFIG   = os.path.join(UPLOAD_FOLDER, 'last_csv_path.json')
DEFAULT_IND_CSV   = os.path.join(_PROJECT_ROOT, 'data', 'nifty_500.csv')
DEFAULT_IND_LABEL = "Nifty 500 Default (nifty_500.csv)"

os.makedirs(UPLOAD_FOLDER,     exist_ok=True)
os.makedirs(HISTORY_CACHE_DIR, exist_ok=True)
os.makedirs(SNAPSHOT_DIR,      exist_ok=True)

HISTORY_LIMIT = 5

# Benchmark: Nifty 500 (^CRSLDX) is the correct broad-market index for this
# screener's RS comparison (the page explicitly says "vs Nifty 500"). Nifty 50
# (^NSEI) was used before — that mismatch means stocks are ranked by how well
# they beat a narrow 50-stock index, not the 500-stock universe the screener's
# universe comes from. ^CRSLDX is tried first; ^NSEI used as fallback.
PRIMARY_BENCHMARK  = ("^CRSLDX", "Nifty 500")
FALLBACK_BENCHMARK = ("^NSEI",   "Nifty 50")

# ---------------------------------------------------------------------------
# Progress tracking
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
# Source-selection (Memory #9 / #10)
# ---------------------------------------------------------------------------

def _get_active_source():
    """Returns (filepath, display_name, is_default)."""
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


# ---------------------------------------------------------------------------
# Ticker list loading
# ---------------------------------------------------------------------------

def _read_ticker_file(filepath):
    """Read CSV or XLSX; accept Symbol/Ticker column (case-insensitive)."""
    ext = os.path.splitext(filepath)[1].lower()
    try:
        df = pd.read_excel(filepath) if ext in ('.xlsx', '.xls') else pd.read_csv(filepath)
    except Exception as e:
        raise ValueError(f"Could not read file: {e}")
    col_map = {c.lower(): c for c in df.columns}
    found   = next((col_map[k] for k in ('symbol', 'ticker', 'symbols', 'tickers') if k in col_map), None)
    if found is None:
        raise ValueError(f"File must have a Symbol or Ticker column. Found: {', '.join(df.columns.tolist())}")
    # Also try to pick up an Industry/Sector column for the sector display
    sec_col = next((col_map[k] for k in ('industry', 'sector', 'gics sector') if k in col_map), None)
    results = []
    for _, row in df.iterrows():
        sym = str(row[found]).strip().upper()
        sec = str(row[sec_col]).strip() if sec_col else 'Unknown'
        if sym:
            results.append({'Symbol': sym, 'Industry': sec})
    return results


def load_nifty500_symbols():
    """
    Load Nifty 500 symbols from the static CSV in data/nifty_500.csv
    (same file used by the VOLAR IND screeners). Falls back to a live NSE
    download if the CSV is absent, but prints a warning so you know.
    """
    source, _, is_default = _get_active_source()

    if os.path.exists(source):
        try:
            items = _read_ticker_file(source)
            label = "default nifty_500.csv" if is_default else os.path.basename(source)
            print(f"[RS+ROC IND] Loaded {len(items)} symbols from {label}")
            return items
        except Exception as e:
            print(f"[RS+ROC IND] Could not read {source}: {e} — trying NSE live URL")

    print("[RS+ROC IND] WARNING: falling back to live NSE URL (internet required)")
    try:
        url = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
        df  = pd.read_csv(url)
        col_map = {c.lower(): c for c in df.columns}
        sym_col = col_map.get('symbol')
        sec_col = col_map.get('industry', col_map.get('sector'))
        if not sym_col:
            raise ValueError("No Symbol column in NSE CSV")
        return [{'Symbol': str(row[sym_col]).strip().upper(),
                 'Industry': str(row[sec_col]).strip() if sec_col else 'Unknown'}
                for _, row in df.iterrows() if str(row[sym_col]).strip()]
    except Exception as e:
        print(f"[RS+ROC IND] NSE fallback also failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Schema normalisation (Memory #3)
# ---------------------------------------------------------------------------

def _normalize_stock(s):
    s.setdefault('rs_h',          [])
    s.setdefault('rs_up',         False)
    s.setdefault('rank_diff',     0)
    s.setdefault('rank_status',   'stable')
    s.setdefault('sector',        '')
    s.setdefault('roc_3m',        0.0)
    s.setdefault('roc_6m',        0.0)
    s.setdefault('rs_percentile', 0)
    return s


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
# Benchmark fetch
# ---------------------------------------------------------------------------

def _fetch_benchmark():
    """Try Nifty 500 first, fall back to Nifty 50. Returns (close_series, label)."""
    for ticker, label in (PRIMARY_BENCHMARK, FALLBACK_BENCHMARK):
        data, _ = ind_cache.get_price_history_bulk([ticker], interval='1d', lookback_days=500)
        df = data.get(ticker)
        if df is not None and not df.empty and len(df) >= 200:
            return df['Close'].dropna(), f"{label} ({ticker})"
    return None, None


# ---------------------------------------------------------------------------
# RS formula (Memory #1)
# ---------------------------------------------------------------------------

def _compute_rs(stock_close, bench_close):
    """
    Ratio-of-relatives RS — consistent with every other screener in this
    codebase. Unlike subtraction (stock_ret - bench_ret), this stays
    monotonic in stock_return even when the benchmark is negative.

    Old code: rs = stock_ret_1y - bench_ret_1y   ← incorrect
    Correct:  rs = (1+stock_ret)/(1+bench_ret) - 1
    """
    stock_ret = (stock_close.iloc[-1] / stock_close.iloc[0]) - 1
    bench_ret = (bench_close.iloc[-1] / bench_close.iloc[0]) - 1
    if (1 + bench_ret) == 0:
        return None
    return ((1 + stock_ret) / (1 + bench_ret)) - 1


# ---------------------------------------------------------------------------
# Background scan
# ---------------------------------------------------------------------------

def run_scan(source_path, source_name):
    _set_progress(active=True, processed=0, total=0,
                  current_symbol="", stage="loading_symbols", error=None)

    stock_list = load_nifty500_symbols()
    if not stock_list:
        _set_progress(active=False, stage="error",
                      error="Could not load Nifty 500 symbols. Check nifty_500.csv in data/ or internet connection.")
        return

    # Build .NS-suffixed yfinance symbols (guard against double-suffix)
    yf_symbols = [
        s['Symbol'] if s['Symbol'].endswith('.NS') else f"{s['Symbol']}.NS"
        for s in stock_list
    ]
    sym_to_item = {yf: item for yf, item in zip(yf_symbols, stock_list)}

    _set_progress(stage="fetching_benchmark", total=len(yf_symbols))
    bench_close, benchmark_label = _fetch_benchmark()
    if bench_close is None:
        _set_progress(active=False, stage="error",
                      error="Could not fetch benchmark. Scan aborted.")
        return

    # Bulk-fetch all symbols through the shared IND cache (Memory #8 + #13)
    def _fetch_progress(i, total, sym):
        _set_progress(stage="fetching_prices", processed=i, total=total, current_symbol=sym)

    price_data, fetch_report = ind_cache.get_price_history_bulk(
        yf_symbols, interval='1d', lookback_days=500, progress_callback=_fetch_progress
    )
    price_data_asof = latest_bar_date(price_data)

    # Cache source log (Memory #13)
    _n, _ch, _yf, _fl = len(yf_symbols), fetch_report['from_cache'], fetch_report['fetched'], fetch_report['failed']
    sep = "=" * 55
    print(f"\n{sep}")
    print(f"  [CACHE] RS+ROC IND {source_name} — price data source summary")
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

    # Load previous data for rank delta + RS trend tracking
    old_ranks, existing_rs_h = {}, {}
    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON) as f:
                old_cache = json.load(f).get('stocks', [])
                old_ranks     = {s['symbol']: s['rank']       for s in old_cache}
                existing_rs_h = {s['symbol']: s.get('rs_h', []) for s in old_cache}
        except (json.JSONDecodeError, OSError):
            pass

    _set_progress(stage="scanning", processed=0, total=len(yf_symbols), current_symbol="")

    raw_results = []
    for i, yf_sym in enumerate(yf_symbols):
        _set_progress(processed=i, current_symbol=yf_sym)
        item = sym_to_item[yf_sym]
        df   = price_data.get(yf_sym)
        if df is None or df.empty:
            continue
        try:
            close = df['Close'].dropna()
            if len(close) < 200:
                continue

            # Align benchmark to this stock's trading dates
            bench_aligned = bench_close.reindex(close.index).ffill()
            if bench_aligned.isna().any():
                continue

            current_price = float(close.iloc[-1])
            ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
            if current_price <= ema200:
                continue

            rs_val = _compute_rs(close, bench_aligned)
            if rs_val is None:
                continue

            # ROC 3M = 63 sessions, ROC 6M = 126 sessions
            roc_3m = ((current_price / float(close.iloc[-63]))  - 1) * 100 if len(close) >= 63  else None
            roc_6m = ((current_price / float(close.iloc[-126])) - 1) * 100 if len(close) >= 126 else None

            if roc_3m is None:
                continue

            raw_results.append({
                "symbol":  item['Symbol'],           # clean symbol without .NS
                "sector":  item['Industry'],
                "price":   round(current_price, 2),
                "rs_raw":  round(rs_val, 4),
                "roc_3m":  round(roc_3m, 2),
                "roc_6m":  round(roc_6m, 2) if roc_6m is not None else None,
            })
        except Exception as e:
            print(f"  [RS+ROC IND] Error {yf_sym}: {e}")

    _set_progress(processed=len(yf_symbols), current_symbol="")

    stocks     = []
    leaders_90 = []
    if raw_results:
        df = pd.DataFrame(raw_results)
        df['rs_raw'] = pd.to_numeric(df['rs_raw'], errors='coerce')
        df = df.dropna(subset=['rs_raw'])
        if not df.empty:
            df['rs_percentile'] = df['rs_raw'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)

            # Sort: RS percentile DESC (primary), ROC 3M DESC (tiebreak)
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
            leaders_90 = [s['symbol'] for s in stocks if s.get('rs_percentile', 0) >= 90]

    last_time      = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    scanned_count  = len(yf_symbols)
    excluded_count = scanned_count - len(stocks)

    payload = {
        'stocks':               stocks,
        'time':                 last_time,
        'source':               source_name,
        'benchmark_label':      benchmark_label,
        'scanned_count':        scanned_count,
        'excluded_count':       excluded_count,
        'stale_symbols_count':  len(_fl),
        'stale_symbols_sample': [s.replace('.NS', '') for s in _fl[:10]],
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
        "source":          source_name,
        "count":           len(stocks),
        "leaders_90":      leaders_90,
        "benchmark_label": benchmark_label,
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

@rs_roc_bp.route("/rs-roc-momentum", methods=["GET", "POST"])
def rs_roc_momentum_process():
    if request.method == "POST":
        if _get_progress()["active"]:
            return redirect(url_for('rs_roc.rs_roc_momentum_process', scanning=1))

        file        = request.files.get('file')
        use_default = request.form.get('use_default') == '1'

        if file and file.filename != '':
            from werkzeug.utils import secure_filename
            filename      = secure_filename(file.filename)
            ext           = os.path.splitext(filename)[1].lower()
            save_filename = f"uploaded_ind_roc_tickers{ext}"
            filepath      = os.path.join(UPLOAD_FOLDER, save_filename)
            file.save(filepath)
            with open(LAST_CSV_CONFIG, 'w') as f:
                json.dump({'path': filepath, 'name': filename}, f)
            source_path, source_name = filepath, filename
        elif use_default:
            source_path, source_name = DEFAULT_IND_CSV, DEFAULT_IND_LABEL
        else:
            source_path, source_name, _ = _get_active_source()

        if not source_path or not os.path.exists(source_path):
            err = f"Default file not found: {DEFAULT_IND_CSV}. Place nifty_500.csv in data/ or upload a file."
            _set_progress(active=False, stage="error", error=err)
            return redirect(url_for('rs_roc.rs_roc_momentum_process'))

        thread = threading.Thread(target=run_scan, args=(source_path, source_name), daemon=True)
        thread.start()
        return redirect(url_for('rs_roc.rs_roc_momentum_process', scanning=1))

    # --- GET ---
    stocks, last_time = [], None
    benchmark_label = source_name = None
    excluded_count = scanned_count = stale_symbols_count = 0
    stale_symbols_sample = []
    price_data_asof = cache_hits = yf_fetches = None

    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON) as f:
                cache = json.load(f)
                stocks               = [_normalize_stock(s) for s in cache.get('stocks', [])]
                last_time            = cache.get('time')
                benchmark_label      = cache.get('benchmark_label')
                source_name          = cache.get('source')
                excluded_count       = cache.get('excluded_count', 0)
                scanned_count        = cache.get('scanned_count', 0)
                stale_symbols_count  = cache.get('stale_symbols_count', 0)
                stale_symbols_sample = cache.get('stale_symbols_sample', [])
                price_data_asof      = cache.get('price_data_asof')
                cache_hits           = cache.get('cache_hits', 0)
                yf_fetches           = cache.get('yf_fetches', 0)
        except (json.JSONDecodeError, OSError):
            pass

    history    = _load_history()
    progress   = _get_progress()
    is_scanning = progress["active"] or request.args.get('scanning') == '1'
    _, active_file, is_default_source = _get_active_source()

    return render_template(
        "rs_roc_ind_momentum.html",
        stocks=stocks,
        last_time=last_time,
        source_name=source_name,
        benchmark_label=benchmark_label,
        excluded_count=excluded_count,
        scanned_count=scanned_count,
        stale_symbols_count=stale_symbols_count,
        stale_symbols_sample=stale_symbols_sample,
        price_data_asof=price_data_asof,
        cache_hits=cache_hits,
        yf_fetches=yf_fetches,
        history=history,
        active_file=active_file,
        is_default_source=is_default_source,
        default_label=DEFAULT_IND_LABEL,
        is_scanning=is_scanning,
        scan_error=progress.get("error"),
        restored=request.args.get('restored')     == '1',
        restore_error=request.args.get('restore_error') == '1',
    )


@rs_roc_bp.route("/rs-roc-momentum/progress")
def rs_roc_ind_progress():
    return jsonify(_get_progress())


@rs_roc_bp.route("/rs-roc-momentum/clear-source", methods=["POST"])
def rs_roc_ind_clear_source():
    try:
        if os.path.exists(LAST_CSV_CONFIG):
            os.remove(LAST_CSV_CONFIG)
    except OSError:
        pass
    return redirect(url_for('rs_roc.rs_roc_momentum_process'))


@rs_roc_bp.route("/restore-rs-roc-ind/<snapshot_file>", methods=["POST"])
def restore_rs_roc_ind_snapshot(snapshot_file):
    """POST-only restore — prevents stray click/prefetch overwriting results (Memory #11)."""
    safe_name     = os.path.basename(snapshot_file)
    snapshot_path = os.path.join(SNAPSHOT_DIR, safe_name)
    valid = safe_name.startswith('snapshot_') and safe_name.endswith('.json') and os.path.exists(snapshot_path)
    if not valid:
        return redirect(url_for('rs_roc.rs_roc_momentum_process', restore_error=1))
    try:
        with open(snapshot_path) as f:
            payload = json.load(f)
        with open(RESULTS_JSON, 'w') as f:
            json.dump(payload, f)
    except (json.JSONDecodeError, OSError):
        return redirect(url_for('rs_roc.rs_roc_momentum_process', restore_error=1))
    return redirect(url_for('rs_roc.rs_roc_momentum_process', restored=1))


@rs_roc_bp.route("/export-rs-roc")
def export_rs_roc():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON) as f:
            stocks = json.load(f).get('stocks', [])
        if stocks:
            df        = pd.DataFrame(stocks)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_path = os.path.join(UPLOAD_FOLDER, 'temp_export.csv')
            df.to_csv(temp_path, index=False)
            return send_file(temp_path, as_attachment=True,
                             download_name=f"IND_RS_ROC_Screener_{timestamp}.csv")
    return "No scan data available to export", 404