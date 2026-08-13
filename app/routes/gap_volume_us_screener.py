import os
import json
import uuid
import threading
import pandas as pd
from datetime import datetime
from flask import Blueprint, render_template, request, send_file, redirect, url_for, jsonify

from app.services.market_data_cache import us_cache, latest_bar_date  # shared US SQLite cache

gap_vol_bp = Blueprint("gap_volume", __name__)

# Anchor all paths to __file__ — os.getcwd() at module level is unreliable
# after Werkzeug hot-reload (Memory #12).
_PROJECT_ROOT   = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER   = os.path.join(_PROJECT_ROOT, 'uploads', 'gap_volume_us')
SNAPSHOT_DIR    = os.path.join(UPLOAD_FOLDER, 'snapshots')
RESULTS_JSON    = os.path.join(UPLOAD_FOLDER, 'last_gap_vol_us_results.json')
HISTORY_JSON    = os.path.join(UPLOAD_FOLDER, 'scan_history_gap_vol_us.json')
LAST_CSV_CONFIG = os.path.join(UPLOAD_FOLDER, 'last_csv_us_config.json')
DEFAULT_US_CSV  = os.path.join(_PROJECT_ROOT, 'data', 'sp500.csv')
DEFAULT_US_LABEL = "S&P 500 Default (sp500.csv)"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SNAPSHOT_DIR,  exist_ok=True)

HISTORY_LIMIT  = 5
US_BENCHMARK   = ("^GSPC", "S&P 500")

# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_PROG = {"active": False, "processed": 0, "total": 0,
         "current_symbol": "", "stage": "idle", "error": None}

def _set_progress(**kw):
    with _lock: _PROG.update(kw)

def _get_progress():
    with _lock: return dict(_PROG)


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
    return DEFAULT_US_CSV, DEFAULT_US_LABEL, True


def _read_ticker_file(filepath):
    """Read CSV or XLSX — accept Symbol/Ticker column (case-insensitive)."""
    ext = os.path.splitext(filepath)[1].lower()
    try:
        df = pd.read_excel(filepath) if ext in ('.xlsx', '.xls') else pd.read_csv(filepath)
    except Exception as e:
        raise ValueError(f"Could not read file: {e}")
    col_map = {c.lower(): c for c in df.columns}
    found   = next((col_map[k] for k in ('symbol', 'ticker', 'symbols', 'tickers') if k in col_map), None)
    if found is None:
        raise ValueError(f"File must have a Symbol or Ticker column. Found: {', '.join(df.columns.tolist())}")
    return [str(s).strip().upper() for s in df[found].dropna().unique() if str(s).strip()]


# ---------------------------------------------------------------------------
# Schema normalisation (Memory #3)
# ---------------------------------------------------------------------------

def _normalize_stock(s):
    s.setdefault('symbol',            '')
    s.setdefault('price',             0.0)
    s.setdefault('pullback_pct',      0.0)
    s.setdefault('volume_ratio',      1.0)
    s.setdefault('high_volume_alert', False)
    s.setdefault('rs_percentile',     0)
    s.setdefault('rs_raw',            0.0)
    s.setdefault('has_gap',           False)
    s.setdefault('has_vol',           False)
    s.setdefault('rank',              0)
    s.setdefault('rs_h',              [])
    s.setdefault('rs_up',             False)
    s.setdefault('rank_diff',         0)
    s.setdefault('rank_status',       'stable')
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
# Technical indicators
# ---------------------------------------------------------------------------

def _compute_rs(stock_close, bench_close):
    """
    Ratio-of-relatives RS vs S&P 500 — consistent with every other screener
    in this codebase (Memory #1).

    Old code: stock_return - index_return (subtraction)
    Correct:  (1 + stock_ret) / (1 + bench_ret) - 1
    """
    stock_ret = (stock_close.iloc[-1] / stock_close.iloc[0]) - 1
    bench_ret = (bench_close.iloc[-1] / bench_close.iloc[0]) - 1
    if (1 + bench_ret) == 0:
        return None
    return ((1 + stock_ret) / (1 + bench_ret)) - 1


