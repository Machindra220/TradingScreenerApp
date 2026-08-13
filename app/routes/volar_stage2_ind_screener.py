import os
import json
import uuid
import threading
import pandas as pd
import yfinance as yf
from datetime import datetime
from flask import Blueprint, render_template, request, send_file, redirect, url_for, jsonify

from app.services.market_data_cache import get_price_history_bulk, latest_bar_date  # shared cross-page price cache

volar_bp = Blueprint("volar_ind", __name__)

# --- PATH LOGIC (Root Level) ---
# Anchor all paths to __file__ (app/routes/volar_stage2_ind_screener.py)
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER = os.path.join(_PROJECT_ROOT, 'uploads', 'volar_ind')
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_volar_results.json')
LAST_CSV_CONFIG = os.path.join(UPLOAD_FOLDER, 'last_csv_path.json')
HISTORY_CACHE_DIR = os.path.join(UPLOAD_FOLDER, 'history_cache')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(HISTORY_CACHE_DIR, exist_ok=True)

HISTORY_LIMIT = 5
DEFAULT_IND_CSV   = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'nifty_500.csv'))
DEFAULT_IND_LABEL = "Nifty 500 Default (nifty_500.csv)"


def _get_active_source_ind():
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
    return DEFAULT_IND_CSV, DEFAULT_IND_LABEL, True

# Benchmark resolution: try the broad Nifty 500 index first (this page's own
# description is "RS %tile against Nifty500"), fall back to Nifty 50 if
# yfinance can't return enough history for it. Whichever one actually gets
# used is recorded and shown in the UI, matching the convention on the
# Adaptive VOLAR screener.
PRIMARY_INDEX = ("^CRSLDX", "Nifty 500")
FALLBACK_INDEX = ("^NSEI", "Nifty 50")

# ---------------------------------------------------------------------------
# In-memory scan progress (single-process; fine for a personal-use tool).
# ---------------------------------------------------------------------------
progress_lock = threading.Lock()
SCAN_PROGRESS = {
    "active": False,
    "processed": 0,
    "total": 0,
    "current_symbol": "",
    "stage": "idle",   # idle | reading_csv | fetching_index | scanning | done | error
    "error": None,
}


def _set_progress(**kwargs):
    with progress_lock:
        SCAN_PROGRESS.update(kwargs)


def _get_progress():
    with progress_lock:
        return dict(SCAN_PROGRESS)


def fetch_index_data():
    """Resolve the benchmark via the shared cache (so it's only genuinely
    re-fetched from yfinance once a day, across BOTH this page and the
    Adaptive VOLAR screener, instead of once per scan per page). Tries
    Nifty 500 first, falls back to Nifty 50. Returns (index_df, label) or
    (None, None) if both fail."""
    for ticker_sym, label in (PRIMARY_INDEX, FALLBACK_INDEX):
        try:
            results, _ = get_price_history_bulk([ticker_sym], interval="1d", lookback_days=500,
                                        progress_callback=lambda *a: None)
            idx_df_raw = results.get(ticker_sym)
            idx_df = _normalise_df(idx_df_raw, ticker_sym)
            if idx_df is not None and not idx_df.empty and 'Close' in idx_df.columns and len(idx_df) >= 200:
                return idx_df, f"{label} ({ticker_sym})"
        except Exception as e:
            print(f"  Benchmark fetch failed for {ticker_sym}: {e}")
    return None, None


def compute_volar(close_series):
    total_return = (close_series.iloc[-1] / close_series.iloc[0]) - 1
    volatility = close_series.pct_change(fill_method=None).std()
    if volatility is None or pd.isna(volatility) or volatility == 0:
        return None
    return total_return / volatility


def compute_relative_strength(stock_close, index_close):
    """
    Relative strength vs the benchmark, expressed as the stock's total return
    in EXCESS of the index's total return over the same window
    (e.g. 0.12 => the stock beat the index by 12 percentage points).

    NOTE: this used to be stock_return / index_return, which inverts sign
    whenever the index return is negative — in a down market, a stock that
    fell LESS than the index would score worse than one that fell MORE, and
    vice versa. (1+stock_return)/(1+index_return) - 1 is a ratio-of-relatives
    that stays monotonic in stock_return regardless of the index's sign, so
    "higher RS" reliably means "did better than the benchmark."
    """
    stock_return = (stock_close.iloc[-1] / stock_close.iloc[0]) - 1
    index_return = (index_close.iloc[-1] / index_close.iloc[0]) - 1
    if index_return <= -1:
        return None
    return ((1 + stock_return) / (1 + index_return)) - 1



