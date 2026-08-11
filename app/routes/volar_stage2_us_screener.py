import os
import json
import uuid
import threading
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Blueprint, render_template, request, send_file, redirect, url_for, jsonify

from app.services.market_data_cache import us_cache, latest_bar_date  # US market DB instance

volar_us_bp = Blueprint("volar_us", __name__)

# Anchor all paths to __file__ (app/routes/volar_stage2_us_screener.py)
_PROJECT_ROOT    = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER    = os.path.join(_PROJECT_ROOT, 'uploads', 'volar_us')
RESULTS_JSON     = os.path.join(UPLOAD_FOLDER, 'last_volar_us_results.json')
LAST_CSV_CONFIG  = os.path.join(UPLOAD_FOLDER, 'last_csv_path.json')
HISTORY_CACHE_DIR = os.path.join(UPLOAD_FOLDER, 'history_cache')
os.makedirs(UPLOAD_FOLDER,     exist_ok=True)
os.makedirs(HISTORY_CACHE_DIR, exist_ok=True)

HISTORY_LIMIT = 5
US_INDEX = ("^GSPC", "S&P 500")
DEFAULT_US_CSV   = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'sp500.csv'))
DEFAULT_US_LABEL = "S&P 500 Default (sp500.csv)"


def _get_active_source_us():
    """
    Returns (filepath, display_name, is_default).
    Priority: uploaded file saved in LAST_CSV_CONFIG → built-in default CSV.
    Never raises — callers check os.path.exists(filepath) themselves.
    """
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
# Calculations
# ---------------------------------------------------------------------------

def compute_volar(close_series):
    total_return = (close_series.iloc[-1] / close_series.iloc[0]) - 1
    volatility   = close_series.pct_change(fill_method=None).std()
    if volatility is None or pd.isna(volatility) or volatility == 0:
        return None
    return total_return / volatility


def compute_relative_strength(stock_close, index_close):
    """
    Ratio-of-relatives RS — correct even when the index return is negative.

    The old formula (stock_return / index_return) inverts sign when the index
    is down: a stock that falls LESS than the index (a relative winner) scores
    WORSE than one that falls MORE, reversing the entire ranking in a downturn.
    (1+stock)/(1+index) - 1 stays monotonic in stock_return regardless of the
    index's sign, so "higher RS" reliably means "beat the benchmark."
    """
    stock_return = (stock_close.iloc[-1] / stock_close.iloc[0]) - 1
    index_return = (index_close.iloc[-1] / index_close.iloc[0]) - 1
    if index_return <= -1:
        return None
    return ((1 + stock_return) / (1 + index_return)) - 1


def is_volar_us_candidate(symbol, index_df, stock_df):
    """
    Evaluates a single US symbol against Stage-2 criteria using pre-fetched
    DataFrames from the shared cache — NO network I/O inside this function.
    """
    try:
        if stock_df is None or stock_df.empty or index_df is None or index_df.empty or len(stock_df) < 200:
            return None

        close = stock_df["Close"]
        high_52w      = close[-252:].max()
        current_price = close.iloc[-1]
        pullback      = (high_52w - current_price) / high_52w

        # adjust=False for consistency with the IND screener's EMA200, so the
        # same borderline stock doesn't pass on one page and fail on the other.
        ema_200 = close.ewm(span=200, adjust=False).mean().iloc[-1]

        volar_val   = compute_volar(close)
        rs_val      = compute_relative_strength(close, index_df["Close"])
        performance = (close.iloc[-1] / close.iloc[0]) - 1

        # is not None check — never `if volar_val and rs_val`, which treats
        # 0.0 (a legitimate value) as falsy and silently drops the stock.
        if pullback < 0.3 and current_price > ema_200 and volar_val is not None and rs_val is not None:
            return {
                "symbol":            symbol,
                "symbol_clean":      symbol,      # US tickers have no suffix to strip
                "price":             round(current_price,  2),
                "pullback_pct":      round(pullback * 100,  2),
                "ema_200":           round(ema_200,         2),
                "volar":             round(volar_val,       2),
                "relative_strength": round(rs_val,          4),
                "performance":       round(performance * 100, 2),
            }
    except Exception as e:
        print(f"  Error screening {symbol}: {e}")
    return None