def _check_gap_up(df, lookback_days=7, gap_threshold=0.01):
    """Gap-up: today's open > previous day's high by >= gap_threshold."""
    if len(df) < (lookback_days + 1):
        return False
    recent = df.tail(lookback_days + 1)
    for i in range(1, len(recent)):
        curr_open = recent['Open'].iloc[i]
        prev_high = recent['High'].iloc[i - 1]
        if prev_high > 0 and (curr_open - prev_high) / prev_high >= gap_threshold:
            return True
    return False


def _check_volume_breakout(df):
    """
    Volume breakout: today > 5-day max AND vol_ratio >= 1.5x 20-day avg.
    Added 1.5x floor (was missing) to avoid firing on minor upticks.
    """
    if len(df) < 21:
        return False, False, 1.0
    curr_vol    = df['Volume'].iloc[-1]
    prev_5d_max = df['Volume'].iloc[-6:-1].max()
    avg_20d     = df['Volume'].iloc[-21:-1].mean()
    vol_ratio   = round(curr_vol / avg_20d, 2) if avg_20d > 0 else 1.0
    is_breakout = curr_vol > prev_5d_max and vol_ratio >= 1.5   # floor added
    is_abnormal = curr_vol > (avg_20d * 2.5) if avg_20d > 0 else False
    return is_breakout, is_abnormal, vol_ratio


# ---------------------------------------------------------------------------
# Background scan
# ---------------------------------------------------------------------------

