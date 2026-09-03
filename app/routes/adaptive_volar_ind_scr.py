import os
import json
import threading
import pandas as pd
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, session, jsonify, redirect, url_for

from app.services.market_data_cache import ind_cache, latest_bar_date  # shared IND SQLite cache

volar_ind_adaptive_bp = Blueprint('volar_ind_adaptive_bp', __name__)

# Anchor all paths to __file__ (app/routes/adaptive_volar_ind_scr.py) so
# they resolve correctly regardless of where Flask was started from — using
# os.getcwd() at module-import time is fragile because the Werkzeug reloader
# can run in a different working directory than the main process.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER = os.path.join(_PROJECT_ROOT, 'uploads', 'volar_ind_adaptive')
SNAPSHOT_DIR = os.path.join(UPLOAD_FOLDER, 'snapshots')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SNAPSHOT_DIR, exist_ok=True)

RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'volar_results_ind_adaptive.json')
HISTORY_JSON = os.path.join(UPLOAD_FOLDER, 'scan_history_ind_adaptive.json')

# How many past scans to keep browsable/restorable
HISTORY_LIMIT = 5

DEFAULT_ADP_CSV   = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'nifty_500.csv'))
DEFAULT_ADP_LABEL = "Nifty 500 Default (nifty_500.csv)"

# Fixed lookback periods (trading days)
LB_3M = 55    # ~3 months
LB_6M = 122   # ~6 months

# Benchmark resolution: try the broad Nifty 500 index first (best match for RS
# vs. the wider market), fall back to Nifty 50 if yfinance can't return enough
# history for it. Whichever one actually gets used is recorded and shown in the
# UI so the "Benchmark:" label always matches the data the RS numbers were
# computed against.
PRIMARY_INDEX = ("^CRSLDX", "Nifty 500")
FALLBACK_INDEX = ("^NSEI", "Nifty 50")

# Fields that must be valid numbers for a stock to be included in a scan.
REQUIRED_NUMERIC_FIELDS = ['rs_3m', 'rs_6m', 'volar_3m', 'volar_6m']


# ---------------------------------------------------------------------------
# DataFrame column normaliser — handles both simple-string and MultiIndex
# bulk-fetch formats returned by the shared cache.  Without this, symbols
# whose DataFrames arrive as MultiIndex (the common case from yf.download)
# crash silently on stock_df['Close'] and are skipped entirely.
# ---------------------------------------------------------------------------

def _normalise_df(df, sym=None):
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
    df.columns = [c.title() if isinstance(c, str) and c.lower() in
                  ('open', 'high', 'low', 'close', 'volume') else c for c in df.columns]
    return df

# ---------------------------------------------------------------------------
# In-memory scan progress (single-process; fine for a personal-use tool).
# ---------------------------------------------------------------------------
progress_lock = threading.Lock()
SCAN_PROGRESS = {
    "active": False,
    "processed": 0,
    "total": 0,
    "current_symbol": "",
    "stage": "idle",   # idle | fetching_index | fetching_prices | scanning | done | error
    "error": None,
}


def _set_progress(**kwargs):
    with progress_lock:
        SCAN_PROGRESS.update(kwargs)


def _get_progress():
    with progress_lock:
        return dict(SCAN_PROGRESS)


def compute_volar(prices):
    """Return-to-volatility ratio over the given price window."""
    if len(prices) < 2:
        return None
    returns = prices.pct_change().dropna()
    std = returns.std()
    if std == 0 or pd.isna(std):
        return None
    total_ret = (prices.iloc[-1] / prices.iloc[0]) - 1
    return round(total_ret / std, 2)


def make_bar_heights(values, max_height=14, min_height=2):
    """
    Heights (px) for a tiny 'growing bars' trend indicator, scaled directly
    against the 0-100 percentile range (not min/max of the 5 points) so bar
    heights are comparable across different stocks' rows, not just within one.
    """
    if not values:
        return []
    return [round(min_height + (max(0, min(100, v)) / 100) * (max_height - min_height), 1) for v in values]