def _normalise_df(df, sym=None):
    """
    Handles all three DataFrame formats from cache / yfinance:
      - yf.Ticker(sym).history()     → simple string cols ('Close', etc.)
      - yf.download([sym1, sym2...]) → MultiIndex tuple cols ('Close','TCS.NS')
      - cache bulk fetch             → either of the above
    Without this, MultiIndex DataFrames returned by the bulk fetch fail
    silently on stock_df["Close"], causing most symbols to be skipped.
    """
    if df is None or df.empty:
        return None
    cols = df.columns
    if isinstance(cols, pd.MultiIndex) or (len(cols) > 0 and isinstance(cols[0], tuple)):
        if sym is not None:
            for cand in [sym, sym.replace('.NS', ''), sym + '.NS']:
                try:
                    sliced = df.xs(cand, axis=1, level=1)
                    sliced.columns = [c.title() for c in sliced.columns]
                    return sliced
                except KeyError:
                    pass
        flat_cols = {}
        for c in cols:
            field = c[0] if isinstance(c, tuple) else c
            if field.lower() in ('open', 'high', 'low', 'close', 'volume'):
                flat_cols[c] = field.title()
        if flat_cols:
            df = df[list(flat_cols.keys())].copy()
            df.columns = list(flat_cols.values())
            return df
        return None
    df = df.copy()
    df.columns = [c.title() if isinstance(c, str) and
                  c.lower() in ('open', 'high', 'low', 'close', 'volume')
                  else c for c in df.columns]
    return df


def is_volar_candidate(symbol, index_df, stock_df):
    """Evaluates a single symbol against the Stage-2 criteria using a price
    history DataFrame that was already pulled from the shared cache — this
    function does no network I/O of its own."""
    try:
        # Normalise both DataFrames — handles simple-column AND MultiIndex
        # bulk-fetch output. Without this, stock_df["Close"] raises KeyError
        # on MultiIndex DataFrames and the symbol is silently skipped.
        stock_df  = _normalise_df(stock_df, symbol)
        index_df  = _normalise_df(index_df)
        if stock_df is None or stock_df.empty or index_df is None or index_df.empty or len(stock_df) < 200:
            return None
        if 'Close' not in stock_df.columns or 'Close' not in index_df.columns:
            return None

        close = stock_df["Close"]
        high_52w = close[-252:].max()
        current_price = close.iloc[-1]
        pullback = (high_52w - current_price) / high_52w
        # adjust=False standardized to match the Adaptive VOLAR screener's
        # EMA200 — otherwise the two pages can disagree on a borderline
        # Stage-2 pass/fail for the same stock on the same day.
        ema_200 = close.ewm(span=200, adjust=False).mean().iloc[-1]

        volar_val = compute_volar(close)
        rs_val = compute_relative_strength(close, index_df["Close"])
        performance = (close.iloc[-1] / close.iloc[0]) - 1

        # NOTE: this used to be `... and volar_val and rs_val`, which treats
        # a legitimate 0.0 (e.g. a stock performing exactly in line with the
        # index) as falsy and silently drops it. `is not None` is the correct
        # check — we only want to exclude genuine calculation failures.
        if pullback < 0.3 and current_price > ema_200 and volar_val is not None and rs_val is not None:
            return {
                "symbol": symbol,
                "price": round(current_price, 2),
                "pullback_pct": round(pullback * 100, 2),
                "ema_200": round(ema_200, 2),
                "volar": round(volar_val, 2),
                "relative_strength": round(rs_val, 2),
                "performance": round(performance * 100, 2)
            }
    except Exception as e:
        print(f"Error screening {symbol}: {e}")
    return None


