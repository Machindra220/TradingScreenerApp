import os
import json
import uuid
import threading
import pandas as pd
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, session, jsonify, redirect, url_for

from app.services.market_data_cache import us_cache, latest_bar_date  # US market SQLite cache

volar_us_adaptive_bp = Blueprint('volar_us_adaptive_bp', __name__)

# Anchor all paths to __file__ so they resolve correctly regardless of where
# Flask was started — os.getcwd() at module-import time is fragile because the
# Werkzeug reloader can run in a different working directory than the main process.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER = os.path.join(_PROJECT_ROOT, 'uploads', 'volar_us_adaptive')
SNAPSHOT_DIR  = os.path.join(UPLOAD_FOLDER, 'snapshots')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SNAPSHOT_DIR,  exist_ok=True)

RESULTS_JSON    = os.path.join(UPLOAD_FOLDER, 'volar_results_us_adaptive.json')
HISTORY_JSON    = os.path.join(UPLOAD_FOLDER, 'scan_history_us_adaptive.json')
LAST_CSV_CONFIG = os.path.join(UPLOAD_FOLDER, 'last_csv_path.json')

DEFAULT_US_CSV   = os.path.join(_PROJECT_ROOT, 'data', 'sp500.csv')
DEFAULT_US_LABEL = "S&P 500 Default (sp500.csv)"

HISTORY_LIMIT = 5
LB_3M = 55     # ~3 months
LB_6M = 122    # ~6 months

US_INDEX = ("^GSPC", "S&P 500")

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
# Source-selection helper (Memory #9)
# ---------------------------------------------------------------------------

def _get_active_source():
    """Returns (filepath, display_name, is_default)."""
    if os.path.exists(LAST_CSV_CONFIG):
        try:
            with open(LAST_CSV_CONFIG, 'r') as f:
                cfg = json.load(f)
            fp   = cfg.get('path', '')
            name = cfg.get('name', os.path.basename(fp))
            if fp and os.path.exists(fp):
                return fp, name, False
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_US_CSV, DEFAULT_US_LABEL, True


# ---------------------------------------------------------------------------
# Calculations
# ---------------------------------------------------------------------------

def compute_volar(prices):
    if len(prices) < 2:
        return None
    returns = prices.pct_change().dropna()
    std = returns.std()
    if std == 0 or pd.isna(std):
        return None
    total_ret = (prices.iloc[-1] / prices.iloc[0]) - 1
    return round(total_ret / std, 2)


def make_bar_heights(values, max_height=14, min_height=2):
    """Heights (px) for the growing-bars RS trend indicator (Memory #6)."""
    if not values:
        return []
    return [round(min_height + (max(0, min(100, v)) / 100) * (max_height - min_height), 1) for v in values]


def _normalize_stock(s):
    """
    Backfill fields the template expects that an older cached scan may not have
    written. Prevents UndefinedError crashes on comparison operators in Jinja
    when the schema has changed since the last scan. (Memory #3)
    """
    s.setdefault('rs3_h', [])
    s.setdefault('rs6_h', [])
    s.setdefault('vol_h', [])
    s.setdefault('perf_h', [])
    s.setdefault('rank_h', [])
    s.setdefault('rs3_up', False)
    s.setdefault('rs6_up', False)
    s.setdefault('rs3_bars', [])
    s.setdefault('rs6_bars', [])
    s.setdefault('rank_delta', None)
    s.setdefault('rank_status', 'stable')
    s.setdefault('rank_diff', 0)
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
            try:
                os.remove(os.path.join(SNAPSHOT_DIR, fname))
            except OSError:
                pass


def _read_ticker_file(filepath):
    """Read CSV or XLSX, accept any column named Symbol/Ticker (case-insensitive)."""
    ext = os.path.splitext(filepath)[1].lower()
    try:
        df = pd.read_excel(filepath) if ext in ('.xlsx', '.xls') else pd.read_csv(filepath)
    except Exception as e:
        raise ValueError(f"Could not read file: {e}")
    col_map = {c.lower(): c for c in df.columns}
    found = next((col_map[k] for k in ('symbol', 'ticker', 'symbols', 'tickers') if k in col_map), None)
    if found is None:
        raise ValueError(f"File must have a Symbol or Ticker column. Found: {', '.join(df.columns.tolist())}")
    return [str(s).strip().upper() for s in df[found].dropna().unique() if str(s).strip()]


# ---------------------------------------------------------------------------
# Background scan
# ---------------------------------------------------------------------------