def fetch_index_data():
    """
    Resolve the benchmark via the shared cache (so it's only genuinely
    re-fetched from yfinance once a day, across BOTH this page and the
    Stage 2 screener, instead of once per scan per page). Tries Nifty 500
    first, falls back to Nifty 50. Returns (close_series, label) or
    (None, None) if both fail.
    """
    for ticker_sym, label in (PRIMARY_INDEX, FALLBACK_INDEX):
        try:
            # get_price_history_bulk returns (dict, report) — unpack correctly.
            idx_data, _ = ind_cache.get_price_history_bulk(
                [ticker_sym], interval='1d', lookback_days=LB_6M + 220,
                progress_callback=lambda *a: None,
            )
            idx_df_raw = idx_data.get(ticker_sym)
            idx_df = _normalise_df(idx_df_raw, ticker_sym)
            if idx_df is not None and 'Close' in idx_df.columns and len(idx_df) >= LB_6M:
                return idx_df['Close'].dropna(), f"{label} ({ticker_sym})"
        except Exception as e:
            print(f"  Benchmark fetch failed for {ticker_sym}: {e}")
    return None, None


def is_volar_adaptive_ind(symbol, idx_close, stock_df):
    """
    Computes RS and VOLAR for both 3M (55-day) and 6M (122-day) lookback
    periods against a pre-fetched benchmark series, using a price history
    DataFrame that was already pulled from the shared cache (not fetched
    here — this function does no network I/O of its own). Returns None if
    the stock fails the EMA200 filter or lacks clean data.
    """
    try:
        if stock_df is None or stock_df.empty:
            return None
        # Normalise before column access — cache may return MultiIndex format
        stock_df = _normalise_df(stock_df, symbol if symbol.endswith('.NS') else f"{symbol}.NS")
        if stock_df is None or stock_df.empty or 'Close' not in stock_df.columns or len(stock_df) < LB_6M:
            return None

        close = stock_df['Close'].dropna()
        if len(close) < LB_6M:
            return None
        curr = close.iloc[-1]

        # Align the benchmark series to this stock's actual trading dates so
        # the "N trading days ago" offsets line up even when the stock has
        # occasional missing sessions (illiquid names, corporate actions).
        idx_aligned = idx_close.reindex(close.index).ffill()
        if idx_aligned.iloc[-LB_6M:].isna().any():
            return None

        ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
        if pd.isna(ema200) or curr <= ema200:
            return None

        idx_curr = idx_aligned.iloc[-1]

        # --- 3M (55-day) ---
        start_3m = close.iloc[-LB_3M]
        idx_start_3m = idx_aligned.iloc[-LB_3M]
        perf_3m = (curr / start_3m) - 1
        idx_perf_3m = (idx_curr / idx_start_3m) - 1
        # Ratio-of-relatives form — correct even when the benchmark return is
        # negative. (Naive perf_3m / idx_perf_3m inverts the ranking in that
        # case, which was the bug in the original version of this file.)
        rs_3m = round((1 + perf_3m) / (1 + idx_perf_3m) - 1, 4) if (1 + idx_perf_3m) != 0 else None
        volar_3m = compute_volar(close.iloc[-LB_3M:])

        # --- 6M (122-day) ---
        start_6m = close.iloc[-LB_6M]
        idx_start_6m = idx_aligned.iloc[-LB_6M]
        perf_6m = (curr / start_6m) - 1
        idx_perf_6m = (idx_curr / idx_start_6m) - 1
        rs_6m = round((1 + perf_6m) / (1 + idx_perf_6m) - 1, 4) if (1 + idx_perf_6m) != 0 else None
        volar_6m = compute_volar(close.iloc[-LB_6M:])

        result = {
            "symbol": symbol.replace(".NS", ""),
            "price": round(float(curr), 2),
            "rs_3m": rs_3m,
            "volar_3m": volar_3m,
            "perf_3m": round(perf_3m * 100, 2),
            "rs_6m": rs_6m,
            "volar_6m": volar_6m,
            "perf_6m": round(perf_6m * 100, 2),
        }

        # Any stock that still comes out with a NaN/None in a required field
        # (thin trading, bad tick, etc.) is dropped rather than silently
        # written as 0/NaN into the table.
        if any(result[f] is None or (isinstance(result[f], float) and pd.isna(result[f]))
               for f in REQUIRED_NUMERIC_FIELDS):
            return None

        return result

    except Exception as e:
        print(f"  ERROR processing {symbol}: {e}")
        return None