def _normalize_stock(s):
    """
    Backfill any fields the template expects that an older cached scan might
    not have written, so a stale results file never crashes the template on
    a comparison like stock.rank_delta > 0 against a genuinely missing key.
    """
    s.setdefault('symbol_clean', s.get('symbol', ''))
    s.setdefault('rs_h',        [])
    s.setdefault('vol_h',       [])
    s.setdefault('perf_h',      [])
    s.setdefault('rs_bars',     [])
    s.setdefault('rs_up',       False)
    s.setdefault('rank_diff',   0)
    s.setdefault('rank_status', 'stable')
    s.setdefault('rank_delta',  None)
    s.setdefault('is_consistent', False)
    return s


def make_bar_heights(values, max_height=14, min_height=2):
    """Heights (px) for the growing-bars RS trend indicator, scaled against
    0-100 percentile range (not per-row min/max) so bars compare across rows."""
    if not values:
        return []
    return [round(min_height + (max(0, min(100, v)) / 100) * (max_height - min_height), 1) for v in values]


def _prune_snapshots(keep_ids):
    keep_files = {f"{sid}.json" for sid in keep_ids}
    for fname in os.listdir(HISTORY_CACHE_DIR):
        if fname == 'meta_history.json':
            continue
        if fname not in keep_files:
            try: os.remove(os.path.join(HISTORY_CACHE_DIR, fname))
            except OSError: pass