def screen_volar(symbols, index_df):
    # Bulk-fetch every symbol's price history from the shared cache ONCE,
    # up front — this is what actually eliminates the per-symbol yfinance
    # call that used to happen inside is_volar_candidate(). Only symbols
    # that are genuinely missing/stale in the cache trigger a real network
    # call; a failed fetch for one symbol never affects any other symbol's
    # data or drops it from the run — it just falls back to whatever was
    # last cached for it, if anything.
    #
    # progress_callback here is what makes the scan button's progress bar
    # actually move during this call — this is where essentially all the
    # wall-clock time goes on a cold/stale cache (network fetches, with
    # pacing delays to avoid re-tripping rate limits), so without wiring
    # progress through this call specifically, the button would sit at 0%
    # for the entire fetch and only start moving once this returns.
    def _fetch_progress(i, total, sym):
        _set_progress(stage="fetching_prices", processed=i, total=total, current_symbol=sym)

    price_data, fetch_report = get_price_history_bulk(
        symbols, interval="1d", lookback_days=500, progress_callback=_fetch_progress
    )
    price_data_asof = latest_bar_date(price_data)

    _set_progress(stage="scanning", processed=0, total=len(symbols), current_symbol="")
    results = []
    for i, sym in enumerate(symbols):
        _set_progress(processed=i, current_symbol=sym)
        data = is_volar_candidate(sym, index_df, price_data.get(sym))
        if data:
            results.append(data)
    _set_progress(processed=len(symbols), current_symbol="")

    if not results:
        return [], fetch_report, price_data_asof

    df = pd.DataFrame(results)
    # NOTE: this percentile is computed only among stocks that already
    # PASSED the Stage-2 shortlist filter above (pullback < 30% and price >
    # 200-EMA) — not against the full Nifty 500 universe. That means it's a
    # "how strong within today's shortlist" score, not a literal "vs all
    # 500 stocks" percentile.
    df['rs_percentile'] = df['relative_strength'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)

    existing_history = {}
    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON, 'r') as f:
                old_cache = json.load(f).get('stocks', [])
                existing_history = {s['symbol']: {
                    'rs_h': s.get('rs_h', []),
                    'vol_h': s.get('vol_h', []),
                    'perf_h': s.get('perf_h', [])
                } for s in old_cache}
        except (json.JSONDecodeError, OSError):
            existing_history = {}

    def inject_trends(row):
        sym = row['symbol']
        h = existing_history.get(sym, {'rs_h': [], 'vol_h': [], 'perf_h': []})
        row['rs_h'] = (h['rs_h'] + [row['rs_percentile']])[-5:]
        row['vol_h'] = (h['vol_h'] + [row['volar']])[-5:]
        row['perf_h'] = (h['perf_h'] + [row['performance']])[-5:]
        row['rs_up'] = len(row['rs_h']) > 1 and all(x < y for x, y in zip(row['rs_h'], row['rs_h'][1:]))
        return row

    df = df.apply(inject_trends, axis=1)
    df.sort_values(by="relative_strength", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    df["rank"] = df.index + 1
    return df.to_dict(orient="records"), fetch_report, price_data_asof


def _normalize_stock(s):
    """Backfills fields the template expects that an older cached scan might
    not have written, so a stale results file never crashes the template."""
    s.setdefault('rs_h', [])
    s.setdefault('vol_h', [])
    s.setdefault('perf_h', [])
    s.setdefault('rs_up', False)
    s.setdefault('rank_diff', 0)
    s.setdefault('rank_status', 'stable')
    s.setdefault('symbol_clean', s.get('symbol', '').replace('.NS', ''))
    s.setdefault('is_consistent', False)
    return s


def _prune_snapshots(keep_snapshot_ids):
    keep_files = {f"{sid}.json" for sid in keep_snapshot_ids}
    for fname in os.listdir(HISTORY_CACHE_DIR):
        if fname == 'meta_history.json':
            continue
        if fname not in keep_files:
            try:
                os.remove(os.path.join(HISTORY_CACHE_DIR, fname))
            except OSError:
                pass


def run_scan(filepath, source_name):
    """Runs the full scan in a background thread and persists results/progress."""
    _set_progress(active=True, processed=0, total=0, current_symbol="",
                  stage="reading_csv", error=None)

    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext in ('.xlsx', '.xls'):
            df_input = pd.read_excel(filepath)
        else:
            df_input = pd.read_csv(filepath)
    except Exception as e:
        _set_progress(active=False, stage="error", error=f"Could not read file: {e}")
        return

    col_map  = {c.lower(): c for c in df_input.columns}
    col_name = next((col_map[k] for k in ('symbol', 'ticker', 'symbols', 'tickers') if k in col_map), None)
    if col_name is None:
        _set_progress(active=False, stage="error",
                       error=f"File must have a Symbol or Ticker column. Found: {', '.join(df_input.columns.tolist())}")
        return

    raw_symbols = (str(s).strip().upper() for s in df_input[col_name].dropna().unique())
    symbols = [s if s.endswith(".NS") else f"{s}.NS" for s in raw_symbols if str(s).strip()]

    _set_progress(stage="fetching_index", total=len(symbols))
    index_df, benchmark_label = fetch_index_data()
    if index_df is None:
        _set_progress(active=False, stage="error",
                       error="Could not fetch benchmark index data. Scan aborted — previous results kept.")
        return

    old_ranks = {}
    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON, 'r') as f:
                cache = json.load(f)
                old_ranks = {s.get('symbol_clean', s['symbol']): s['rank'] for s in cache.get('stocks', [])}
        except (json.JSONDecodeError, OSError):
            old_ranks = {}

    _set_progress(stage="scanning")
    results, fetch_report, price_data_asof = screen_volar(symbols, index_df)

    # ── Cache source log ─────────────────────────────────────────────────────
    _n  = len(symbols)
    _ch = fetch_report["from_cache"]
    _yf = fetch_report["fetched"]
    _fl = fetch_report["failed"]
    sep = "=" * 55
    print(f"\n{sep}")
    print(f"  [CACHE] IND Stage2 {source_name} — price data source summary")
    print(f"{sep}")
    print(f"  Total symbols   : {_n}")
    print(f"  From DB cache   : {_ch} ({round(_ch/_n*100) if _n else 0}%)  <- no yfinance call")
    print(f"  Fetched fresh   : {_yf}  ({round(_yf/_n*100) if _n else 0}%)  <- yfinance hit + DB updated")
    print(f"  Failed (429/err): {len(_fl)}")
    if _fl:
        extra = f" ...+{len(_fl)-10} more" if len(_fl) > 10 else ""
        print(f"  Failed symbols  : {', '.join(_fl[:10])}{extra}")
    print(f"  Price data as of: {price_data_asof}")
    print(f"{sep}\n")

    enriched = []
    leaders_90 = []
    for stock in results:
        sym_clean = stock["symbol"].replace(".NS", "")
        stock["symbol_clean"] = sym_clean
        if stock.get('rs_percentile', 0) >= 90:
            leaders_90.append(sym_clean)

        prev_rank = old_ranks.get(sym_clean)
        if prev_rank is None:
            stock["rank_status"], stock["rank_diff"] = "new", 0
        else:
            diff = prev_rank - stock["rank"]
            stock["rank_diff"] = diff
            stock["rank_status"] = "up" if diff > 0 else ("down" if diff < 0 else "stable")
        enriched.append(stock)

    last_processed_time = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    scanned_count = len(symbols)
    excluded_count = scanned_count - len(results)

    # Snapshot this scan to disk BEFORE overwriting the active results, so a
    # bad/thin scan can always be rolled back to from Scan History.
    snapshot_id = f"snapshot_{uuid.uuid4().hex}"
    snapshot_file_path = os.path.join(HISTORY_CACHE_DIR, f"{snapshot_id}.json")
    with open(snapshot_file_path, 'w') as f:
        json.dump(enriched, f)

    meta_history_file = os.path.join(HISTORY_CACHE_DIR, 'meta_history.json')
    history_meta = []
    if os.path.exists(meta_history_file):
        try:
            with open(meta_history_file, 'r') as f:
                history_meta = json.load(f)
        except (json.JSONDecodeError, OSError):
            history_meta = []

    history_meta.insert(0, {
        "snapshot_id": snapshot_id,
        "time": last_processed_time,
        "source": source_name,
        "count": len(enriched),
        "leaders_90": leaders_90,
        "benchmark_label": benchmark_label,
        "price_data_asof": price_data_asof,
    })
    history_meta = history_meta[:HISTORY_LIMIT]
    with open(meta_history_file, 'w') as f:
        json.dump(history_meta, f)

    # Drop snapshot files that fell out of the retained history window —
    # previously these accumulated forever since nothing ever deleted them.
    _prune_snapshots([h['snapshot_id'] for h in history_meta])

    with open(RESULTS_JSON, 'w') as f:
        json.dump({
            'stocks': enriched,
            'time': last_processed_time,
            'source': source_name,
            'benchmark_label': benchmark_label,
            'scanned_count': scanned_count,
            'excluded_count': excluded_count,
            # Symbols the shared price cache couldn't refresh this run (e.g.
            # hit a 429 after retries) — they used whatever was already
            # cached rather than being dropped or blocking the scan.
            'stale_symbols_count': len(fetch_report['failed']),
            'stale_symbols_sample': [s.replace('.NS', '') for s in fetch_report['failed'][:10]],
        'cache_hits':           fetch_report['from_cache'],
        'yf_fetches':           fetch_report['fetched'],
            # The most recent trading-day close actually reflected in the
            # underlying price data used for this scan — distinct from
            # 'time' above (when the scan itself ran). A scan can complete
            # instantly off cached data that's a day or more old if a
            # refresh was skipped/failed, so this is the real "how current
            # is this data" answer, not the scan timestamp.
            'price_data_asof': price_data_asof,
        }, f)

    _set_progress(active=False, stage="done", current_symbol="")