def _load_history():
    if os.path.exists(HISTORY_JSON):
        try:
            with open(HISTORY_JSON, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _normalize_stock(s):
    """
    Backfills any fields the template expects that an older cached scan (from
    a previous version of this file) might not have written. Without this, a
    stale volar_results_ind_adaptive.json left over from before a schema
    change (e.g. the rank_delta column) crashes the template with an
    UndefinedError the moment a comparison like `stock.rank_delta > 0` runs
    against a genuinely missing key.
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
    return s


def _prune_snapshots(keep_filenames):
    keep = set(keep_filenames)
    for fname in os.listdir(SNAPSHOT_DIR):
        if fname not in keep:
            try:
                os.remove(os.path.join(SNAPSHOT_DIR, fname))
            except OSError:
                pass


def run_scan(symbols, source_name):
    """Runs the full scan in a background thread and persists results/progress."""
    _set_progress(active=True, processed=0, total=len(symbols), current_symbol="",
                  stage="fetching_index", error=None)

    idx_close, benchmark_label = fetch_index_data()

    if idx_close is None:
        _set_progress(active=False, stage="error",
                       error="Could not fetch benchmark index data. Scan aborted — previous results kept.")
        return

    _set_progress(stage="fetching_prices", processed=0, total=len(symbols))
    ticker_symbols = [f"{s}.NS" if not s.endswith(".NS") else s for s in symbols]

    # progress_callback is what actually moves the button's progress bar
    # during this call — this is where the real wall-clock time goes on a
    # cold/stale cache, not the (fast, in-memory) loop below. Without this,
    # the button sits frozen at 0% for the entire fetch, then jumps straight
    # to 100% once the screening loop runs.
    def _fetch_progress(i, total, sym):
        _set_progress(processed=i, total=total, current_symbol=sym)

    price_data, fetch_report = ind_cache.get_price_history_bulk(
        ticker_symbols, interval='1d', lookback_days=LB_6M + 220, progress_callback=_fetch_progress
    )
    price_data_asof = latest_bar_date(price_data)

    # ── Cache source log ─────────────────────────────────────────────────────
    _n  = len(symbols)
    _ch = fetch_report['from_cache']
    _yf = fetch_report['fetched']
    _fl = fetch_report['failed']
    sep = "=" * 55
    print(f"\n{sep}")
    print(f"  [CACHE] IND Adaptive {source_name} — price data source summary")
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

    _set_progress(stage="scanning", processed=0, total=len(symbols), current_symbol="")

    raw_results = []
    for i, (sym, ticker_symbol) in enumerate(zip(symbols, ticker_symbols)):
        _set_progress(processed=i, current_symbol=sym)
        result = is_volar_adaptive_ind(sym, idx_close, price_data.get(ticker_symbol))
        if result:
            raw_results.append(result)
    _set_progress(processed=len(symbols), current_symbol="")

    stocks = []
    if raw_results:
        df = pd.DataFrame(raw_results)

        # Percentile rank both RS periods within this Stage-2 shortlist
        df['rs3_pct'] = df['rs_3m'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)
        df['rs6_pct'] = df['rs_6m'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)

        # --- TREND TRACKING LOGIC ---
        # Sort by 3M RS ratio (strongest relative performers first) and assign
        # rank BEFORE injecting history, since rank_delta needs the rank that
        # was just computed for this scan.
        df = df.sort_values('rs_3m', ascending=False).reset_index(drop=True)
        df['rank'] = df.index + 1

        existing_history = {}
        if os.path.exists(RESULTS_JSON):
            try:
                with open(RESULTS_JSON, 'r') as f:
                    old_cache = json.load(f).get('stocks', [])
                    existing_history = {
                        s['symbol']: {
                            'rs3_h':  s.get('rs3_h', []),
                            'rs6_h':  s.get('rs6_h', []),
                            'vol_h':  s.get('vol_h', []),
                            'perf_h': s.get('perf_h', []),
                            'rank_h': s.get('rank_h', []),
                        } for s in old_cache
                    }
            except (json.JSONDecodeError, OSError):
                existing_history = {}

        def inject_history(row):
            h = existing_history.get(row['symbol'], {'rs3_h': [], 'rs6_h': [], 'vol_h': [], 'perf_h': [], 'rank_h': []})
            row['rs3_h']  = (h['rs3_h']  + [row['rs3_pct']])[-5:]
            row['rs6_h']  = (h['rs6_h']  + [row['rs6_pct']])[-5:]
            row['vol_h']  = (h['vol_h']  + [row['volar_3m']])[-5:]
            row['perf_h'] = (h['perf_h'] + [row['perf_3m']])[-5:]
            row['rank_h'] = (h.get('rank_h', []) + [row['rank']])[-5:]
            row['rs3_up'] = len(row['rs3_h']) > 1 and all(x < y for x, y in zip(row['rs3_h'], row['rs3_h'][1:]))
            row['rs6_up'] = len(row['rs6_h']) > 1 and all(x < y for x, y in zip(row['rs6_h'], row['rs6_h'][1:]))
            # Single-scan rank movement vs the immediately preceding scan only
            # (not a 5-scan streak) — a faster, noisier "mover" signal meant to
            # complement rs3_up/rs6_up, not replace them. Positive = moved up
            # (toward rank 1). None = no prior scan to compare against yet.
            if len(row['rank_h']) > 1:
                row['rank_delta'] = row['rank_h'][-2] - row['rank_h'][-1]
            else:
                row['rank_delta'] = None
            return row

        df = df.apply(inject_history, axis=1)
        # ----------------------------

        stocks = df.to_dict(orient='records')

        # Growing-bars trend indicators (replace the old line sparkline)
        for row in stocks:
            row['rs3_bars'] = make_bar_heights(row['rs3_h'])
            row['rs6_bars'] = make_bar_heights(row['rs6_h'])

    last_processed_time = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    excluded_count = len(symbols) - len(raw_results)

    payload = {
        'stocks': stocks,
        'time': last_processed_time,
        'source': source_name,
        'benchmark_label': benchmark_label,
        'scanned_count': len(symbols),
        'excluded_count': excluded_count,
        # Symbols the shared price cache couldn't refresh this run (e.g. hit
        # a 429 after retries) — their scan result, if any, used whatever
        # data was already cached rather than being dropped or blocked on.
        'stale_symbols_count': len(fetch_report['failed']),
        'stale_symbols_sample': [s.replace('.NS', '') for s in fetch_report['failed'][:10]],
        'cache_hits':           fetch_report['from_cache'],
        'yf_fetches':           fetch_report['fetched'],
        # Most recent trading-day close actually reflected in the price data
        # used for this scan — distinct from 'time' above (when the scan
        # ran). The scan can complete instantly off cached data that's a day
        # or more old if a refresh was skipped/failed, so this is the real
        # "how current is this data" answer.
        'price_data_asof': price_data_asof,
    }

    # Snapshot this scan to disk BEFORE overwriting the active results, so a
    # bad/thin scan (e.g. cut short by a 429 rate-limit mid-run) can always be
    # rolled back to from the Scan History panel.
    snapshot_filename = f"snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(os.path.join(SNAPSHOT_DIR, snapshot_filename), 'w') as f:
        json.dump(payload, f)

    with open(RESULTS_JSON, 'w') as f:
        json.dump(payload, f)

    history = _load_history()
    history.insert(0, {
        "time": last_processed_time,
        "source": source_name,
        "count": len(stocks),
        "benchmark_label": benchmark_label,
        "snapshot_file": snapshot_filename,
    })
    history = history[:HISTORY_LIMIT]
    with open(HISTORY_JSON, 'w') as f:
        json.dump(history, f)

    # Drop snapshot files that fell out of the retained history window
    _prune_snapshots([h.get('snapshot_file') for h in history if h.get('snapshot_file')])

    _set_progress(active=False, stage="done", current_symbol="")


@volar_ind_adaptive_bp.route("/volar-ind-adaptive", methods=["GET", "POST"])
def volar_ind_process():
    if request.method == "POST":
        if _get_progress()["active"]:
            # A scan is already running — don't start a second one.
            return redirect(url_for('volar_ind_adaptive_bp.volar_ind_process', scanning=1))

        file = request.files.get('file')
        use_default = request.form.get('use_default') == '1'

        if file and file.filename != '':
            from werkzeug.utils import secure_filename
            filename      = secure_filename(file.filename)
            ext           = os.path.splitext(filename)[1].lower()
            save_filename = f"uploaded_adaptive_ind_tickers{ext}"
            filepath      = os.path.join(UPLOAD_FOLDER, save_filename)
            file.save(filepath)
            session['last_uploaded_csv_ind'] = filepath
            session['last_filename_ind'] = filename
            session.modified = True

        if use_default:
            # Explicit default chosen — bypass session-pinned file
            saved_path  = DEFAULT_ADP_CSV
            source_name = DEFAULT_ADP_LABEL
            # Also clear session pin so subsequent runs also use default
            session.pop('last_uploaded_csv_ind', None)
            session.pop('last_filename_ind', None)
            session.modified = True
        else:
            saved_path  = session.get('last_uploaded_csv_ind')
            source_name = session.get('last_filename_ind', 'Unknown')

        if not saved_path or not os.path.exists(saved_path):
            err = (f"Default file not found: {DEFAULT_ADP_CSV}. "
                   f"Please place nifty_500.csv in data/ or upload a custom file.")
            _set_progress(active=False, stage="error", error=err)
            return redirect(url_for('volar_ind_adaptive_bp.volar_ind_process'))

        ext_saved = os.path.splitext(saved_path)[1].lower()
        try:
            if ext_saved in ('.xlsx', '.xls'):
                df_input = pd.read_excel(saved_path)
            else:
                df_input = pd.read_csv(saved_path)
        except Exception as e:
            _set_progress(active=False, stage="error", error=f"Could not read file: {e}")
            return redirect(url_for('volar_ind_adaptive_bp.volar_ind_process'))

        col_map  = {c.lower(): c for c in df_input.columns}
        col_name = next((col_map[k] for k in ('symbol', 'ticker', 'symbols', 'tickers') if k in col_map), None)
        if col_name is None:
            _set_progress(active=False, stage="error",
                          error=f"File must have a Symbol or Ticker column. Found: {', '.join(df_input.columns.tolist())}")
            return redirect(url_for('volar_ind_adaptive_bp.volar_ind_process'))

        raw_symbols = (str(s).strip().upper() for s in df_input[col_name].dropna().unique())
        symbols = [s.replace('.NS', '') for s in raw_symbols if str(s).strip()]

        thread = threading.Thread(target=run_scan, args=(symbols, source_name), daemon=True)
        thread.start()

        return redirect(url_for('volar_ind_adaptive_bp.volar_ind_process', scanning=1))

    # --- GET ---
    stocks, last_processed_time, source_name = [], None, "None"
    benchmark_label, excluded_count, scanned_count = None, 0, 0
    stale_symbols_count, stale_symbols_sample = 0, []
    price_data_asof = None

    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON, 'r') as f:
                cache = json.load(f)
                stocks = [_normalize_stock(s) for s in cache.get('stocks', [])]
                last_processed_time = cache.get('time')
                source_name = cache.get('source', 'None')
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

    history = _load_history()
    progress = _get_progress()
    is_scanning = progress["active"] or request.args.get('scanning') == '1'

    active_file      = session.get('last_filename_ind')
    is_default_source = not bool(active_file)

    return render_template(
        "stage2_adaptive_volar_ind_scr.html",
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
        history=history,
        active_file=active_file,
        is_default_source=is_default_source,
        default_label=DEFAULT_ADP_LABEL,
        is_scanning=is_scanning,
        scan_error=progress.get("error"),
        restored=request.args.get('restored') == '1',
        restore_error=request.args.get('restore_error') == '1',
    )


@volar_ind_adaptive_bp.route("/volar-ind-adaptive/clear-source", methods=["POST"])
def volar_ind_clear_source():
    """Remove the pinned CSV from session so the next scan uses Nifty 500 default."""
    session.pop('last_uploaded_csv_ind', None)
    session.pop('last_filename_ind', None)
    session.modified = True
    return redirect(url_for('volar_ind_adaptive_bp.volar_ind_process'))


@volar_ind_adaptive_bp.route("/volar-ind-adaptive/progress")
def volar_ind_progress():
    return jsonify(_get_progress())


@volar_ind_adaptive_bp.route("/volar-ind-adaptive/restore/<snapshot_file>", methods=["POST"])
def volar_ind_restore(snapshot_file):
    """Roll the active results back to an earlier scan snapshot — useful when
    a scan gets cut short by a 429 / rate-limit and overwrites good data with
    a thin result set."""
    safe_name = os.path.basename(snapshot_file)
    snapshot_path = os.path.join(SNAPSHOT_DIR, safe_name)

    valid = (
        safe_name.startswith('snapshot_')
        and safe_name.endswith('.json')
        and os.path.exists(snapshot_path)
    )

    if not valid:
        return redirect(url_for('volar_ind_adaptive_bp.volar_ind_process', restore_error=1))

    try:
        with open(snapshot_path, 'r') as f:
            payload = json.load(f)
        with open(RESULTS_JSON, 'w') as f:
            json.dump(payload, f)
    except (json.JSONDecodeError, OSError):
        return redirect(url_for('volar_ind_adaptive_bp.volar_ind_process', restore_error=1))

    return redirect(url_for('volar_ind_adaptive_bp.volar_ind_process', restored=1))