def run_scan(source_path, source_name):
    _set_progress(active=True, processed=0, total=0,
                  current_symbol="", stage="loading_symbols", error=None)

    try:
        symbols = _read_ticker_file(source_path)
    except ValueError as e:
        _set_progress(active=False, stage="error", error=str(e))
        return
    if not symbols:
        _set_progress(active=False, stage="error", error="No valid symbols found in file.")
        return

    _set_progress(stage="fetching_benchmark", total=len(symbols))

    # Fetch benchmark ONCE via shared cache (Memory #8 — never per-stock)
    bench_data, _ = us_cache.get_price_history_bulk(
        [US_BENCHMARK[0]], interval='1d', lookback_days=500
    )
    bench_df = bench_data.get(US_BENCHMARK[0])
    if bench_df is None or bench_df.empty or len(bench_df) < 200:
        _set_progress(active=False, stage="error",
                      error=f"Could not fetch benchmark {US_BENCHMARK[0]}.")
        return
    bench_close = bench_df['Close'].dropna()

    # Bulk-fetch all symbols through the shared US cache (Memory #8 + #13)
    def _fp(i, total, sym):
        _set_progress(stage="fetching_prices", processed=i, total=total, current_symbol=sym)

    price_data, fetch_report = us_cache.get_price_history_bulk(
        symbols, interval='1d', lookback_days=500, progress_callback=_fp
    )
    price_data_asof = latest_bar_date(price_data)

    # Cache source log (Memory #13)
    _n, _ch, _yf, _fl = len(symbols), fetch_report['from_cache'], fetch_report['fetched'], fetch_report['failed']
    sep = "=" * 55
    print(f"\n{sep}")
    print(f"  [CACHE] GapVol US {source_name}")
    print(f"{sep}")
    print(f"  Total: {_n}  |  From cache: {_ch} ({round(_ch/_n*100) if _n else 0}%)  |  Fetched: {_yf}  |  Failed: {len(_fl)}")
    print(f"  Price data as of: {price_data_asof}")
    print(f"{sep}\n")

    # Load previous RS history and ranks for trend tracking + rank delta
    old_ranks   = {}
    existing_rs = {}
    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON) as f:
                old = json.load(f)
            all_old = (old.get('sections', {}).get('both', []) +
                       old.get('sections', {}).get('vol_only', []) +
                       old.get('sections', {}).get('gap_only', []))
            old_ranks   = {s['symbol']: s['rank']         for s in all_old}
            existing_rs = {s['symbol']: s.get('rs_h', []) for s in all_old}
        except (json.JSONDecodeError, OSError):
            pass

    _set_progress(stage="screening", processed=0, total=len(symbols), current_symbol="")

    raw_results = []
    for i, sym in enumerate(symbols):
        _set_progress(processed=i, current_symbol=sym)
        df = price_data.get(sym)
        if df is None or df.empty:
            continue
        try:
            close = df['Close'].dropna()
            if len(close) < 200:
                continue

            current_price = float(close.iloc[-1])
            high_52w      = float(close.max())
            pullback      = (high_52w - current_price) / high_52w
            ema200        = close.ewm(span=200, adjust=False).mean().iloc[-1]

            if current_price <= ema200 or pullback >= 0.30:
                continue

            bench_aligned = bench_close.reindex(close.index).ffill()
            if bench_aligned.isna().any():
                continue

            rs_val = _compute_rs(close, bench_aligned)
            if rs_val is None:
                continue

            has_gap = _check_gap_up(df, lookback_days=7, gap_threshold=0.01)
            is_vol, is_high_vol, vol_ratio = _check_volume_breakout(df)

            if not has_gap and not is_vol:
                continue

            raw_results.append({
                "symbol":            sym,
                "price":             round(current_price, 2),
                "pullback_pct":      round(pullback * 100, 2),
                "volume_ratio":      vol_ratio,
                "high_volume_alert": is_high_vol,
                "rs_raw":            rs_val,
                "has_gap":           has_gap,
                "has_vol":           is_vol,
            })
        except Exception as e:
            print(f"  Error {sym}: {e}")

    _set_progress(processed=len(symbols), current_symbol="")

    def build_section(records):
        if not records:
            return []
        df = pd.DataFrame(records)
        df['rs_raw'] = pd.to_numeric(df['rs_raw'], errors='coerce')
        df = df.dropna(subset=['rs_raw'])
        if df.empty:
            return []
        df['rs_percentile'] = df['rs_raw'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)
        df.sort_values('rs_percentile', ascending=False, inplace=True)
        df.reset_index(drop=True, inplace=True)
        df['rank'] = df.index + 1

        def inject(row):
            h = existing_rs.get(row['symbol'], [])
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

        return df.apply(inject, axis=1).to_dict(orient='records')

    both     = build_section([r for r in raw_results if r['has_gap'] and r['has_vol']])
    vol_only = build_section([r for r in raw_results if r['has_vol'] and not r['has_gap']])
    gap_only = build_section([r for r in raw_results if r['has_gap'] and not r['has_vol']])

    all_stocks  = both + vol_only + gap_only
    leaders_90  = [s['symbol'] for s in all_stocks if s.get('rs_percentile', 0) >= 90]
    last_time   = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    scanned_cnt = len(symbols)

    payload = {
        'sections':             {'both': both, 'vol_only': vol_only, 'gap_only': gap_only},
        'time':                 last_time,
        'source':               source_name,
        'benchmark_label':      f"{US_BENCHMARK[1]} ({US_BENCHMARK[0]})",
        'scanned_count':        scanned_cnt,
        'passed_count':         len(all_stocks),
        'excluded_count':       scanned_cnt - len(all_stocks),
        'stale_symbols_count':  len(_fl),
        'stale_symbols_sample': _fl[:10],
        'price_data_asof':      price_data_asof,
        'cache_hits':           _ch,
        'yf_fetches':           _yf,
    }

    snapshot_filename = f"snapshot_{uuid.uuid4().hex}.json"
    with open(os.path.join(SNAPSHOT_DIR, snapshot_filename), 'w') as f:
        json.dump(payload, f)

    with open(RESULTS_JSON, 'w') as f:
        json.dump(payload, f)

    history = _load_history()
    history.insert(0, {
        "time":            last_time,
        "source":          source_name,
        "count":           len(all_stocks),
        "count_both":      len(both),
        "count_vol":       len(vol_only),
        "count_gap":       len(gap_only),
        "leaders_90":      leaders_90,
        "benchmark_label": f"{US_BENCHMARK[1]} ({US_BENCHMARK[0]})",
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

@gap_vol_bp.route("/gap-volume-scan", methods=["GET", "POST"])
def gap_volume_scan_process():
    if request.method == "POST":
        if _get_progress()["active"]:
            return redirect(url_for('gap_volume.gap_volume_scan_process', scanning=1))

        file        = request.files.get('file')
        use_default = request.form.get('use_default') == '1'

        if file and file.filename != '':
            from werkzeug.utils import secure_filename
            filename      = secure_filename(file.filename)
            ext           = os.path.splitext(filename)[1].lower()
            save_filename = f"uploaded_gap_vol_us_tickers{ext}"
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
            err = f"Default file not found: {DEFAULT_US_CSV}. Place sp500.csv in data/ or upload a file."
            _set_progress(active=False, stage="error", error=err)
            return redirect(url_for('gap_volume.gap_volume_scan_process'))

        thread = threading.Thread(target=run_scan, args=(source_path, source_name), daemon=True)
        thread.start()
        return redirect(url_for('gap_volume.gap_volume_scan_process', scanning=1))

    # --- GET ---
    sections = {'both': [], 'vol_only': [], 'gap_only': []}
    last_time = benchmark_label = source_name = price_data_asof = None
    scanned_count = passed_count = excluded_count = 0
    stale_symbols_count, stale_symbols_sample = 0, []
    cache_hits = yf_fetches = None

    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON) as f:
                cache = json.load(f)
            raw = cache.get('sections', {})
            sections = {
                'both':     [_normalize_stock(s) for s in raw.get('both', [])],
                'vol_only': [_normalize_stock(s) for s in raw.get('vol_only', [])],
                'gap_only': [_normalize_stock(s) for s in raw.get('gap_only', [])],
            }
            last_time            = cache.get('time')
            benchmark_label      = cache.get('benchmark_label')
            source_name          = cache.get('source')
            scanned_count        = cache.get('scanned_count', 0)
            passed_count         = cache.get('passed_count', 0)
            excluded_count       = cache.get('excluded_count', 0)
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
        "gap_volume_us_screener.html",
        both_stocks=sections['both'],
        vol_stocks=sections['vol_only'],
        gap_stocks=sections['gap_only'],
        last_time=last_time,
        benchmark_label=benchmark_label,
        source_name=source_name,
        scanned_count=scanned_count,
        passed_count=passed_count,
        excluded_count=excluded_count,
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
        restored=request.args.get('restored')      == '1',
        restore_error=request.args.get('restore_error') == '1',
    )


