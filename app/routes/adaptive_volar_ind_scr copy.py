import os
import json
import threading
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, session, jsonify, redirect, url_for

volar_ind_adaptive_bp = Blueprint('volar_ind_adaptive_bp', __name__)

UPLOAD_FOLDER = os.path.join(os.getcwd(), 'uploads', 'volar_ind_adaptive')
SNAPSHOT_DIR = os.path.join(UPLOAD_FOLDER, 'snapshots')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SNAPSHOT_DIR, exist_ok=True)

RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'volar_results_ind_adaptive.json')
HISTORY_JSON = os.path.join(UPLOAD_FOLDER, 'scan_history_ind_adaptive.json')

# How many past scans to keep browsable/restorable
HISTORY_LIMIT = 5

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
# In-memory scan progress (single-process; fine for a personal-use tool).
# ---------------------------------------------------------------------------
progress_lock = threading.Lock()
SCAN_PROGRESS = {
    "active": False,
    "processed": 0,
    "total": 0,
    "current_symbol": "",
    "stage": "idle",   # idle | fetching_index | scanning | done | error
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


def fetch_index_data(start_date):
    """
    Fetch the benchmark ONCE per scan (not per stock — repeated per-stock
    fetches were tripping yfinance rate limits mid-scan and corrupting
    results with NaNs). Tries Nifty 500 first, falls back to Nifty 50.
    Returns (close_series, label) or (None, None) if both fail.
    """
    for ticker_sym, label in (PRIMARY_INDEX, FALLBACK_INDEX):
        try:
            idx_df = yf.Ticker(ticker_sym).history(start=start_date)
            if len(idx_df) >= LB_6M:
                return idx_df['Close'].dropna(), f"{label} ({ticker_sym})"
        except Exception as e:
            print(f"  Benchmark fetch failed for {ticker_sym}: {e}")
    return None, None


def is_volar_adaptive_ind(symbol, idx_close):
    """
    Computes RS and VOLAR for both 3M (55-day) and 6M (122-day) lookback
    periods against a pre-fetched benchmark series. Returns None if the
    stock fails the EMA200 filter or lacks clean data.
    """
    try:
        ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol

        fetch_days = LB_6M + 220
        start_date = (datetime.today() - timedelta(days=fetch_days)).strftime("%Y-%m-%d")

        df = yf.Ticker(ticker_symbol).history(start=start_date)
        if len(df) < LB_6M:
            return None

        close = df['Close'].dropna()
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

    fetch_days = LB_6M + 220
    start_date = (datetime.today() - timedelta(days=fetch_days)).strftime("%Y-%m-%d")
    idx_close, benchmark_label = fetch_index_data(start_date)

    if idx_close is None:
        _set_progress(active=False, stage="error",
                       error="Could not fetch benchmark index data. Scan aborted — previous results kept.")
        return

    _set_progress(stage="scanning")

    raw_results = []
    for i, sym in enumerate(symbols):
        _set_progress(processed=i, current_symbol=sym)
        result = is_volar_adaptive_ind(sym, idx_close)
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
        if file and file.filename != '':
            filepath = os.path.join(UPLOAD_FOLDER, file.filename)
            file.save(filepath)
            session['last_uploaded_csv_ind'] = filepath
            session['last_filename_ind'] = file.filename
            session.modified = True

        saved_path = session.get('last_uploaded_csv_ind')
        if not saved_path or not os.path.exists(saved_path):
            return redirect(url_for('volar_ind_adaptive_bp.volar_ind_process'))

        df_input = pd.read_csv(saved_path)
        symbols = df_input['symbol'].dropna().unique().tolist()
        source_name = session.get('last_filename_ind', 'Unknown')

        thread = threading.Thread(target=run_scan, args=(symbols, source_name), daemon=True)
        thread.start()

        return redirect(url_for('volar_ind_adaptive_bp.volar_ind_process', scanning=1))

    # --- GET ---
    stocks, last_processed_time, source_name = [], None, "None"
    benchmark_label, excluded_count, scanned_count = None, 0, 0

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
        except (json.JSONDecodeError, OSError):
            pass

    history = _load_history()
    progress = _get_progress()
    is_scanning = progress["active"] or request.args.get('scanning') == '1'

    return render_template(
        "stage2_adaptive_volar_ind_scr.html",
        stocks=stocks,
        last_processed_time=last_processed_time,
        source_name=source_name,
        benchmark_label=benchmark_label,
        excluded_count=excluded_count,
        scanned_count=scanned_count,
        history=history,
        active_file=session.get('last_filename_ind'),
        is_scanning=is_scanning,
        scan_error=progress.get("error"),
        restored=request.args.get('restored') == '1',
        restore_error=request.args.get('restore_error') == '1',
    )


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