def run_scan(symbols, source_name):
    _set_progress(active=True, processed=0, total=len(symbols),
                  current_symbol="", stage="fetching_index", error=None)

    # Fetch the S&P 500 benchmark ONCE via the US cache (Memory #8 — never per-stock)
    idx_data, _ = us_cache.get_price_history_bulk([US_INDEX[0]], interval='1d', lookback_days=LB_6M + 220)
    idx_df = idx_data.get(US_INDEX[0])
    if idx_df is None or idx_df.empty or len(idx_df) < LB_6M:
        _set_progress(active=False, stage="error",
                      error=f"Could not fetch benchmark {US_INDEX[0]}. Scan aborted.")
        return

    idx_close = idx_df['Close'].dropna()

    # Bulk-fetch all symbols through the shared US cache. Progress wired through
    # the callback so the button fills during the network phase, not after it.
    # (Memory #8 — progress must go through the cache call itself)
    def _fetch_progress(i, total, sym):
        _set_progress(stage="fetching_prices", processed=i, total=total, current_symbol=sym)

    price_data, fetch_report = us_cache.get_price_history_bulk(
        symbols, interval='1d', lookback_days=LB_6M + 220, progress_callback=_fetch_progress
    )
    price_data_asof = latest_bar_date(price_data)

    # ── Cache source log ─────────────────────────────────────────────────────
    # Printed to the terminal after every bulk fetch so you can verify whether
    # symbols are being served from the SQLite cache or re-fetched from yfinance.
    # "from_cache" = data was fresh in market_data_us.db, no network call made.
    # "fetched"    = data was stale/missing, yfinance was hit and DB was updated.
    # "failed"     = yfinance returned nothing after retries (rate-limit / bad symbol).
    total_requested = len(symbols)
    cache_hits   = fetch_report['from_cache']
    yf_fetches   = fetch_report['fetched']
    failed_syms  = fetch_report['failed']
    cache_pct    = round(cache_hits / total_requested * 100) if total_requested else 0
    yf_pct       = round(yf_fetches  / total_requested * 100) if total_requested else 0

    print(f"\n{'='*55}")
    print(f"  [CACHE] US Adaptive scan — price data source summary")
    print(f"{'='*55}")
    print(f"  Total requested : {total_requested}")
    print(f"  From DB cache   : {cache_hits} ({cache_pct}%)  ← no yfinance call")
    print(f"  Fetched fresh   : {yf_fetches}  ({yf_pct}%)  ← yfinance hit + DB updated")
    print(f"  Failed (429/err): {len(failed_syms)}")
    if failed_syms:
        print(f"  Failed symbols  : {', '.join(failed_syms[:10])}"
              + (f" ...+{len(failed_syms)-10} more" if len(failed_syms) > 10 else ""))
    print(f"  Price data as of: {price_data_asof}")
    print(f"{'='*55}\n")

    # Also store the source breakdown in the scan payload so the UI can show it
    _set_progress(
        cache_hits=cache_hits, yf_fetches=yf_fetches,
        failed_count=len(failed_syms), total_requested=total_requested
    )

    # Load previous scan's ranks for rank_delta calculation
    old_ranks = {}
    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON, 'r') as f:
                old_ranks = {s['symbol']: s['rank'] for s in json.load(f).get('stocks', [])}
        except (json.JSONDecodeError, OSError):
            pass

    # Load previous RS history for trend tracking
    existing_history = {}
    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON, 'r') as f:
                old_cache = json.load(f).get('stocks', [])
                existing_history = {s['symbol']: {
                    'rs3_h': s.get('rs3_h', []), 'rs6_h': s.get('rs6_h', []),
                    'vol_h': s.get('vol_h', []), 'perf_h': s.get('perf_h', []),
                    'rank_h': s.get('rank_h', []),
                } for s in old_cache}
        except (json.JSONDecodeError, OSError):
            pass

    _set_progress(stage="scanning", processed=0, total=len(symbols), current_symbol="")

    raw_results = []
    for i, sym in enumerate(symbols):
        _set_progress(processed=i, current_symbol=sym)
        try:
            stock_df = price_data.get(sym)
            if stock_df is None or stock_df.empty or len(stock_df) < LB_6M:
                continue

            close = stock_df['Close'].dropna()
            if len(close) < LB_6M:
                continue
            curr = float(close.iloc[-1])

            # Align index to this stock's trading dates
            idx_aligned = idx_close.reindex(close.index).ffill()
            if idx_aligned.iloc[-LB_6M:].isna().any():
                continue

            ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
            if pd.isna(ema200) or curr <= ema200:
                continue

            idx_curr = float(idx_aligned.iloc[-1])

            # 3M calculations (Memory #1 — ratio-of-relatives RS, not naive division)
            perf_3m     = (curr / close.iloc[-LB_3M]) - 1
            idx_perf_3m = (idx_curr / float(idx_aligned.iloc[-LB_3M])) - 1
            rs_3m = round((1 + perf_3m) / (1 + idx_perf_3m) - 1, 4) if (1 + idx_perf_3m) != 0 else None
            volar_3m = compute_volar(close.iloc[-LB_3M:])

            # 6M calculations
            perf_6m     = (curr / close.iloc[-LB_6M]) - 1
            idx_perf_6m = (idx_curr / float(idx_aligned.iloc[-LB_6M])) - 1
            rs_6m = round((1 + perf_6m) / (1 + idx_perf_6m) - 1, 4) if (1 + idx_perf_6m) != 0 else None
            volar_6m = compute_volar(close.iloc[-LB_6M:])

            # Drop any stock where a required field is None (Memory #1 — is not None, never falsy)
            if any(v is None for v in [rs_3m, rs_6m, volar_3m, volar_6m]):
                continue

            raw_results.append({
                "symbol":   sym,
                "price":    round(curr, 2),
                "rs_3m":    rs_3m,   "volar_3m": volar_3m, "perf_3m": round(perf_3m * 100, 2),
                "rs_6m":    rs_6m,   "volar_6m": volar_6m, "perf_6m": round(perf_6m * 100, 2),
            })
        except Exception as e:
            print(f"  ERROR {sym}: {e}")
    _set_progress(processed=len(symbols), current_symbol="")

    stocks = []
    leaders_90 = []
    if raw_results:
        df = pd.DataFrame(raw_results)
        df['rs3_pct'] = df['rs_3m'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)
        df['rs6_pct'] = df['rs_6m'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)
        df.sort_values('rs_3m', ascending=False, inplace=True)
        df.reset_index(drop=True, inplace=True)
        df['rank'] = df.index + 1

        def inject_history(row):
            h = existing_history.get(row['symbol'], {'rs3_h': [], 'rs6_h': [], 'vol_h': [], 'perf_h': [], 'rank_h': []})
            row['rs3_h']  = (h['rs3_h']  + [row['rs3_pct']])[-5:]
            row['rs6_h']  = (h['rs6_h']  + [row['rs6_pct']])[-5:]
            row['vol_h']  = (h['vol_h']  + [row['volar_3m']])[-5:]
            row['perf_h'] = (h['perf_h'] + [row['perf_3m']])[-5:]
            row['rank_h'] = (h.get('rank_h', []) + [row['rank']])[-5:]
            row['rs3_up'] = len(row['rs3_h']) > 1 and all(x < y for x, y in zip(row['rs3_h'], row['rs3_h'][1:]))
            row['rs6_up'] = len(row['rs6_h']) > 1 and all(x < y for x, y in zip(row['rs6_h'], row['rs6_h'][1:]))
            # Single-scan rank delta (Memory design-system #9)
            row['rank_delta'] = (row['rank_h'][-2] - row['rank_h'][-1]) if len(row['rank_h']) > 1 else None
            # Rank status for filter chip (Memory #9 — new entrant / big mover)
            prev_rank = old_ranks.get(row['symbol'])
            if prev_rank is None:
                row['rank_status'], row['rank_diff'] = 'new', 0
            else:
                diff = prev_rank - row['rank']
                row['rank_diff']   = diff
                row['rank_status'] = 'up' if diff > 0 else ('down' if diff < 0 else 'stable')
            return row

        df = df.apply(inject_history, axis=1)
        stocks = df.to_dict(orient='records')

        for stock in stocks:
            stock['rs3_bars'] = make_bar_heights(stock['rs3_h'])
            stock['rs6_bars'] = make_bar_heights(stock['rs6_h'])
            if stock.get('rs3_pct', 0) >= 90:
                leaders_90.append(stock['symbol'])

    last_processed_time = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    scanned_count  = len(symbols)
    excluded_count = scanned_count - len(stocks)

    payload = {
        'stocks':               stocks,
        'time':                 last_processed_time,
        'source':               source_name,
        'benchmark_label':      f"{US_INDEX[1]} ({US_INDEX[0]})",
        'scanned_count':        scanned_count,
        'excluded_count':       excluded_count,
        'stale_symbols_count':  len(fetch_report['failed']),
        'stale_symbols_sample': fetch_report['failed'][:10],
        'price_data_asof':      price_data_asof,
        'cache_hits':           cache_hits,
        'yf_fetches':           yf_fetches,
    }

    # Snapshot BEFORE overwriting active results (Memory #5 — always restorable)
    snapshot_filename = f"snapshot_{uuid.uuid4().hex}.json"
    with open(os.path.join(SNAPSHOT_DIR, snapshot_filename), 'w') as f:
        json.dump(payload, f)

    with open(RESULTS_JSON, 'w') as f:
        json.dump(payload, f)

    history = _load_history()
    history.insert(0, {
        "time":            last_processed_time,
        "source":          source_name,
        "count":           len(stocks),
        "leaders_90":      leaders_90,
        "benchmark_label": f"{US_INDEX[1]} ({US_INDEX[0]})",
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

@volar_us_adaptive_bp.route("/volar-us-adaptive", methods=["GET", "POST"])
def volar_us_process():
    if request.method == "POST":
        if _get_progress()["active"]:
            return redirect(url_for('volar_us_adaptive_bp.volar_us_process', scanning=1))

        file = request.files.get('file')
        use_default = request.form.get('use_default') == '1'

        if file and file.filename != '':
            from werkzeug.utils import secure_filename
            filename      = secure_filename(file.filename)
            ext           = os.path.splitext(filename)[1].lower()
            save_filename = f"uploaded_us_adaptive_tickers{ext}"
            filepath      = os.path.join(UPLOAD_FOLDER, save_filename)
            file.save(filepath)
            with open(LAST_CSV_CONFIG, 'w') as f:
                json.dump({'path': filepath, 'name': filename}, f)
            source_name = filename
            source_path = filepath
        elif use_default:
            source_path, source_name = DEFAULT_US_CSV, DEFAULT_US_LABEL
        else:
            source_path, source_name, _ = _get_active_source()

        if not source_path or not os.path.exists(source_path):
            err = f"Default file not found: {DEFAULT_US_CSV}. Place sp500.csv in data/ or upload a custom file."
            _set_progress(active=False, stage="error", error=err)
            return redirect(url_for('volar_us_adaptive_bp.volar_us_process'))

        try:
            symbols = _read_ticker_file(source_path)
        except ValueError as e:
            _set_progress(active=False, stage="error", error=str(e))
            return redirect(url_for('volar_us_adaptive_bp.volar_us_process'))

        thread = threading.Thread(target=run_scan, args=(symbols, source_name), daemon=True)
        thread.start()
        return redirect(url_for('volar_us_adaptive_bp.volar_us_process', scanning=1))

    # --- GET ---
    stocks, last_processed_time, source_name = [], None, "None"
    benchmark_label, excluded_count, scanned_count = None, 0, 0
    stale_symbols_count, stale_symbols_sample, price_data_asof = 0, [], None
    cache_hits, yf_fetches = 0, 0

    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON, 'r') as f:
                cache = json.load(f)
                stocks              = [_normalize_stock(s) for s in cache.get('stocks', [])]
                last_processed_time = cache.get('time')
                source_name         = cache.get('source', 'None')
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

    history = _load_history()
    progress = _get_progress()
    is_scanning = progress["active"] or request.args.get('scanning') == '1'
    _, active_file, is_default_source = _get_active_source()

    return render_template(
        "stage2_adaptive_volar_us_scr.html",
        stocks=stocks,
        last_processed_time=last_processed_time,
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
        default_label=DEFAULT_US_LABEL,
        is_scanning=is_scanning,
        scan_error=progress.get("error"),
        restored=request.args.get('restored') == '1',
        restore_error=request.args.get('restore_error') == '1',
    )


@volar_us_adaptive_bp.route("/volar-us-adaptive/progress")
def volar_us_adaptive_progress():
    return jsonify(_get_progress())


@volar_us_adaptive_bp.route("/volar-us-adaptive/clear-source", methods=["POST"])
def volar_us_adaptive_clear_source():
    try:
        if os.path.exists(LAST_CSV_CONFIG):
            os.remove(LAST_CSV_CONFIG)
    except OSError:
        pass
    return redirect(url_for('volar_us_adaptive_bp.volar_us_process'))


@volar_us_adaptive_bp.route("/volar-us-adaptive/restore/<snapshot_file>", methods=["POST"])
def volar_us_adaptive_restore(snapshot_file):
    safe_name = os.path.basename(snapshot_file)
    snapshot_path = os.path.join(SNAPSHOT_DIR, safe_name)
    valid = safe_name.startswith('snapshot_') and safe_name.endswith('.json') and os.path.exists(snapshot_path)
    if not valid:
        return redirect(url_for('volar_us_adaptive_bp.volar_us_process', restore_error=1))
    try:
        with open(snapshot_path, 'r') as f:
            payload = json.load(f)
        with open(RESULTS_JSON, 'w') as f:
            json.dump(payload, f)
    except (json.JSONDecodeError, OSError):
        return redirect(url_for('volar_us_adaptive_bp.volar_us_process', restore_error=1))
    return redirect(url_for('volar_us_adaptive_bp.volar_us_process', restored=1))