def _load_meta_history():
    meta_path = os.path.join(HISTORY_CACHE_DIR, 'meta_history.json')
    if os.path.exists(meta_path):
        try:
            with open(meta_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


# ---------------------------------------------------------------------------
# Background scan
# ---------------------------------------------------------------------------

def _read_ticker_file(filepath):
    """
    Read a CSV or XLSX ticker file and return a list of symbol strings.
    Accepts any column named Symbol, symbol, Ticker, ticker, or SYMBOL.
    Raises ValueError with a user-friendly message if nothing can be found.
    """
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext in ('.xlsx', '.xls'):
            df = pd.read_excel(filepath)
        else:
            df = pd.read_csv(filepath)
    except Exception as e:
        raise ValueError(f"Could not read file: {e}")

    # Case-insensitive column search
    col_map = {c.lower(): c for c in df.columns}
    found = None
    for candidate in ('symbol', 'ticker', 'symbols', 'tickers'):
        if candidate in col_map:
            found = col_map[candidate]
            break
    if found is None:
        raise ValueError(
            f"File must have a column named Symbol, Ticker, Symbols, or Tickers. "
            f"Found columns: {', '.join(df.columns.tolist())}"
        )
    return [str(s).strip().upper() for s in df[found].dropna().unique() if str(s).strip()]


def run_scan_us(filepath, source_name):
    _set_progress(active=True, processed=0, total=0, current_symbol="",
                  stage="reading_csv", error=None)

    try:
        symbols = _read_ticker_file(filepath)
    except ValueError as e:
        _set_progress(active=False, stage="error", error=str(e))
        return

    if not symbols:
        _set_progress(active=False, stage="error", error="No valid symbols found in the file.")
        return
    _set_progress(stage="fetching_index", total=len(symbols))

    # Fetch the benchmark once via the US cache — shared with subsequent
    # per-stock reads so the index data also benefits from caching.
    idx_data, _ = us_cache.get_price_history_bulk([US_INDEX[0]], interval='1d', lookback_days=500)
    index_df = idx_data.get(US_INDEX[0])
    if index_df is None or index_df.empty or len(index_df) < 200:
        _set_progress(active=False, stage="error",
                      error=f"Could not fetch benchmark {US_INDEX[0]}. Scan aborted.")
        return

    # Bulk-fetch all symbols through the US cache — progress wired through
    # the callback so the button moves during the network phase, not after.
    def _fetch_progress(i, total, sym):
        _set_progress(stage="fetching_prices", processed=i, total=total, current_symbol=sym)

    price_data, fetch_report = us_cache.get_price_history_bulk(
        symbols, interval='1d', lookback_days=500, progress_callback=_fetch_progress
    )
    price_data_asof = latest_bar_date(price_data)

    # ── Cache source log ─────────────────────────────────────────────────────
    # Printed to the terminal so you can verify whether each symbol is served
    # from the local SQLite cache (fast, no network) or re-fetched from yfinance.
    _n  = len(symbols)
    _ch = fetch_report["from_cache"]
    _yf = fetch_report["fetched"]
    _fl = fetch_report["failed"]
    print(f"\n{'='*55}")
    print(f"  [CACHE] {source_name} — price data source summary")
    print(f"{'='*55}")
    print(f"  Total symbols   : {_n}")
    print(f"  From DB cache   : {_ch} ({round(_ch/_n*100) if _n else 0}%)  <- no yfinance call")
    print(f"  Fetched fresh   : {_yf}  ({round(_yf/_n*100) if _n else 0}%)  <- yfinance hit + DB updated")
    print(f"  Failed (429/err): {len(_fl)}")
    if _fl:
        extra = f" ...+{len(_fl)-10} more" if len(_fl) > 10 else ""
        print(f"  Failed symbols  : {', '.join(_fl[:10])}{extra}")
    print(f"  Price data as of: {price_data_asof}")
    print(f"{'='*55}\n")

    # Load old ranks so we can compute rank_delta this scan
    old_ranks = {}
    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON, 'r') as f:
                old_ranks = {s.get('symbol_clean', s['symbol']): s['rank']
                             for s in json.load(f).get('stocks', [])}
        except (json.JSONDecodeError, OSError):
            pass

    # Load last-scan RS percentile history for trend tracking
    existing_history = {}
    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON, 'r') as f:
                old_cache = json.load(f).get('stocks', [])
                existing_history = {s['symbol']: {
                    'rs_h':   s.get('rs_h', []),
                    'vol_h':  s.get('vol_h', []),
                    'perf_h': s.get('perf_h', []),
                } for s in old_cache}
        except (json.JSONDecodeError, OSError):
            pass

    _set_progress(stage="scanning", processed=0, total=len(symbols), current_symbol="")

    raw_results = []
    for i, sym in enumerate(symbols):
        _set_progress(processed=i, current_symbol=sym)
        result = is_volar_us_candidate(sym, index_df, price_data.get(sym))
        if result:
            raw_results.append(result)
    _set_progress(processed=len(symbols), current_symbol="")

    stocks = []
    if raw_results:
        df = pd.DataFrame(raw_results)
        df['rs_percentile'] = df['relative_strength'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)
        df.sort_values('relative_strength', ascending=False, inplace=True)
        df.reset_index(drop=True, inplace=True)
        df['rank'] = df.index + 1

        def inject_trends(row):
            sym = row['symbol']
            h   = existing_history.get(sym, {'rs_h': [], 'vol_h': [], 'perf_h': []})
            row['rs_h']   = (h['rs_h']   + [row['rs_percentile']])[-5:]
            row['vol_h']  = (h['vol_h']  + [row['volar']])[-5:]
            row['perf_h'] = (h['perf_h'] + [row['performance']])[-5:]
            row['rs_up']  = len(row['rs_h']) > 1 and all(x < y for x, y in zip(row['rs_h'], row['rs_h'][1:]))
            return row

        df = df.apply(inject_trends, axis=1)
        stocks = df.to_dict(orient="records")

        leaders_90 = []
        for stock in stocks:
            sym = stock['symbol']
            stock['symbol_clean'] = sym

            # Growing-bar heights for the RS trend indicator
            stock['rs_bars'] = make_bar_heights(stock['rs_h'])

            if stock.get('rs_percentile', 0) >= 90:
                leaders_90.append(sym)

            # Rank delta vs. the previous scan
            prev_rank = old_ranks.get(sym)
            if prev_rank is None:
                stock['rank_status'], stock['rank_diff'], stock['rank_delta'] = 'new', 0, None
            else:
                diff = prev_rank - stock['rank']
                stock['rank_diff']   = diff
                stock['rank_delta']  = diff
                stock['rank_status'] = 'up' if diff > 0 else ('down' if diff < 0 else 'stable')

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
        'cache_hits':           fetch_report['from_cache'],
        'yf_fetches':           fetch_report['fetched'],
        'price_data_asof':      price_data_asof,
    }

    # Snapshot BEFORE overwriting active results — so a scan cut short by a
    # 429 or any other error can always be rolled back via Scan History.
    snapshot_id = f"snapshot_{uuid.uuid4().hex}"
    with open(os.path.join(HISTORY_CACHE_DIR, f"{snapshot_id}.json"), 'w') as f:
        json.dump(payload, f)

    with open(RESULTS_JSON, 'w') as f:
        json.dump(payload, f)

    history_meta = _load_meta_history()
    history_meta.insert(0, {
        "snapshot_id":     snapshot_id,
        "time":            last_processed_time,
        "source":          source_name,
        "count":           len(stocks),
        "leaders_90":      leaders_90 if raw_results else [],
        "benchmark_label": f"{US_INDEX[1]} ({US_INDEX[0]})",
        "price_data_asof": price_data_asof,
    })
    history_meta = history_meta[:HISTORY_LIMIT]
    with open(os.path.join(HISTORY_CACHE_DIR, 'meta_history.json'), 'w') as f:
        json.dump(history_meta, f)

    _prune_snapshots([h['snapshot_id'] for h in history_meta])
    _set_progress(active=False, stage="done", current_symbol="")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@volar_us_bp.route("/volar-us", methods=["GET", "POST"])