@volar_bp.route("/volar-ind", methods=["GET", "POST"])
def volar_process():
    compare_mode = request.args.get('compare') == 'true'

    if request.method == "POST":
        if _get_progress()["active"]:
            return redirect(url_for('volar_ind.volar_process', scanning=1))

        file = request.files.get('file')
        use_default = request.form.get('use_default') == '1'
        filepath, source_name = None, None

        if file and file.filename != '':
            from werkzeug.utils import secure_filename
            filename      = secure_filename(file.filename)
            ext           = os.path.splitext(filename)[1].lower()
            save_filename = f"uploaded_ind_tickers{ext}"
            filepath      = os.path.join(UPLOAD_FOLDER, save_filename)
            file.save(filepath)
            with open(LAST_CSV_CONFIG, 'w') as f:
                json.dump({'path': filepath, 'name': filename}, f)
            source_name = filename
        elif use_default:
            filepath, source_name = DEFAULT_IND_CSV, DEFAULT_IND_LABEL
        else:
            filepath, source_name, _ = _get_active_source_ind()

        if not filepath or not os.path.exists(filepath):
            err = (f"Default file not found: {DEFAULT_IND_CSV}. "
                   f"Please place nifty_500.csv in the data/ folder or upload a custom CSV.")
            _set_progress(active=False, stage="error", error=err)
            return redirect(url_for('volar_ind.volar_process'))

        thread = threading.Thread(target=run_scan, args=(filepath, source_name), daemon=True)
        thread.start()

        return redirect(url_for('volar_ind.volar_process', scanning=1))

    # --- GET ---
    stocks = []
    last_processed_time = None
    source_name = "None"
    benchmark_label = None
    excluded_count = 0
    scanned_count = 0
    stale_symbols_count = 0
    stale_symbols_sample = []
    price_data_asof = None

    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON, 'r') as f:
                cache = json.load(f)
                stocks = [_normalize_stock(s) for s in cache.get('stocks', [])]
                last_processed_time = cache.get('time')
                source_name = cache.get('source', 'Cached Scan')
                benchmark_label = cache.get('benchmark_label')
                excluded_count = cache.get('excluded_count', 0)
                scanned_count = cache.get('scanned_count', 0)
                stale_symbols_count = cache.get('stale_symbols_count', 0)
                stale_symbols_sample = cache.get('stale_symbols_sample', [])
                cache_hits          = cache.get('cache_hits', 0)
                yf_fetches          = cache.get('yf_fetches', 0)
                price_data_asof = cache.get('price_data_asof')
        except (json.JSONDecodeError, OSError):
            pass

    meta_history_file = os.path.join(HISTORY_CACHE_DIR, 'meta_history.json')
    history_meta = []
    if os.path.exists(meta_history_file):
        try:
            with open(meta_history_file, 'r') as f:
                history_meta = json.load(f)
        except (json.JSONDecodeError, OSError):
            history_meta = []

    if compare_mode and len(history_meta) >= 3:
        leader_sets = [set(h.get('leaders_90', [])) for h in history_meta[:3]]
        consistent_symbols = set.intersection(*leader_sets) if leader_sets else set()
        for s in stocks:
            if s.get('symbol_clean') in consistent_symbols:
                s['is_consistent'] = True

    progress = _get_progress()
    is_scanning = progress["active"] or request.args.get('scanning') == '1'
    _, active_file, is_default_source = _get_active_source_ind()

    return render_template(
        "stage2_volar_ind.html",
        stocks=stocks,
        last_processed_time=last_processed_time,
        source_name=source_name,
        benchmark_label=benchmark_label,
        excluded_count=excluded_count,
        scanned_count=scanned_count,
        stale_symbols_count=stale_symbols_count,
        cache_hits=cache_hits,
        yf_fetches=yf_fetches,
        stale_symbols_sample=stale_symbols_sample,
        price_data_asof=price_data_asof,
        history=history_meta,
        compare_mode=compare_mode,
        is_scanning=is_scanning,
        scan_error=progress.get("error"),
        active_file=active_file,
        is_default_source=is_default_source,
        default_label=DEFAULT_IND_LABEL,
        restored=request.args.get('restored') == '1',
        restore_error=request.args.get('restore_error') == '1',
    )