@gap_vol_bp.route("/gap-volume-scan/progress")
def gap_vol_us_progress():
    return jsonify(_get_progress())


@gap_vol_bp.route("/gap-volume-scan/clear-source", methods=["POST"])
def gap_vol_us_clear_source():
    try:
        if os.path.exists(LAST_CSV_CONFIG):
            os.remove(LAST_CSV_CONFIG)
    except OSError:
        pass
    return redirect(url_for('gap_volume.gap_volume_scan_process'))


@gap_vol_bp.route("/restore-gap-vol-us/<snapshot_file>", methods=["POST"])
def restore_gap_vol_us(snapshot_file):
    """POST-only restore — prevents stray click/prefetch overwriting results (Memory #11)."""
    safe_name     = os.path.basename(snapshot_file)
    snapshot_path = os.path.join(SNAPSHOT_DIR, safe_name)
    valid = safe_name.startswith('snapshot_') and safe_name.endswith('.json') and os.path.exists(snapshot_path)
    if not valid:
        return redirect(url_for('gap_volume.gap_volume_scan_process', restore_error=1))
    try:
        with open(snapshot_path) as f:
            payload = json.load(f)
        with open(RESULTS_JSON, 'w') as f:
            json.dump(payload, f)
    except (json.JSONDecodeError, OSError):
        return redirect(url_for('gap_volume.gap_volume_scan_process', restore_error=1))
    return redirect(url_for('gap_volume.gap_volume_scan_process', restored=1))


@gap_vol_bp.route("/export-gap-volume-us")
def export_gap_volume_us():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON) as f:
            data = json.load(f)
        all_records = []
        for label, items in data.get('sections', {}).items():
            for item in items:
                rec = item.copy()
                rec['screener_section'] = label.upper()
                all_records.append(rec)
        if all_records:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_path = os.path.join(UPLOAD_FOLDER, 'temp_export_gap_vol_us.csv')
            pd.DataFrame(all_records).to_csv(temp_path, index=False)
            return send_file(temp_path, as_attachment=True,
                             download_name=f"US_GapVolume_Screener_{timestamp}.csv")
    return "No scan data available.", 404