def volar_us_process():
    compare_mode = request.args.get('compare') == 'true'

    if request.method == "POST":
        if _get_progress()["active"]:
            return redirect(url_for('volar_us.volar_us_process', scanning=1))

        file = request.files.get('file')
        use_default = request.form.get('use_default') == '1'
        filepath, source_name = None, None

        if file and file.filename != '':
            # New file uploaded — save it and pin it as the active source
            from werkzeug.utils import secure_filename
            filename      = secure_filename(file.filename)
            ext           = os.path.splitext(filename)[1].lower()
            save_filename = f"uploaded_us_tickers{ext}"
            filepath      = os.path.join(UPLOAD_FOLDER, save_filename)
            file.save(filepath)
            with open(LAST_CSV_CONFIG, 'w') as f:
                json.dump({'path': filepath, 'name': filename}, f)
            source_name = filename
        elif use_default:
            # User explicitly chose the built-in default — ignore any pinned file
            filepath, source_name = DEFAULT_US_CSV, DEFAULT_US_LABEL
        else:
            # No new upload and no explicit default → use whatever is pinned
            filepath, source_name, _ = _get_active_source_us()

        if not filepath or not os.path.exists(filepath):
            err = (f"Default file not found: {DEFAULT_US_CSV}. "
                   f"Please place sp500.csv in the data/ folder or upload a custom CSV.")
            _set_progress(active=False, stage="error", error=err)
            return redirect(url_for('volar_us.volar_us_process'))

        thread = threading.Thread(target=run_scan_us, args=(filepath, source_name), daemon=True)
        thread.start()
        return redirect(url_for('volar_us.volar_us_process', scanning=1))

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
                source_name         = cache.get('source', 'Cached Scan')
                benchmark_label     = cache.get('benchmark_label')
                excluded_count      = cache.get('excluded_count', 0)
                scanned_count       = cache.get('scanned_count',  0)
                stale_symbols_count = cache.get('stale_symbols_count', 0)
                stale_symbols_sample = cache.get('stale_symbols_sample', [])
                cache_hits          = cache.get('cache_hits', 0)
                yf_fetches          = cache.get('yf_fetches', 0)
                price_data_asof     = cache.get('price_data_asof')
        except (json.JSONDecodeError, OSError):
            pass

    history_meta = _load_meta_history()
    if compare_mode and len(history_meta) >= 3:
        leader_sets = [set(h.get('leaders_90', [])) for h in history_meta[:3]]
        consistent  = set.intersection(*leader_sets) if leader_sets else set()
        for s in stocks:
            if s.get('symbol_clean') in consistent:
                s['is_consistent'] = True

    progress   = _get_progress()
    is_scanning = progress["active"] or request.args.get('scanning') == '1'
    active_filepath, active_file, is_default_source = _get_active_source_us()

    return render_template(
        "stage2_volar_us.html",
        stocks               = stocks,
        last_processed_time  = last_processed_time,
        source_name          = source_name,
        benchmark_label      = benchmark_label,
        excluded_count       = excluded_count,
        scanned_count        = scanned_count,
        stale_symbols_count  = stale_symbols_count,
        stale_symbols_sample = stale_symbols_sample,
        price_data_asof      = price_data_asof,
        cache_hits           = cache_hits,
        yf_fetches           = yf_fetches,
        history              = history_meta,
        compare_mode         = compare_mode,
        is_scanning          = is_scanning,
        scan_error           = progress.get("error"),
        active_file          = active_file,
        is_default_source    = is_default_source,
        default_label        = DEFAULT_US_LABEL,
        restored             = request.args.get('restored')     == '1',
        restore_error        = request.args.get('restore_error') == '1',
    )