@volar_bp.route("/volar-ind/clear-source", methods=["POST"])
def volar_ind_clear_source():
    """Remove the pinned CSV so the next scan uses Nifty 500 default."""
    try:
        if os.path.exists(LAST_CSV_CONFIG):
            os.remove(LAST_CSV_CONFIG)
    except OSError:
        pass
    return redirect(url_for('volar_ind.volar_process'))


@volar_bp.route("/volar-ind/progress")
def volar_ind_progress():
    return jsonify(_get_progress())


@volar_bp.route("/restore-volar-ind/<snapshot_id>", methods=["POST"])
def restore_volar_ind_snapshot(snapshot_id):
    """Roll the active results back to an earlier scan snapshot. Now a POST
    (was a bare GET link with no confirmation — a stray click or crawler
    prefetch could silently overwrite current results)."""
    safe_id = os.path.basename(snapshot_id)
    snapshot_file_path = os.path.join(HISTORY_CACHE_DIR, f"{safe_id}.json")
    meta_history_file = os.path.join(HISTORY_CACHE_DIR, 'meta_history.json')

    if not os.path.exists(snapshot_file_path):
        return redirect(url_for('volar_ind.volar_process', restore_error=1))

    try:
        with open(snapshot_file_path, 'r') as f:
            restored_records = json.load(f)
    except (json.JSONDecodeError, OSError):
        return redirect(url_for('volar_ind.volar_process', restore_error=1))

    restored_time = datetime.now().strftime("%d-%b-%Y %H:%M:%S") + " (Restored Snapshot)"
    restored_source = "Snapshot"
    restored_benchmark = None
    restored_price_asof = None

    if os.path.exists(meta_history_file):
        try:
            with open(meta_history_file, 'r') as f:
                meta_list = json.load(f)
            for m in meta_list:
                if m.get('snapshot_id') == safe_id:
                    restored_time = m.get('time') + " (Restored Snapshot)"
                    restored_source = m.get('source')
                    restored_benchmark = m.get('benchmark_label')
                    restored_price_asof = m.get('price_data_asof')
                    break
        except (json.JSONDecodeError, OSError):
            pass

    with open(RESULTS_JSON, 'w') as f:
        json.dump({
            'stocks': restored_records,
            'time': restored_time,
            'source': restored_source,
            'benchmark_label': restored_benchmark,
            'price_data_asof': restored_price_asof,
            'scanned_count': None,
            'excluded_count': None,
        }, f)

    return redirect(url_for('volar_ind.volar_process', restored=1))


@volar_bp.route("/export-volar")
def export_volar():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            data = json.load(f)
        stocks = data.get('stocks', [])
        if stocks:
            df = pd.DataFrame(stocks)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            export_filename = f"Volar_Screener_{timestamp}.csv"
            export_path = os.path.join(UPLOAD_FOLDER, 'temp_export.csv')
            df.to_csv(export_path, index=False)
            return send_file(export_path, as_attachment=True, download_name=export_filename)
    return "No data to export", 404


@volar_bp.route("/add-favorite-india", methods=["POST"])
def add_favorite_india():
    symbol = request.form.get('symbol')
    fav_path = os.path.join(UPLOAD_FOLDER, 'favorites_india.json')
    favorites = []
    if os.path.exists(fav_path):
        with open(fav_path, 'r') as f:
            favorites = json.load(f)
    if symbol not in favorites:
        favorites.append(symbol)
        with open(fav_path, 'w') as f:
            json.dump(favorites, f)
    return {"status": "success", "message": f"{symbol} added to India Watchlist"}


@volar_bp.route("/view-favorites-india")
def view_favorites_india():
    fav_path = os.path.join(UPLOAD_FOLDER, 'favorites_india.json')
    stocks = []
    if os.path.exists(fav_path):
        with open(fav_path, 'r') as f:
            symbols = json.load(f)
        index_df, _ = fetch_index_data()
        yf_symbols = [s if s.endswith(".NS") else f"{s}.NS" for s in symbols]
        # Same shared-cache bulk fetch as the main scan — a watchlist of even
        # a couple dozen symbols was previously hitting yfinance once per
        # symbol every time this page loaded.
        price_data, _ = get_price_history_bulk(yf_symbols, interval="1d", lookback_days=500,
                                            progress_callback=lambda *a: None) if index_df is not None else ({}, {})
        for sym, yf_sym in zip(symbols, yf_symbols):
            try:
                raw_df = price_data.get(yf_sym)
                norm_df = _normalise_df(raw_df, yf_sym) if raw_df is not None else None
                data = is_volar_candidate(yf_sym, index_df, norm_df) if index_df is not None else None
                if data:
                    data['symbol_clean'] = sym
                    stocks.append(data)
            except Exception as e:
                print(f"Error loading favorite {sym}: {e}")
                continue
    return render_template("view_favorites.html", stocks=stocks, market="India", currency="₹")