@volar_us_bp.route("/volar-us/clear-source", methods=["POST"])
def volar_us_clear_source():
    """Remove the pinned CSV/XLSX so the next scan falls back to the
    built-in S&P 500 default. POST-only to prevent accidental clears via
    browser prefetch or link crawlers."""
    try:
        if os.path.exists(LAST_CSV_CONFIG):
            os.remove(LAST_CSV_CONFIG)
    except OSError:
        pass
    return redirect(url_for('volar_us.volar_us_process'))


@volar_us_bp.route("/volar-us/progress")
def volar_us_progress():
    return jsonify(_get_progress())


@volar_us_bp.route("/restore-volar-us/<snapshot_id>", methods=["POST"])
def restore_volar_us_snapshot(snapshot_id):
    """POST + confirm — prevents stray click / prefetch overwriting live results."""
    safe_id       = os.path.basename(snapshot_id)
    snapshot_path = os.path.join(HISTORY_CACHE_DIR, f"{safe_id}.json")

    if not (safe_id.startswith('snapshot_') and os.path.exists(snapshot_path)):
        return redirect(url_for('volar_us.volar_us_process', restore_error=1))

    try:
        with open(snapshot_path, 'r') as f:
            payload = json.load(f)

        # Patch in the original scan metadata from meta_history if available
        for m in _load_meta_history():
            if m.get('snapshot_id') == safe_id:
                payload.setdefault('time',             m.get('time', ''))
                payload.setdefault('source',           m.get('source', ''))
                payload.setdefault('benchmark_label',  m.get('benchmark_label'))
                payload.setdefault('price_data_asof',  m.get('price_data_asof'))
                break

        payload['time'] = payload.get('time', '') + ' (Restored Snapshot)'
        with open(RESULTS_JSON, 'w') as f:
            json.dump(payload, f)
    except (json.JSONDecodeError, OSError):
        return redirect(url_for('volar_us.volar_us_process', restore_error=1))

    return redirect(url_for('volar_us.volar_us_process', restored=1))


@volar_us_bp.route("/export-volar-us")
def export_volar_us():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            data = json.load(f)
        stocks = data.get('stocks', [])
        if stocks:
            df              = pd.DataFrame(stocks)
            timestamp       = datetime.now().strftime("%Y%m%d_%H%M%S")
            export_filename = f"US_Volar_Stage2_{timestamp}.csv"
            export_path     = os.path.join(UPLOAD_FOLDER, 'temp_export_us.csv')
            df.to_csv(export_path, index=False)
            return send_file(export_path, as_attachment=True, download_name=export_filename)
    return "No data to export", 404


@volar_us_bp.route("/add-favorite-us", methods=["POST"])
def add_favorite_us():
    symbol   = request.form.get('symbol')
    fav_path = os.path.join(UPLOAD_FOLDER, 'favorites_us.json')
    favorites = []
    if os.path.exists(fav_path):
        with open(fav_path, 'r') as f:
            favorites = json.load(f)
    if symbol not in favorites:
        favorites.append(symbol)
        with open(fav_path, 'w') as f:
            json.dump(favorites, f)
    return {"status": "success", "message": f"{symbol} added to US Watchlist"}


@volar_us_bp.route("/view-favorites-us")
def view_favorites_us():
    fav_path = os.path.join(UPLOAD_FOLDER, 'favorites_us.json')
    stocks = []
    if os.path.exists(fav_path):
        with open(fav_path, 'r') as f:
            symbols = json.load(f)
        idx_data, _ = us_cache.get_price_history_bulk([US_INDEX[0]], interval='1d', lookback_days=500)
        index_df    = idx_data.get(US_INDEX[0])
        price_data, _ = us_cache.get_price_history_bulk(symbols, interval='1d', lookback_days=500)
        for sym in symbols:
            data = is_volar_us_candidate(sym, index_df, price_data.get(sym)) if index_df is not None else None
            if data:
                stocks.append(data)
    return render_template("view_favorites.html", stocks=stocks, market="US", currency="$")


@volar_us_bp.route("/remove-favorite-us", methods=["POST"])
def remove_favorite_us():
    symbol   = request.form.get('symbol')
    fav_path = os.path.join(UPLOAD_FOLDER, 'favorites_us.json')
    if os.path.exists(fav_path):
        with open(fav_path, 'r') as f:
            favorites = json.load(f)
        if symbol in favorites:
            favorites.remove(symbol)
            with open(fav_path, 'w') as f:
                json.dump(favorites, f)
    return {"status": "success"}