@volar_bp.route("/remove-favorite-india", methods=["POST"])
def remove_favorite_india():
    symbol = request.form.get('symbol')
    fav_path = os.path.join(UPLOAD_FOLDER, 'favorites_india.json')
    if os.path.exists(fav_path):
        with open(fav_path, 'r') as f:
            favorites = json.load(f)
        if symbol in favorites:
            favorites.remove(symbol)
            with open(fav_path, 'w') as f:
                json.dump(favorites, f)
    return {"status": "success"}


@volar_bp.route("/add-to-strategy", methods=["POST"])
def add_to_strategy():
    symbol = request.form.get('symbol')
    strategy = request.form.get('strategy')
    market = request.form.get('market')
    folder = UPLOAD_FOLDER if market == 'india' else os.path.join(_PROJECT_ROOT, 'uploads', 'volar_us')
    fav_path = os.path.join(folder, f'strategy_{strategy.lower().replace(" ", "_")}.json')
    yf_sym = symbol if market == 'us' else (symbol if symbol.endswith(".NS") else f"{symbol}.NS")
    ticker = yf.Ticker(yf_sym)
    current_price = ticker.history(period="1d")['Close'].iloc[-1]
    new_entry = {
        "symbol": symbol,
        "entry_date": datetime.now().strftime("%Y-%m-%d"),
        "entry_price": round(current_price, 2)
    }
    data = []
    if os.path.exists(fav_path):
        with open(fav_path, 'r') as f:
            data = json.load(f)
    if not any(item['symbol'] == symbol for item in data):
        data.append(new_entry)
        with open(fav_path, 'w') as f:
            json.dump(data, f)
    return {"status": "success", "message": f"{symbol} added to {strategy} strategy at ${round(current_price, 2)}"}


@volar_bp.route("/view-strategy/<name>")
def view_strategy(name):
    # NOTE: the route parameter used to be <n> while the function parameter
    # was `name` — Flask passes route params as keyword args, so this raised
    # a TypeError ("unexpected keyword argument 'n'") every time this route
    # was hit. Fixed by matching the two.
    market = request.args.get('market', 'india')
    folder = UPLOAD_FOLDER if market == 'india' else os.path.join(_PROJECT_ROOT, 'uploads', 'volar_us')
    file_path = os.path.join(folder, f'strategy_{name.lower()}.json')
    performance_data = []
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            entries = json.load(f)
        for item in entries:
            yf_sym = item['symbol'] if market == 'us' else f"{item['symbol']}.NS"
            ticker = yf.Ticker(yf_sym)
            current_price = ticker.history(period="1d")['Close'].iloc[-1]
            ret_pct = ((current_price - item['entry_price']) / item['entry_price']) * 100
            performance_data.append({
                "symbol": item['symbol'],
                "entry_date": item['entry_date'],
                "entry_price": item['entry_price'],
                "current_price": round(current_price, 2),
                "return_pct": round(ret_pct, 2)
            })
    return render_template("strategy_watchlist.html", stocks=performance_data, strategy_name=name.upper(), currency="₹" if market == 'india' else "$")